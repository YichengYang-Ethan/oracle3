"""API fetchers for Polymarket and Kalshi public endpoints."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from ._cache import TTLCache
from ._types import NormalizedMarket

logger = logging.getLogger(__name__)

GAMMA_API = 'https://gamma-api.polymarket.com'
KALSHI_API = 'https://api.elections.kalshi.com/trade-api/v2'

_DEFAULT_TIMEOUT = 30.0


def _parse_iso_date(s: str | None) -> datetime | None:
    """Parse an ISO 8601 date string to a datetime."""
    if not s:
        return None
    try:
        s = s.replace('Z', '+00:00')
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _parse_unix_ts(ts: int | str | None) -> datetime | None:
    """Parse a Unix timestamp (seconds) to a datetime."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def _parse_json_field(raw: object, default: list[object] | None = None) -> list[object]:
    """Parse a JSON field that may be a string or already a list."""
    if default is None:
        default = []
    if isinstance(raw, str):
        try:
            return json.loads(raw)  # type: ignore[return-value]
        except (json.JSONDecodeError, TypeError):
            return default
    if isinstance(raw, list):
        return raw
    return default


def _extract_tags(raw_tags: object) -> list[str]:
    """Extract tag labels from a raw tags field."""
    if not isinstance(raw_tags, list):
        return []
    return [
        t.get('label', t) if isinstance(t, dict) else str(t)
        for t in raw_tags
    ]


def _normalize_poly_market(
    mkt: dict[str, object],
    event_id: str,
    event_title: str,
    event_tags: list[str],
    event_end_date: str | None,
) -> NormalizedMarket:
    """Convert a raw Polymarket market dict to a NormalizedMarket."""
    question = str(mkt.get('question', event_title))
    end_date = _parse_iso_date(
        str(mkt.get('endDate', '')) or event_end_date
    )

    token_ids = _parse_json_field(mkt.get('clobTokenIds', '[]'))
    prices = _parse_json_field(mkt.get('outcomePrices', '[]'))
    yes_price = float(prices[0]) if prices else None  # type: ignore[arg-type]

    mkt_tags = _extract_tags(mkt.get('tags', []))
    all_tags = event_tags + mkt_tags

    return NormalizedMarket(
        platform='polymarket',
        event_id=event_id,
        market_id=str(mkt.get('id', '')),
        title=question,
        end_date=end_date,
        tags=all_tags,
        resolution_source=str(mkt.get('resolutionSource', '')),
        extra={
            'token_id': token_ids[0] if token_ids else '',
            'no_token_id': token_ids[1] if len(token_ids) > 1 else '',
            'yes_price': yes_price,
            'volume': float(str(mkt.get('volume', 0) or 0)),
            'description': str(mkt.get('description', '')),
        },
    )


def _normalize_kalshi_market(
    mkt: dict[str, object],
    ev_title: str,
    series_ticker: str,
    ev_category: str,
    ev_event_ticker: str,
) -> NormalizedMarket:
    """Convert a raw Kalshi market dict to a NormalizedMarket."""
    title = str(mkt.get('title', '')) or ev_title
    end_date = _parse_iso_date(
        str(mkt.get('expiration_time', ''))
        or str(mkt.get('close_time', ''))
    )

    # Kalshi prices are in cents (1-99)
    yes_bid = mkt.get('yes_bid')
    yes_ask = mkt.get('yes_ask')
    yes_price = None
    if yes_bid is not None and yes_ask is not None:
        yes_price = (float(yes_bid) + float(yes_ask)) / 2.0 / 100.0  # type: ignore[arg-type]
    elif yes_ask is not None:
        yes_price = float(yes_ask) / 100.0  # type: ignore[arg-type]

    mkt_series = str(mkt.get('series_ticker', '')) or series_ticker
    event_ticker = str(mkt.get('event_ticker', '')) or ev_event_ticker

    tags: list[str] = []
    if ev_category:
        tags.append(ev_category)

    return NormalizedMarket(
        platform='kalshi',
        event_id=event_ticker,
        market_id=str(mkt.get('ticker', '')),
        title=title,
        end_date=end_date,
        tags=tags,
        series_ticker=mkt_series,
        resolution_source=str(mkt.get('settlement_source', '')),
        extra={
            'market_ticker': str(mkt.get('ticker', '')),
            'event_ticker': event_ticker,
            'yes_price': yes_price,
            'volume': mkt.get('volume', 0),
            'rules': str(
                mkt.get('rules_primary', mkt.get('settlement_timer_duration', ''))
            ),
        },
    )


async def fetch_polymarket_events(
    cache: TTLCache | None = None,
    max_events: int = 500,
) -> list[NormalizedMarket]:
    """Fetch active Polymarket events and normalize their markets."""
    cache_key = f'polymarket_events_{max_events}'
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

    markets: list[NormalizedMarket] = []
    offset = 0
    page_size = 100

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        while offset < max_events:
            try:
                resp = await client.get(
                    f'{GAMMA_API}/events',
                    params={
                        'active': 'true',
                        'closed': 'false',
                        'limit': min(page_size, max_events - offset),
                        'offset': offset,
                    },
                )
                resp.raise_for_status()
                events = resp.json()
                if not events:
                    break

                for ev in events:
                    event_id = str(ev.get('id', ''))
                    event_title = str(ev.get('title', ''))
                    event_tags = _extract_tags(ev.get('tags', []))
                    event_end = ev.get('endDate')

                    for mkt in ev.get('markets', []):
                        markets.append(_normalize_poly_market(
                            mkt, event_id, event_title, event_tags,
                            str(event_end) if event_end else None,
                        ))

                offset += len(events)
                if len(events) < page_size:
                    break
            except httpx.HTTPError as exc:
                logger.warning('Polymarket fetch offset=%d: %s', offset, exc)
                break

    logger.info('Fetched %d Polymarket markets', len(markets))
    if cache is not None:
        cache.set(cache_key, markets)
    return markets


def _process_kalshi_events_page(
    events: list[dict[str, object]],
) -> list[NormalizedMarket]:
    """Convert a page of raw Kalshi events to NormalizedMarkets."""
    markets: list[NormalizedMarket] = []
    for ev in events:
        ev_title = str(ev.get('title', ''))
        series = str(ev.get('series_ticker', ''))
        category = str(ev.get('category', ''))
        ev_ticker = str(ev.get('event_ticker', ''))

        raw_markets = ev.get('markets', [])
        if not isinstance(raw_markets, list):
            continue
        for mkt in raw_markets:
            if not isinstance(mkt, dict):
                continue
            markets.append(_normalize_kalshi_market(
                mkt, ev_title, series, category, ev_ticker,
            ))
    return markets


async def fetch_kalshi_events(
    cache: TTLCache | None = None,
    max_pages: int = 15,
) -> list[NormalizedMarket]:
    """Fetch active Kalshi events and normalize their markets."""
    cache_key = f'kalshi_events_{max_pages}'
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

    markets: list[NormalizedMarket] = []
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        for _ in range(max_pages):
            try:
                params: dict[str, str | int] = {
                    'status': 'open',
                    'limit': 100,
                    'with_nested_markets': 'true',
                }
                if cursor:
                    params['cursor'] = cursor

                resp = await client.get(
                    f'{KALSHI_API}/events',
                    params=params,
                    headers={'Accept': 'application/json'},
                )
                resp.raise_for_status()
                data = resp.json()
                events = data.get('events', [])
                if not events:
                    break

                markets.extend(_process_kalshi_events_page(events))

                cursor = data.get('cursor')
                if not cursor:
                    break
            except httpx.HTTPError as exc:
                logger.warning('Kalshi fetch page: %s', exc)
                break

    logger.info('Fetched %d Kalshi markets', len(markets))
    if cache is not None:
        cache.set(cache_key, markets)
    return markets
