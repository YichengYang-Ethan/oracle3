"""Replay DataSource for PredictionMarketBench Kalshi episodes.

Reads orderbook.parquet + trades.parquet from an episode directory and
emits OrderBookEvent / PriceChangeEvent in chronological order, giving
strategies realistic order-book depth and trade-derived price ticks.

Episode layout (from github.com/Oddpool/PredictionMarketBench):
    episodes/{id}/
    ├── metadata.json
    ├── orderbook.parquet   # columns: ts, sequence_id, ticker, yes_bids, no_bids
    ├── trades.parquet      # columns: ts, trade_id, ticker, side, taker_side, price_cents, count
    └── settlement.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.ticker.ticker import KalshiTicker

from ..data_source import DataSource

logger = logging.getLogger(__name__)


class KalshiReplayDataSource(DataSource):
    """Replays a PredictionMarketBench episode as oracle3 events."""

    def __init__(self, episode_dir: str, *, max_events: int | None = None) -> None:
        self.episode_dir = Path(episode_dir)
        self.max_events = max_events
        self._tickers: dict[str, KalshiTicker] = {}
        self.events: list[Event] = []
        self.index = 0
        self._load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        meta_path = self.episode_dir / 'metadata.json'
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            episode_id = meta.get('episode_id', '')
            for t in meta.get('tickers', []):
                self._get_ticker(t, episode_id)

        events: list[tuple[float, Event]] = []

        # --- orderbook snapshots → OrderBookEvent ---
        ob_path = self.episode_dir / 'orderbook.parquet'
        if ob_path.exists():
            df = pd.read_parquet(ob_path)
            for row in df.itertuples(index=False):
                ts_val = self._to_timestamp(row.ts)
                ticker = self._get_ticker(row.ticker)

                # Parse yes_bids to get bid side
                yes_bids = self._parse_levels(row.yes_bids)
                # no_bids represent ask side (complement)
                no_bids = self._parse_levels(row.no_bids)

                bid_vol = sum(lv['size'] for lv in yes_bids)
                ask_vol = sum(lv['size'] for lv in no_bids)

                best_bid_price = yes_bids[0]['price_cents'] / 100.0 if yes_bids else 0.0
                best_ask_price = (100 - no_bids[0]['price_cents']) / 100.0 if no_bids else 1.0

                # Emit OB event with bid-side info
                if bid_vol > 0 or ask_vol > 0:
                    ev = OrderBookEvent(
                        ticker=ticker,
                        price=Decimal(str(best_bid_price)),
                        size=Decimal(str(bid_vol)),
                        size_delta=Decimal(str(ask_vol)),
                        side='bid',
                    )
                    ev.timestamp = datetime.fromtimestamp(ts_val, tz=timezone.utc)
                    events.append((ts_val, ev))

        # --- trades → PriceChangeEvent ---
        trades_path = self.episode_dir / 'trades.parquet'
        if trades_path.exists():
            df = pd.read_parquet(trades_path)
            for row in df.itertuples(index=False):
                ts_val = self._to_timestamp(row.ts)
                ticker = self._get_ticker(row.ticker)
                price = Decimal(str(row.price_cents)) / Decimal('100')

                ev = PriceChangeEvent(
                    ticker=ticker,
                    price=price,
                    timestamp=datetime.fromtimestamp(ts_val, tz=timezone.utc),
                )
                events.append((ts_val, ev))

        # Sort by timestamp
        events.sort(key=lambda x: x[0])

        if self.max_events:
            events = events[:self.max_events]

        self.events = [ev for _, ev in events]
        logger.info(
            'KalshiReplay loaded: %d events from %s (%d tickers)',
            len(self.events), self.episode_dir.name, len(self._tickers),
        )

    def _get_ticker(self, symbol: str, episode_id: str = '') -> KalshiTicker:
        if symbol in self._tickers:
            return self._tickers[symbol]

        # Parse Kalshi ticker: KXBTCD-26JAN2017-T98249.99
        parts = symbol.split('-')
        series = parts[0] if parts else symbol
        event = '-'.join(parts[:2]) if len(parts) >= 2 else symbol

        ticker = KalshiTicker(
            symbol=symbol,
            name=symbol,
            market_ticker=symbol,
            event_ticker=event,
            series_ticker=series,
        )
        self._tickers[symbol] = ticker
        return ticker

    @staticmethod
    def _parse_levels(raw: Any) -> list[dict]:
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
        if isinstance(raw, list):
            return raw
        return []

    @staticmethod
    def _to_timestamp(val: Any) -> float:
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, datetime):
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)
            return val.timestamp()
        if isinstance(val, pd.Timestamp):
            if val.tzinfo is None:
                val = val.tz_localize('UTC')
            return val.timestamp()
        if isinstance(val, str):
            dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        return 0.0

    # ------------------------------------------------------------------
    # DataSource interface
    # ------------------------------------------------------------------

    async def get_next_event(self) -> Event | None:
        if self.index < len(self.events):
            event = self.events[self.index]
            self.index += 1
            return event
        return None

    def get_tickers(self) -> list[KalshiTicker]:
        return list(self._tickers.values())
