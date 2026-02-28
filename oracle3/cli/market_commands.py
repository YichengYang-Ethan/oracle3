"""CLI commands for browsing prediction markets on Polymarket and Kalshi."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import click
import httpx

# ---------------------------------------------------------------------------
# Polymarket helpers (via Gamma API — no auth required)
# ---------------------------------------------------------------------------

GAMMA_EVENTS_URL = 'https://gamma-api.polymarket.com/events'
GAMMA_MARKETS_URL = 'https://gamma-api.polymarket.com/markets'
CLOB_PRICES_HISTORY_URL = 'https://clob.polymarket.com/prices-history'


def _parse_clob_ids(mkt: dict) -> list[str]:
    """Return the parsed list of CLOB token IDs from a market dict (handles JSON-string encoding)."""
    raw = mkt.get('clobTokenIds') or mkt.get('clob_token_ids') or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raw = []
    return list(raw)


async def _polymarket_list_markets(limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            GAMMA_EVENTS_URL,
            params={'active': 'true', 'closed': 'false', 'limit': min(limit, 100)},
        )
    if resp.status_code != 200:
        raise click.ClickException(
            f'Polymarket API returned HTTP {resp.status_code}: {resp.text[:200]}'
        )
    events = resp.json()
    markets = []
    for event in events[:limit]:
        for mkt in event.get('markets', []):
            if len(markets) >= limit:
                break
            markets.append(
                {
                    'id': mkt.get('id', ''),
                    'question': mkt.get('question', ''),
                    'event_id': str(event.get('id', '')),
                    'event_title': event.get('title', ''),
                    'token_id': _parse_clob_ids(mkt)[0] if _parse_clob_ids(mkt) else '',
                    'best_bid': mkt.get('bestBid', ''),
                    'best_ask': mkt.get('bestAsk', ''),
                    'volume': mkt.get('volume', ''),
                    'end_date': mkt.get('endDate', ''),
                }
            )
        if len(markets) >= limit:
            break
    return markets[:limit]


async def _polymarket_search_markets(query: str, limit: int) -> list[dict]:
    all_markets = await _polymarket_list_markets(500)
    q = query.lower()
    filtered = [
        m
        for m in all_markets
        if q in m.get('question', '').lower() or q in m.get('event_title', '').lower()
    ]
    return filtered[:limit]


async def _polymarket_market_info(market_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(GAMMA_MARKETS_URL, params={'id': market_id})
    if resp.status_code != 200:
        return None
    data = resp.json()
    if isinstance(data, list) and data:
        mkt = data[0]
    elif isinstance(data, dict):
        mkt = data
    else:
        return None

    clob_ids = _parse_clob_ids(mkt)
    return {
        'id': mkt.get('id', ''),
        'question': mkt.get('question', ''),
        'event_id': str(mkt.get('eventId', '')),
        'token_id': clob_ids[0] if clob_ids else '',
        'no_token_id': clob_ids[1] if len(clob_ids) > 1 else '',
        'best_bid': mkt.get('bestBid', ''),
        'best_ask': mkt.get('bestAsk', ''),
        'volume': mkt.get('volume', ''),
        'end_date': mkt.get('endDate', ''),
        'description': mkt.get('description', ''),
        'active': mkt.get('active', True),
        'closed': mkt.get('closed', False),
    }


# ---------------------------------------------------------------------------
# Kalshi helpers
# ---------------------------------------------------------------------------

KALSHI_API_URL = 'https://api.elections.kalshi.com/trade-api/v2'


async def _kalshi_list_markets(
    limit: int, api_key_id: str | None, private_key_path: str | None
) -> list[dict]:
    from kalshi_python import Configuration
    from kalshi_python.api.markets_api import MarketsApi
    from kalshi_python.api_client import ApiClient

    config = Configuration(host=KALSHI_API_URL)
    key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
    pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if key_id and pk_path:
        with open(pk_path) as f:
            config.private_key_pem = f.read()
        config.api_key_id = key_id

    api_client = ApiClient(configuration=config)
    markets_api = MarketsApi(api_client)

    kwargs: dict[str, Any] = {'status': 'open', 'limit': min(limit, 200)}
    response = await asyncio.to_thread(lambda: markets_api.get_markets(**kwargs))
    raw = response.markets if hasattr(response, 'markets') else []
    markets = []
    for m in (raw or [])[:limit]:
        d = m.to_dict() if hasattr(m, 'to_dict') else dict(m)
        markets.append(
            {
                'ticker': d.get('ticker', ''),
                'title': d.get('title', ''),
                'event_ticker': d.get('event_ticker', ''),
                'series_ticker': d.get('series_ticker', ''),
                'yes_bid': d.get('yes_bid', 0),
                'yes_ask': d.get('yes_ask', 0),
                'volume': d.get('volume', 0),
                'close_time': str(d.get('close_time', '')),
                'status': d.get('status', ''),
            }
        )
    return markets


async def _kalshi_search_markets(
    query: str, limit: int, api_key_id: str | None, private_key_path: str | None
) -> list[dict]:
    all_markets = await _kalshi_list_markets(500, api_key_id, private_key_path)
    q = query.lower()
    filtered = [
        m
        for m in all_markets
        if q in m.get('title', '').lower() or q in m.get('ticker', '').lower()
    ]
    return filtered[:limit]


async def _kalshi_market_info(
    market_ticker: str, api_key_id: str | None, private_key_path: str | None
) -> dict | None:
    from kalshi_python import Configuration
    from kalshi_python.api.markets_api import MarketsApi
    from kalshi_python.api_client import ApiClient

    config = Configuration(host=KALSHI_API_URL)
    key_id = api_key_id or os.environ.get('KALSHI_API_KEY_ID')
    pk_path = private_key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if key_id and pk_path:
        with open(pk_path) as f:
            config.private_key_pem = f.read()
        config.api_key_id = key_id

    api_client = ApiClient(configuration=config)
    markets_api = MarketsApi(api_client)

    response = await asyncio.to_thread(lambda: markets_api.get_market(market_ticker))
    if not response:
        return None
    m = response.market if hasattr(response, 'market') else response
    d = m.to_dict() if hasattr(m, 'to_dict') else dict(m)
    return {
        'ticker': d.get('ticker', ''),
        'title': d.get('title', ''),
        'event_ticker': d.get('event_ticker', ''),
        'series_ticker': d.get('series_ticker', ''),
        'yes_bid': d.get('yes_bid', 0),
        'yes_ask': d.get('yes_ask', 0),
        'volume': d.get('volume', 0),
        'close_time': str(d.get('close_time', '')),
        'status': d.get('status', ''),
        'rules_primary': d.get('rules_primary', ''),
    }


# ---------------------------------------------------------------------------
# DFlow/Solana helpers (no auth required)
# ---------------------------------------------------------------------------

DFLOW_METADATA_URL = 'https://dev-prediction-markets-api.dflow.net'


async def _dflow_list_markets(limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f'{DFLOW_METADATA_URL}/api/v1/events',
            params={'status': 'active', 'withNestedMarkets': 'true'},
        )
    if resp.status_code != 200:
        raise click.ClickException(
            f'DFlow API returned HTTP {resp.status_code}: {resp.text[:200]}'
        )
    data = resp.json()
    events = data if isinstance(data, list) else data.get('events', data.get('data', []))
    markets: list[dict] = []
    for event in events:
        for mkt in event.get('markets', []):
            if len(markets) >= limit:
                break
            ticker = mkt.get('ticker', mkt.get('marketTicker', ''))
            markets.append({
                'ticker': ticker,
                'title': mkt.get('title', mkt.get('question', '')),
                'event_ticker': event.get('eventTicker', event.get('event_ticker', '')),
                'series_ticker': event.get('seriesTicker', event.get('series_ticker', '')),
                'yes_bid': mkt.get('yesBid', mkt.get('yes_bid', 0)) or 0,
                'yes_ask': mkt.get('yesAsk', mkt.get('yes_ask', 0)) or 0,
                'volume': mkt.get('volume', 0),
                'status': mkt.get('status', 'active'),
            })
        if len(markets) >= limit:
            break
    return markets[:limit]


async def _dflow_search_markets(query: str, limit: int) -> list[dict]:
    all_markets = await _dflow_list_markets(500)
    q = query.lower()
    filtered = [
        m for m in all_markets
        if q in m.get('title', '').lower() or q in m.get('ticker', '').lower()
    ]
    return filtered[:limit]


async def _dflow_market_info(market_ticker: str) -> dict | None:
    all_markets = await _dflow_list_markets(1000)
    for m in all_markets:
        if m.get('ticker', '') == market_ticker:
            return m
    return None


def _fmt_dflow_market(m: dict, idx: int) -> str:
    bid = m.get('yes_bid', 0) or 0
    ask = m.get('yes_ask', 0) or 0
    bid_str = f'{bid}' if isinstance(bid, (int, float)) and bid <= 1 else f'{bid}¢'
    ask_str = f'{ask}' if isinstance(ask, (int, float)) and ask <= 1 else f'{ask}¢'
    lines = [f'[{idx}] {m.get("title", "(no title)")}']
    lines.append(f'     Ticker:   {m.get("ticker", "")}')
    lines.append(f'     Event:    {m.get("event_ticker", "")}')
    lines.append(f'     Bid/Ask:  {bid_str} / {ask_str}')
    if m.get('volume'):
        lines.append(f'     Volume:   {m["volume"]}')
    lines.append('     Chain:    Solana (mainnet-beta)')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_poly_market(m: dict, idx: int) -> str:
    lines = [f'[{idx}] {m.get("question", "(no question)")}']
    event = m.get('event_title')
    if event and event != m.get('question'):
        lines.append(f'     Event:    {event}')
    lines.append(f'     Market ID: {m.get("id", "")}')
    bid = m.get('best_bid', '')
    ask = m.get('best_ask', '')
    if bid or ask:
        lines.append(f'     Bid/Ask:  {bid} / {ask}')
    if m.get('volume'):
        lines.append(f'     Volume:   {m["volume"]}')
    if m.get('end_date'):
        lines.append(f'     Closes:   {m["end_date"]}')
    return '\n'.join(lines)


def _fmt_kalshi_market(m: dict, idx: int) -> str:
    bid_cents = m.get('yes_bid', 0) or 0
    ask_cents = m.get('yes_ask', 0) or 0
    bid_pct = f'{bid_cents}¢'
    ask_pct = f'{ask_cents}¢'
    lines = [f'[{idx}] {m.get("title", "(no title)")}']
    lines.append(f'     Ticker:   {m.get("ticker", "")}')
    lines.append(f'     Event:    {m.get("event_ticker", "")}')
    lines.append(f'     Bid/Ask:  {bid_pct} / {ask_pct}')
    if m.get('volume'):
        lines.append(f'     Volume:   {m["volume"]}')
    if m.get('close_time'):
        lines.append(f'     Closes:   {m["close_time"]}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Click group + commands
# ---------------------------------------------------------------------------


@click.group()
def market() -> None:
    """Explore prediction markets on Polymarket, Kalshi, and Solana/DFlow."""


@market.command('list')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'solana']),
    default='polymarket',
    show_default=True,
)
@click.option('--limit', default=20, show_default=True, type=int)
@click.option(
    '--kalshi-api-key-id',
    default=None,
    help='Kalshi API key id (or KALSHI_API_KEY_ID).',
)
@click.option(
    '--kalshi-private-key-path',
    default=None,
    help='Kalshi private key path (or KALSHI_PRIVATE_KEY_PATH).',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_list(
    exchange: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """List open markets on a prediction exchange."""
    try:
        if exchange == 'polymarket':
            markets = asyncio.run(_polymarket_list_markets(limit))
        elif exchange == 'kalshi':
            markets = asyncio.run(
                _kalshi_list_markets(limit, kalshi_api_key_id, kalshi_private_key_path)
            )
        else:
            markets = asyncio.run(_dflow_list_markets(limit))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch markets: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {'exchange': exchange, 'count': len(markets), 'markets': markets}
            )
        )
        return

    if not markets:
        click.echo('No markets found.')
        return

    click.echo(f'Listing {len(markets)} open market(s) on {exchange}:\n')
    for i, m in enumerate(markets, 1):
        if exchange == 'polymarket':
            click.echo(_fmt_poly_market(m, i))
        elif exchange == 'kalshi':
            click.echo(_fmt_kalshi_market(m, i))
        else:
            click.echo(_fmt_dflow_market(m, i))
        click.echo()


@market.command('search')
@click.option(
    '--query', required=True, help='Keyword to search in market title/question.'
)
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'solana']),
    default='polymarket',
    show_default=True,
)
@click.option('--limit', default=20, show_default=True, type=int)
@click.option('--kalshi-api-key-id', default=None)
@click.option('--kalshi-private-key-path', default=None)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_search(
    query: str,
    exchange: str,
    limit: int,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Search markets by keyword."""
    try:
        if exchange == 'polymarket':
            markets = asyncio.run(_polymarket_search_markets(query, limit))
        elif exchange == 'kalshi':
            markets = asyncio.run(
                _kalshi_search_markets(
                    query, limit, kalshi_api_key_id, kalshi_private_key_path
                )
            )
        else:
            markets = asyncio.run(_dflow_search_markets(query, limit))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to search markets: {exc}') from exc

    if as_json:
        click.echo(
            json.dumps(
                {
                    'exchange': exchange,
                    'query': query,
                    'count': len(markets),
                    'markets': markets,
                }
            )
        )
        return

    if not markets:
        click.echo(f'No markets found matching {query!r}.')
        return

    click.echo(f'Found {len(markets)} market(s) matching {query!r} on {exchange}:\n')
    for i, m in enumerate(markets, 1):
        if exchange == 'polymarket':
            click.echo(_fmt_poly_market(m, i))
        elif exchange == 'kalshi':
            click.echo(_fmt_kalshi_market(m, i))
        else:
            click.echo(_fmt_dflow_market(m, i))
        click.echo()


@market.command('info')
@click.option('--market-id', required=True, help='Market ID or ticker to inspect.')
@click.option(
    '--exchange',
    type=click.Choice(['polymarket', 'kalshi', 'solana']),
    default='polymarket',
    show_default=True,
)
@click.option('--kalshi-api-key-id', default=None)
@click.option('--kalshi-private-key-path', default=None)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_info(
    market_id: str,
    exchange: str,
    kalshi_api_key_id: str | None,
    kalshi_private_key_path: str | None,
    as_json: bool,
) -> None:
    """Show detailed info and top-of-book for a specific market."""
    try:
        if exchange == 'polymarket':
            info = asyncio.run(_polymarket_market_info(market_id))
        elif exchange == 'kalshi':
            info = asyncio.run(
                _kalshi_market_info(
                    market_id, kalshi_api_key_id, kalshi_private_key_path
                )
            )
        else:
            info = asyncio.run(_dflow_market_info(market_id))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch market info: {exc}') from exc

    if info is None:
        raise click.ClickException(f'Market not found: {market_id}')

    if as_json:
        click.echo(json.dumps({'exchange': exchange, 'market': info}))
        return

    click.echo(f'\nMarket Info ({exchange})')
    click.echo('=' * 60)
    if exchange == 'polymarket':
        click.echo(f'Question:   {info.get("question", "")}')
        click.echo(f'Market ID:  {info.get("id", "")}')
        click.echo(f'Event ID:   {info.get("event_id", "")}')
        click.echo(f'Token ID:   {info.get("token_id", "")}')
        bid = info.get('best_bid', '')
        ask = info.get('best_ask', '')
        click.echo(f'Bid / Ask:  {bid} / {ask}')
        click.echo(f'Volume:     {info.get("volume", "")}')
        click.echo(f'Closes:     {info.get("end_date", "")}')
        click.echo(f'Active:     {info.get("active", True)}')
        desc = info.get('description', '')
        if desc:
            click.echo(f'Description: {desc[:300]}{"…" if len(desc) > 300 else ""}')
    elif exchange == 'kalshi':
        click.echo(f'Title:      {info.get("title", "")}')
        click.echo(f'Ticker:     {info.get("ticker", "")}')
        click.echo(f'Event:      {info.get("event_ticker", "")}')
        bid_c = info.get('yes_bid', 0) or 0
        ask_c = info.get('yes_ask', 0) or 0
        click.echo(f'Bid / Ask:  {bid_c}¢ / {ask_c}¢')
        click.echo(f'Volume:     {info.get("volume", 0)}')
        click.echo(f'Closes:     {info.get("close_time", "")}')
        click.echo(f'Status:     {info.get("status", "")}')
        rules = info.get('rules_primary', '')
        if rules:
            click.echo(f'Rules:      {rules[:300]}{"…" if len(rules) > 300 else ""}')
    else:
        click.echo(f'Title:      {info.get("title", "")}')
        click.echo(f'Ticker:     {info.get("ticker", "")}')
        click.echo(f'Event:      {info.get("event_ticker", "")}')
        click.echo(f'Series:     {info.get("series_ticker", "")}')
        bid = info.get('yes_bid', 0) or 0
        ask = info.get('yes_ask', 0) or 0
        click.echo(f'Bid / Ask:  {bid} / {ask}')
        click.echo(f'Volume:     {info.get("volume", 0)}')
        click.echo(f'Status:     {info.get("status", "")}')
        click.echo('Chain:      Solana (mainnet-beta)')
    click.echo()


# ---------------------------------------------------------------------------
# Polymarket price history
# ---------------------------------------------------------------------------

_INTERVAL_FIDELITY: dict[str, int] = {
    '1d': 1440,
    '6h': 360,
    '1h': 60,
}


async def _polymarket_price_history(
    market_id: str, interval: str, limit: int | None
) -> dict:
    fidelity = _INTERVAL_FIDELITY.get(interval, 1440)

    # Resolve the CLOB token ID from the numeric market ID.
    info = await _polymarket_market_info(market_id)
    if info is None:
        raise click.ClickException(f'Market not found: {market_id}')
    token_id = info.get('token_id', '')
    if not token_id:
        raise click.ClickException(f'No CLOB token ID for market: {market_id}')

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            CLOB_PRICES_HISTORY_URL,
            params={'market': token_id, 'interval': interval, 'fidelity': fidelity},
        )
    if resp.status_code != 200:
        raise click.ClickException(
            f'Polymarket CLOB API returned HTTP {resp.status_code}: {resp.text[:200]}'
        )
    data = resp.json()

    # Response is {"history": [{"t": ..., "p": ...}, ...]}
    raw_history = data.get('history') if isinstance(data, dict) else data
    points: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            if isinstance(item, dict) and 't' in item and 'p' in item:
                points.append({'t': item['t'], 'p': item['p']})

    if limit and limit > 0:
        points = points[-limit:]

    first_price = points[0]['p'] if points else None
    last_price = points[-1]['p'] if points else None
    total_move: Any = None
    if first_price is not None and last_price is not None:
        try:
            total_move = round(float(last_price) - float(first_price), 6)
        except (TypeError, ValueError):
            total_move = None

    return {
        'market_id': market_id,
        'token_id': token_id,
        'interval': interval,
        'points': len(points),
        'series': points,
        'first_price': first_price,
        'last_price': last_price,
        'total_move': total_move,
    }


@market.command('history')
@click.option('--market-id', required=True, help='Polymarket market ID.')
@click.option(
    '--interval',
    type=click.Choice(['1d', '6h', '1h']),
    default='1d',
    show_default=True,
    help='Candle interval.',
)
@click.option(
    '--limit',
    default=None,
    type=int,
    help='Take only the last N price points.',
)
@click.option('--json', 'as_json', is_flag=True, default=False)
def market_history(
    market_id: str,
    interval: str,
    limit: int | None,
    as_json: bool,
) -> None:
    """Fetch a market's price history from the Polymarket Gamma API (Polymarket only)."""
    try:
        result = asyncio.run(_polymarket_price_history(market_id, interval, limit))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f'Failed to fetch price history: {exc}') from exc

    if as_json:
        click.echo(json.dumps(result))
        return

    click.echo(f'\nPrice History — market {market_id} ({interval})')
    click.echo('=' * 60)
    click.echo(f'Points:      {result["points"]}')
    click.echo(f'First price: {result["first_price"]}')
    click.echo(f'Last price:  {result["last_price"]}')
    click.echo(f'Total move:  {result["total_move"]}')
    if result['series']:
        click.echo('\nLast 5 points:')
        for pt in result['series'][-5:]:
            click.echo(f'  t={pt["t"]}  p={pt["p"]}')
    click.echo()
