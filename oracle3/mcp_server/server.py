"""Oracle3 MCP server.

Exposes public Kalshi and Polymarket market data, the venues' published fee
schedules, static no-arbitrage checks across related contracts, and a local
paper-trading ledger to any Model Context Protocol client.

No tool can place a real order: the server imports no authenticated trader and
``paper_order`` only writes to a local JSON ledger.

Run it over stdio (the default, for desktop and IDE clients)::

    oracle3-mcp

or over streamable HTTP::

    oracle3-mcp --transport streamable-http
"""

from __future__ import annotations

import argparse
import functools
import inspect
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

import httpx
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from oracle3 import __version__
from oracle3.arbitrage import RELATIONS, Quote
from oracle3.arbitrage import check_constraint as _check
from oracle3.fees import KalshiSchedule, PolymarketSchedule, UnsupportedFeeSchedule
from oracle3.mcp_server import venues
from oracle3.mcp_server.paper import PaperLedger
from oracle3.pricing.distortion import ProbitDistortion

Venue = Literal['kalshi', 'polymarket']
Relation = Literal[
    'implication', 'exclusivity', 'complement', 'same_event', 'event_sum'
]

INSTRUCTIONS = """\
Oracle3 gives read access to Kalshi and Polymarket and checks whether prices of
related contracts violate the axioms of probability after venue fees.

Prices are dollars per contract, between 0 and 1. No tool places a real order;
paper_order fills against the live order book into a local paper ledger.

Typical flow: search_markets -> get_quote or get_orderbook on candidates ->
check_constraint_live with the relation you believe holds -> paper_order the
legs of the best basket.

Relations: implication (A implies B), exclusivity (at most one outcome),
complement (exactly one of two), same_event (one event on two venues),
event_sum (exactly one of n). Whether a relation actually holds is your
judgment; read both markets' resolution rules before relying on a result.

Fees follow each market's own schedule from the venue API: Kalshi taker
0.07*M*C*P*(1-P) (maker 0.0175*M*C*P*(1-P) only on series with maker fees),
Polymarket taker rate*C*p*(1-p), Polymarket makers free.
"""

WEBSITE = 'https://yichengyang-ethan.github.io/oracle3-prediction-market-agent/'

try:  # mcp >= 2 renamed FastMCP to MCPServer and takes the version directly
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    mcp: Any = MCPServer(
        'oracle3', instructions=INSTRUCTIONS, website_url=WEBSITE, version=__version__
    )
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore

    mcp = FastMCP('oracle3', instructions=INSTRUCTIONS, website_url=WEBSITE)
    # FastMCP has no version argument; without this the SDK's own version is reported.
    mcp._mcp_server.version = __version__

# One INFO line per venue request drowns out the protocol traffic on stderr.
logging.getLogger('httpx').setLevel(logging.WARNING)

# Errors an agent can act on: a venue call failed, an input was out of range, or a
# market uses a fee schedule we cannot price. mcp 2.x hides the message of any
# exception that is not a ToolError, so these are re-raised as ToolError.
_USER_ERRORS = (
    venues.VenueError,
    UnsupportedFeeSchedule,
    ValueError,
    KeyError,
    ArithmeticError,  # e.g. decimal.InvalidOperation for a NaN size
    httpx.InvalidURL,  # a market id that cannot form a URL
)


def _agent_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except _USER_ERRORS as exc:
                raise ToolError(str(exc)) from exc

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
PURE = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
LOCAL_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
PAPER = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
PAPER_RESET = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, openWorldHint=False
)


class QuoteInput(BaseModel):
    """Quotes for one contract, in dollars. A missing ask is derived from the other side's bid."""

    market_id: str
    venue: Venue = 'kalshi'
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    kalshi_multiplier: float | None = Field(
        default=None, description='Kalshi series fee multiplier M (default 1).'
    )
    kalshi_maker_fees: bool = Field(
        default=False, description='Whether the Kalshi series charges maker fees.'
    )
    polymarket_rate: float | None = Field(
        default=None,
        description='Polymarket taker fee rate for this market (default: 0.07, the highest documented rate).',
    )


class MarketRef(BaseModel):
    venue: Venue
    market_id: str


def _schedule(q: QuoteInput) -> KalshiSchedule | PolymarketSchedule:
    if q.venue == 'kalshi':
        return KalshiSchedule(
            multiplier=Decimal(
                str(1.0 if q.kalshi_multiplier is None else q.kalshi_multiplier)
            ),
            maker_fees=q.kalshi_maker_fees,
        )
    if q.polymarket_rate is None:
        return PolymarketSchedule()
    return PolymarketSchedule(
        rate=Decimal(str(q.polymarket_rate)), source='supplied rate'
    )


def _quote_dict(q: Quote) -> dict[str, Any]:
    schedule = q.fee_schedule()
    return {
        'venue': q.venue,
        'market_id': q.market_id,
        'title': q.title,
        'yes_bid': q.yes_bid,
        'yes_ask': q.ask('yes'),
        'no_bid': q.no_bid,
        'no_ask': q.ask('no'),
        'fee_schedule': schedule.describe(),
    }


@mcp.tool(annotations=READ)
@_agent_errors
async def search_markets(
    venue: Venue, query: str = '', limit: int = 20, series_ticker: str = ''
) -> dict[str, Any]:
    """Find open markets by keyword. On Kalshi, series_ticker (e.g. KXFEDDECISION) lists one series."""
    markets = await venues.search(venue, query, max(1, min(limit, 100)), series_ticker)
    return {'venue': venue, 'query': query, 'count': len(markets), 'markets': markets}


@mcp.tool(annotations=READ)
@_agent_errors
async def get_market(venue: Venue, market_id: str) -> dict[str, Any]:
    """Market details: title, best prices, volume, close time and (Polymarket) fee schedule and outcomes."""
    return await venues.market(venue, market_id)


@mcp.tool(annotations=READ)
@_agent_errors
async def get_orderbook(
    venue: Venue, market_id: str, depth: int = 10
) -> dict[str, Any]:
    """Order book for both sides as [price, size] levels, best first."""
    return await venues.orderbook(venue, market_id, max(1, min(depth, 50)))


@mcp.tool(annotations=READ)
@_agent_errors
async def get_quote(venue: Venue, market_id: str) -> dict[str, Any]:
    """Best bid and ask on YES and NO plus the fee schedule the venue reports for this market."""
    return _quote_dict(await venues.quote(venue, market_id))


@mcp.tool(annotations=PURE)
@_agent_errors
def trading_fee(
    venue: Venue,
    price: float,
    contracts: float,
    maker: bool = False,
    kalshi_multiplier: float = 1.0,
    kalshi_maker_fees: bool = False,
    polymarket_rate: float | None = None,
) -> dict[str, Any]:
    """Fee for one fill under the venue's published schedule."""
    q = QuoteInput(
        market_id='-',
        venue=venue,
        kalshi_multiplier=kalshi_multiplier,
        kalshi_maker_fees=kalshi_maker_fees,
        polymarket_rate=polymarket_rate,
    )
    schedule = _schedule(q)
    fee = schedule.fee(price, Decimal(str(contracts)), maker=maker)
    return {
        'fee': float(fee),
        'fee_per_contract': float(fee) / contracts,
        'order_type': 'maker' if maker else 'taker',
        'schedule': schedule.describe(),
    }


@mcp.tool(annotations=PURE)
@_agent_errors
def check_constraint(
    relation: Relation,
    quotes: list[QuoteInput],
    contracts: float = 1.0,
    maker: bool = False,
) -> dict[str, Any]:
    """Check a no-arbitrage relation on quotes you supply (offline).

    Quote order: implication (A, B) means A implies B; complement and same_event take (A, B);
    exclusivity and event_sum take every outcome.
    """
    parsed = [
        Quote(
            market_id=q.market_id,
            venue=q.venue,
            yes_bid=q.yes_bid,
            yes_ask=q.yes_ask,
            no_bid=q.no_bid,
            no_ask=q.no_ask,
            schedule=_schedule(q),
        )
        for q in quotes
    ]
    return _check(relation, parsed, contracts=contracts, maker=maker).to_dict()


@mcp.tool(annotations=READ)
@_agent_errors
async def check_constraint_live(
    relation: Relation,
    markets: list[MarketRef],
    contracts: float = 1.0,
    maker: bool = False,
) -> dict[str, Any]:
    """Fetch current quotes and fee schedules for the markets, then check the relation."""
    quotes = [await venues.quote(m.venue, m.market_id) for m in markets]
    result = _check(relation, quotes, contracts=contracts, maker=maker).to_dict()
    result['quotes'] = [_quote_dict(q) for q in quotes]
    return result


@mcp.tool(annotations=PURE)
@_agent_errors
def list_relation_types() -> dict[str, Any]:
    """Supported relations and the probability bound each one enforces."""
    return {'relations': RELATIONS}


@mcp.tool(annotations=PURE)
@_agent_errors
def fair_value(market_price: float, lam: float = 0.183) -> dict[str, Any]:
    """Probability implied by a market price under the Wang transform p_mkt = Phi(Phi^-1(p) + lam).

    The default lam = 0.183 is the pooled estimate in Yang (2026), SSRN 6468338; it pools
    real-money and play-money venues, so treat it as illustrative.
    """
    if not 0.0 < market_price < 1.0:
        raise ValueError('market_price must be strictly between 0 and 1')
    implied = ProbitDistortion(lam).inverse(market_price)
    return {
        'market_price': market_price,
        'lambda': lam,
        'implied_probability': implied,
        'premium': market_price - implied,
    }


@mcp.tool(annotations=LOCAL_READ)
@_agent_errors
def list_relations(
    market_id: str = '', spread_type: str = '', status: str = ''
) -> dict[str, Any]:
    """Relations saved locally by the oracle3 research CLI (~/.oracle3/relations.json)."""
    from oracle3.market.relations import RelationStore

    store = RelationStore()
    rows = store.find_by_market(market_id) if market_id else store.list()
    if spread_type:
        rows = [r for r in rows if r.spread_type == spread_type]
    if status:
        rows = [r for r in rows if r.status == status]
    return {
        'store': str(store.path),
        'count': len(rows),
        'relations': [r.to_dict() for r in rows],
    }


@mcp.tool(annotations=PAPER)
@_agent_errors
async def paper_order(
    venue: Venue,
    market_id: str,
    side: Literal['yes', 'no'],
    contracts: float,
    limit_price: float,
) -> dict[str, Any]:
    """Buy in the local paper ledger, filling against the live displayed book with venue fees. Never trades for real."""
    book = await venues.orderbook(venue, market_id, depth=50)
    schedule = await venues.fee_schedule(venue, market_id)
    return PaperLedger().buy(
        venue=venue,
        market_id=market_id,
        side=side,
        contracts=contracts,
        limit_price=limit_price,
        asks=[(float(p), float(q)) for p, q in book[side]['asks']],
        schedule=schedule,
    )


@mcp.tool(annotations=LOCAL_READ)
@_agent_errors
def paper_portfolio() -> dict[str, Any]:
    """Cash, positions and fill count in the local paper ledger."""
    return PaperLedger().portfolio()


@mcp.tool(annotations=PAPER_RESET)
@_agent_errors
def paper_reset(confirm: bool = False) -> dict[str, Any]:
    """Erase the paper ledger and restore starting cash. Requires confirm=true."""
    if not confirm:
        return {
            'status': 'not_reset',
            'reason': 'pass confirm=true to erase the paper ledger',
        }
    return PaperLedger().reset()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog='oracle3-mcp', description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        '--transport', choices=['stdio', 'streamable-http', 'sse'], default='stdio'
    )
    args = parser.parse_args(argv)
    mcp.run(transport=args.transport)


if __name__ == '__main__':
    main()
