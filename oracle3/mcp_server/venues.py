"""Read-only access to public Kalshi and Polymarket market data.

Only unauthenticated public endpoints are used; nothing in this module can
place, change or cancel an order.

Kalshi quotes are read from the ``*_dollars`` fields and the fixed-point order
book (``orderbook_fp``). Polymarket quotes are read from the CLOB order books
of the market's two outcome tokens. For Polymarket, "yes" refers to the first
outcome token and "no" to the second; for head-to-head markets the outcome
names are returned in ``outcomes``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from oracle3.arbitrage import Quote
from oracle3.fees import (
    FeeSchedule,
    KalshiSchedule,
    PolymarketSchedule,
    UnsupportedFeeSchedule,
)

KALSHI_API = 'https://api.elections.kalshi.com/trade-api/v2'
GAMMA_API = 'https://gamma-api.polymarket.com'
CLOB_API = 'https://clob.polymarket.com'

VENUES = ('kalshi', 'polymarket')

_transport: httpx.AsyncBaseTransport | None = None
_series_cache: dict[str, dict[str, Any]] = {}


class VenueError(RuntimeError):
    """A venue API call failed or returned something unusable."""


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route all requests through ``transport`` (tests use a mock transport)."""
    global _transport
    _transport = transport
    _series_cache.clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=20.0,
        transport=_transport,
        headers={'User-Agent': 'oracle3-mcp (+https://github.com/YichengYang-Ethan)'},
        follow_redirects=True,
    )


async def _get(
    client: httpx.AsyncClient, url: str, params: Mapping[str, Any] | None = None
) -> Any:
    try:
        resp = await client.get(url, params=dict(params or {}))
    except httpx.HTTPError as exc:
        raise VenueError(f'request to {url} failed: {exc}') from exc
    if resp.status_code != 200:
        raise VenueError(f'{url} returned HTTP {resp.status_code}: {resp.text[:200]}')
    return resp.json()


def _num(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bid(value: Any) -> float | None:
    x = _num(value)
    return x if x is not None and 0.0 < x < 1.0 else None


def _ask(value: Any) -> float | None:
    x = _num(value)
    return x if x is not None and 0.0 < x < 1.0 else None


def check_venue(venue: str) -> str:
    key = venue.strip().lower()
    if key not in VENUES:
        raise ValueError(f'unknown venue {venue!r}; expected one of {VENUES}')
    return key


# ── Kalshi ───────────────────────────────────────────────────────────────


def _kalshi_price(m: Mapping[str, Any], field: str) -> Any:
    dollars = m.get(f'{field}_dollars')
    if dollars not in (None, ''):
        return dollars
    cents = m.get(field)
    return None if cents is None else float(cents) / 100.0


def _kalshi_market(m: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'venue': 'kalshi',
        'market_id': m.get('ticker', ''),
        'title': m.get('title', ''),
        'event_ticker': m.get('event_ticker', ''),
        'yes_bid': _bid(_kalshi_price(m, 'yes_bid')),
        'yes_ask': _ask(_kalshi_price(m, 'yes_ask')),
        'no_bid': _bid(_kalshi_price(m, 'no_bid')),
        'no_ask': _ask(_kalshi_price(m, 'no_ask')),
        'volume': _num(m.get('volume_fp'))
        if m.get('volume_fp') is not None
        else _num(m.get('volume')),
        'close_time': m.get('close_time', ''),
        'status': m.get('status', ''),
    }


async def kalshi_search(
    query: str = '', limit: int = 20, series_ticker: str = ''
) -> list[dict[str, Any]]:
    async with _client() as client:
        if series_ticker:
            data = await _get(
                client,
                f'{KALSHI_API}/markets',
                {
                    'status': 'open',
                    'series_ticker': series_ticker,
                    'limit': min(max(limit, 1), 1000),
                },
            )
            return [_kalshi_market(m) for m in data.get('markets', [])][:limit]

        needle = query.strip().lower()
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(5):
            params: dict[str, Any] = {
                'status': 'open',
                'limit': 200,
                'with_nested_markets': 'true',
            }
            if cursor:
                params['cursor'] = cursor
            data = await _get(client, f'{KALSHI_API}/events', params)
            for event in data.get('events', []):
                event_title = str(event.get('title', ''))
                for m in event.get('markets') or []:
                    text = f"{event_title} {m.get('title', '')} {m.get('ticker', '')}".lower()
                    if not needle or needle in text:
                        row = _kalshi_market(m)
                        row['event_title'] = event_title
                        row['series_ticker'] = event.get('series_ticker', '')
                        found.append(row)
                        if len(found) >= limit:
                            return found
            cursor = data.get('cursor')
            if not cursor:
                break
        return found


async def kalshi_market(ticker: str) -> dict[str, Any]:
    async with _client() as client:
        data = await _get(client, f'{KALSHI_API}/markets/{ticker}')
    market = data.get('market')
    if not market:
        raise VenueError(f'Kalshi market {ticker!r} not found')
    row = _kalshi_market(market)
    row['rules_primary'] = market.get('rules_primary', '')
    row['rules_secondary'] = market.get('rules_secondary', '')
    return row


async def kalshi_series(event_ticker: str) -> dict[str, Any]:
    """Series object (with ``fee_type`` and ``fee_multiplier``) for an event."""
    async with _client() as client:
        event = (await _get(client, f'{KALSHI_API}/events/{event_ticker}')).get(
            'event'
        ) or {}
        series_ticker = event.get('series_ticker') or event_ticker.split('-')[0]
        if series_ticker not in _series_cache:
            series = (await _get(client, f'{KALSHI_API}/series/{series_ticker}')).get(
                'series'
            ) or {}
            _series_cache[series_ticker] = dict(series, ticker=series_ticker)
    return _series_cache[series_ticker]


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for level in raw or []:
        if isinstance(level, Mapping):
            price, size = _num(level.get('price')), _num(level.get('size'))
        else:
            price, size = _num(level[0]), _num(level[1])
        if price is not None and size is not None and size > 0:
            out.append((price, size))
    return out


async def kalshi_orderbook(ticker: str, depth: int = 10) -> dict[str, Any]:
    async with _client() as client:
        data = await _get(
            client, f'{KALSHI_API}/markets/{ticker}/orderbook', {'depth': depth}
        )
    if 'orderbook_fp' in data:
        book = data['orderbook_fp'] or {}
        yes_bids = _levels(book.get('yes_dollars'))
        no_bids = _levels(book.get('no_dollars'))
    else:
        book = data.get('orderbook') or {}
        yes_bids = [(p / 100.0, q) for p, q in _levels(book.get('yes'))]
        no_bids = [(p / 100.0, q) for p, q in _levels(book.get('no'))]
    yes_bids.sort(key=lambda lv: -lv[0])
    no_bids.sort(key=lambda lv: -lv[0])
    return {
        'venue': 'kalshi',
        'market_id': ticker,
        'yes': {
            'bids': yes_bids[:depth],
            'asks': [(round(1.0 - p, 6), q) for p, q in no_bids][:depth],
        },
        'no': {
            'bids': no_bids[:depth],
            'asks': [(round(1.0 - p, 6), q) for p, q in yes_bids][:depth],
        },
        'note': 'Kalshi books hold bids only; YES asks are implied by NO bids and vice versa.',
    }


# ── Polymarket ───────────────────────────────────────────────────────────


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return list(value or [])


def polymarket_schedule(market: Mapping[str, Any]) -> tuple[FeeSchedule | None, str]:
    try:
        return PolymarketSchedule.from_gamma_market(market), ''
    except UnsupportedFeeSchedule as exc:
        return None, str(exc)


def _poly_market(m: Mapping[str, Any]) -> dict[str, Any]:
    tokens = [str(t) for t in _json_list(m.get('clobTokenIds'))]
    events = m.get('events') or []
    schedule, problem = polymarket_schedule(m)
    return {
        'venue': 'polymarket',
        'market_id': str(m.get('id', '')),
        'title': m.get('question', ''),
        'event_title': events[0].get('title', '')
        if events and isinstance(events[0], Mapping)
        else '',
        'outcomes': _json_list(m.get('outcomes')),
        'yes_token_id': tokens[0] if tokens else '',
        'no_token_id': tokens[1] if len(tokens) > 1 else '',
        'yes_bid': _bid(m.get('bestBid')),
        'yes_ask': _ask(m.get('bestAsk')),
        'volume': _num(m.get('volumeNum'))
        if m.get('volumeNum') is not None
        else _num(m.get('volume')),
        'end_date': m.get('endDate', ''),
        'neg_risk': bool(m.get('negRisk', False)),
        'fee_schedule': schedule.describe() if schedule else {'error': problem},
    }


async def polymarket_search(query: str = '', limit: int = 20) -> list[dict[str, Any]]:
    async with _client() as client:
        if not query.strip():
            data = await _get(
                client,
                f'{GAMMA_API}/markets',
                {
                    'active': 'true',
                    'closed': 'false',
                    'limit': min(max(limit, 1), 500),
                    'order': 'volume24hr',
                    'ascending': 'false',
                },
            )
            return [_poly_market(m) for m in data][:limit]
        data = await _get(
            client, f'{GAMMA_API}/public-search', {'q': query, 'limit_per_type': limit}
        )
    found: list[dict[str, Any]] = []
    for event in data.get('events') or []:
        for m in event.get('markets') or []:
            if m.get('closed'):
                continue
            row = _poly_market(m)
            row['event_title'] = row['event_title'] or event.get('title', '')
            row['event_id'] = str(event.get('id', ''))
            found.append(row)
            if len(found) >= limit:
                return found
    return found


async def _polymarket_raw(market_id: str) -> dict[str, Any]:
    async with _client() as client:
        try:
            data = await _get(client, f'{GAMMA_API}/markets/{market_id}')
        except VenueError:
            data = await _get(client, f'{GAMMA_API}/markets', {'id': market_id})
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, Mapping) or not data:
        raise VenueError(f'Polymarket market {market_id!r} not found')
    return dict(data)


async def polymarket_market(market_id: str) -> dict[str, Any]:
    raw = await _polymarket_raw(market_id)
    row = _poly_market(raw)
    row['rules'] = raw.get('description', '')
    row['resolution_source'] = raw.get('resolutionSource', '')
    return row


async def _clob_book(
    client: httpx.AsyncClient, token_id: str, depth: int
) -> dict[str, Any]:
    data = await _get(client, f'{CLOB_API}/book', {'token_id': token_id})
    bids = sorted(_levels(data.get('bids')), key=lambda lv: -lv[0])[:depth]
    asks = sorted(_levels(data.get('asks')), key=lambda lv: lv[0])[:depth]
    return {'bids': bids, 'asks': asks}


async def polymarket_orderbook(market_id: str, depth: int = 10) -> dict[str, Any]:
    market = await polymarket_market(market_id)
    if not market['yes_token_id'] or not market['no_token_id']:
        raise VenueError(f'Polymarket market {market_id!r} has no CLOB tokens')
    async with _client() as client:
        yes = await _clob_book(client, market['yes_token_id'], depth)
        no = await _clob_book(client, market['no_token_id'], depth)
    return {
        'venue': 'polymarket',
        'market_id': market_id,
        'outcomes': market['outcomes'],
        'yes': yes,
        'no': no,
        'note': "'yes' is the first outcome token and 'no' the second.",
    }


# ── Unified ──────────────────────────────────────────────────────────────


async def search(
    venue: str, query: str = '', limit: int = 20, series_ticker: str = ''
) -> list[dict[str, Any]]:
    if check_venue(venue) == 'kalshi':
        return await kalshi_search(query, limit, series_ticker)
    return await polymarket_search(query, limit)


async def market(venue: str, market_id: str) -> dict[str, Any]:
    if check_venue(venue) == 'kalshi':
        return await kalshi_market(market_id)
    return await polymarket_market(market_id)


async def orderbook(venue: str, market_id: str, depth: int = 10) -> dict[str, Any]:
    if check_venue(venue) == 'kalshi':
        return await kalshi_orderbook(market_id, depth)
    return await polymarket_orderbook(market_id, depth)


async def fee_schedule(venue: str, market_id: str) -> FeeSchedule:
    """Fee schedule the venue reports for this market."""
    if check_venue(venue) == 'kalshi':
        info = await kalshi_market(market_id)
        series = await kalshi_series(info['event_ticker'])
        return KalshiSchedule.from_series(series)
    raw = await _polymarket_raw(market_id)
    schedule, problem = polymarket_schedule(raw)
    if schedule is None:
        raise UnsupportedFeeSchedule(problem)
    return schedule


async def quote(venue: str, market_id: str) -> Quote:
    """Best bid and ask on both sides, with the market's own fee schedule."""
    key = check_venue(venue)
    book = await orderbook(key, market_id, depth=1)
    info = await market(key, market_id)

    def best(levels: list[tuple[float, float]]) -> float | None:
        return levels[0][0] if levels else None

    return Quote(
        market_id=market_id,
        venue=key,
        yes_bid=best(book['yes']['bids']),
        yes_ask=best(book['yes']['asks']),
        no_bid=best(book['no']['bids']),
        no_ask=best(book['no']['asks']),
        schedule=await fee_schedule(key, market_id),
        title=str(info.get('title', '')),
    )
