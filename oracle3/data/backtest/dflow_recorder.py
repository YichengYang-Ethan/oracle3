"""Record live DFlow REST API data to parquet for backtesting.

Uses REST polling (like DFlowDataSource) instead of WebSocket since the
dev WS endpoint does not push data reliably.

Usage:
    python -m oracle3.data.backtest.dflow_recorder --duration 3600 --output data/episodes/my_session
    python -m oracle3.data.backtest.dflow_recorder --duration 300 --interval 5 --output data/episodes/quick

Produces:
    {output}/dflow_events.parquet   — all events (price + orderbook)
    {output}/metadata.json          — session metadata
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

_API_BASE = 'https://dev-prediction-markets-api.dflow.net'


def _safe_float(v: Any) -> float:
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _normalize_price(p: float) -> float:
    """Normalize price to 0-1 range (DFlow may use 0-100)."""
    if p > 1:
        return p / 100.0
    return p


class DFlowRecorder:
    """Records DFlow market data via REST polling to parquet files."""

    def __init__(
        self,
        output_dir: str,
        api_base: str = _API_BASE,
        polling_interval: float = 10.0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.api_base = api_base
        self.polling_interval = polling_interval
        self._rows: list[dict] = []
        self._tickers_seen: set[str] = set()
        self._last_prices: dict[str, tuple[float, float]] = {}

    async def record(self, duration_seconds: float) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        start = time.time()
        poll_count = 0
        logger.info(
            'Recording DFlow REST data for %.0fs (interval=%.0fs) → %s',
            duration_seconds, self.polling_interval, self.output_dir,
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            while time.time() - start < duration_seconds:
                try:
                    markets = await self._fetch_markets(client)
                    poll_count += 1
                    now = datetime.now(tz=timezone.utc)

                    new_rows = 0
                    for market in markets:
                        new_rows += self._process_market(market, now)

                    logger.info(
                        'Poll #%d: %d markets, %d new rows (total %d)',
                        poll_count, len(markets), new_rows, len(self._rows),
                    )
                except httpx.HTTPError as e:
                    logger.warning('HTTP error during poll: %s', e)
                except Exception:
                    logger.warning('Error during poll', exc_info=True)

                elapsed = time.time() - start
                remaining = duration_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(min(self.polling_interval, remaining))

        self._save()

    async def _fetch_markets(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch all active markets with nested market data."""
        resp = await client.get(
            f'{self.api_base}/api/v1/events',
            params={'status': 'active', 'withNestedMarkets': 'true'},
        )
        resp.raise_for_status()
        data = resp.json()

        events = data if isinstance(data, list) else data.get('events', data.get('data', []))

        markets: list[dict[str, Any]] = []
        for event in events:
            event_ticker = event.get('ticker', event.get('eventTicker', ''))
            series_ticker = event.get('seriesTicker', event.get('series_ticker', ''))
            for mkt in event.get('markets', []):
                mkt['_event_ticker'] = mkt.get('eventTicker', event_ticker)
                mkt['_series_ticker'] = series_ticker
                markets.append(mkt)
        return markets

    def _process_market(self, market: dict[str, Any], now: datetime) -> int:
        """Extract orderbook rows from a market snapshot. Returns count of new rows."""
        market_ticker = market.get('ticker', market.get('marketTicker', ''))
        if not market_ticker:
            return 0

        self._tickers_seen.add(market_ticker)

        yes_bid = _safe_float(market.get('yesBid', market.get('yes_bid', 0)))
        yes_ask = _safe_float(market.get('yesAsk', market.get('yes_ask', 0)))
        last_price = _safe_float(market.get('lastPrice', market.get('last_price', 0)))

        # Synthesize bid/ask from lastPrice if needed
        if yes_bid == 0 and yes_ask == 0 and last_price > 0:
            yes_bid = max(0.01, last_price - 0.01)
            yes_ask = min(0.99, last_price + 0.01)

        prev_bid, prev_ask = self._last_prices.get(market_ticker, (0, 0))
        new_rows = 0

        # Record bid if changed
        if yes_bid > 0 and yes_bid != prev_bid:
            p = _normalize_price(yes_bid)
            if 0 < p < 1:
                self._rows.append({
                    'ts': now, 'ticker': market_ticker,
                    'event_type': 'orderbook', 'price': p,
                    'size': 100.0, 'side': 'bid',
                })
                new_rows += 1

        # Record ask if changed
        if yes_ask > 0 and yes_ask != prev_ask:
            p = _normalize_price(yes_ask)
            if 0 < p < 1:
                self._rows.append({
                    'ts': now, 'ticker': market_ticker,
                    'event_type': 'orderbook', 'price': p,
                    'size': 100.0, 'side': 'ask',
                })
                new_rows += 1

        # Record price event from lastPrice
        if last_price > 0:
            p = _normalize_price(last_price)
            if 0 < p < 1:
                self._rows.append({
                    'ts': now, 'ticker': market_ticker,
                    'event_type': 'price', 'price': p,
                    'size': 0.0, 'side': '',
                })
                new_rows += 1

        self._last_prices[market_ticker] = (yes_bid, yes_ask)
        return new_rows

    def _save(self) -> None:
        if not self._rows:
            logger.warning('No events recorded')
            return

        df = pd.DataFrame(self._rows)
        parquet_path = self.output_dir / 'dflow_events.parquet'
        df.to_parquet(parquet_path, index=False)

        meta = {
            'episode_id': self.output_dir.name,
            'source': 'dflow_rest',
            'api_base': self.api_base,
            'tickers': sorted(self._tickers_seen),
            'start_ts': df['ts'].min().isoformat(),
            'end_ts': df['ts'].max().isoformat(),
            'total_events': len(df),
            'total_tickers': len(self._tickers_seen),
        }
        meta_path = self.output_dir / 'metadata.json'
        meta_path.write_text(json.dumps(meta, indent=2, default=str))

        logger.info(
            'Saved %d events (%d tickers) to %s',
            len(df), len(self._tickers_seen), parquet_path,
        )


async def _main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='Record DFlow market data via REST polling')
    parser.add_argument('--duration', type=float, default=300, help='Seconds to record')
    parser.add_argument('--interval', type=float, default=10, help='Polling interval in seconds')
    parser.add_argument('--output', default='data/episodes/dflow_session', help='Output dir')
    args = parser.parse_args()

    recorder = DFlowRecorder(args.output, polling_interval=args.interval)
    await recorder.record(args.duration)


if __name__ == '__main__':
    asyncio.run(_main())
