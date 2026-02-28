"""Polymarket CLOB API order flow monitor.

Detects large orders, liquidity shifts, spread changes, and volume spikes
by polling the Polymarket CLOB REST API and comparing successive order book
snapshots.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from oracle3.data.base_fetcher import BaseFetcher
from oracle3.ticker.ticker import PolyMarketTicker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OrderFlowSignal:
    """Represents a detected order flow anomaly."""

    ticker: PolyMarketTicker
    signal_type: (
        str  # "large_order", "liquidity_shift", "spread_change", "volume_spike"
    )
    direction: str  # "bullish", "bearish", "neutral"
    magnitude: float  # 0.0-1.0 normalized strength
    details: str  # human-readable description
    timestamp: datetime
    raw_data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_decimal(value: Any, default: Decimal = Decimal('0')) -> Decimal:
    """Convert an arbitrary value to Decimal, returning *default* on failure."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _book_depth(levels: list[dict[str, Any]]) -> Decimal:
    """Sum the USD-equivalent size across all price levels."""
    total = Decimal('0')
    for level in levels:
        price = _safe_decimal(level.get('price'))
        size = _safe_decimal(level.get('size'))
        total += price * size
    return total


def _best_price(levels: list[dict[str, Any]], side: str) -> Decimal | None:
    """Return the best bid or ask price from a list of levels.

    *side* must be ``"bid"`` (highest price) or ``"ask"`` (lowest price).
    """
    if not levels:
        return None
    prices = [_safe_decimal(lv.get('price')) for lv in levels]
    if side == 'bid':
        return max(prices)
    return min(prices)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class PolymarketOrderFlowMonitor(BaseFetcher):
    """Monitors Polymarket CLOB API for order flow signals."""

    CLOB_BASE: str = 'https://clob.polymarket.com'

    def __init__(
        self,
        large_order_threshold: Decimal = Decimal('500'),
        liquidity_change_threshold: float = 0.20,
        spread_change_threshold: float = 0.03,
        volume_spike_multiplier: float = 3.0,
        snapshot_interval: int = 60,
        min_delay: float = 1.0,
        max_delay: float = 2.0,
    ) -> None:
        super().__init__(min_delay=min_delay, max_delay=max_delay)

        # Thresholds
        self.large_order_threshold = large_order_threshold
        self.liquidity_change_threshold = liquidity_change_threshold
        self.spread_change_threshold = spread_change_threshold
        self.volume_spike_multiplier = volume_spike_multiplier
        self.snapshot_interval = snapshot_interval

        # Internal state
        self._prev_snapshots: dict[str, dict[str, Any]] = {}
        self._trade_history: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=200)
        )
        self._volume_ma: dict[str, float] = {}

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def fetch_order_book(self, token_id: str) -> dict[str, Any]:
        """GET /book for a given token_id.  Returns parsed JSON."""
        url = f'{self.CLOB_BASE}/book?token_id={token_id}'
        try:
            response = await self.make_request(url)
            self.validate_response(response, context=f'fetch_order_book({token_id})')
            return self.safe_json_parse(
                response, context=f'fetch_order_book({token_id})'
            )
        except Exception:
            logger.warning('Failed to fetch order book for %s', token_id, exc_info=True)
            return {'bids': [], 'asks': []}

    async def fetch_market_trades(
        self, token_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """GET /trades for a given asset_id.  Returns list of trade dicts."""
        url = f'{self.CLOB_BASE}/trades?asset_id={token_id}&limit={limit}'
        try:
            response = await self.make_request(url)
            self.validate_response(response, context=f'fetch_market_trades({token_id})')
            data = self.safe_json_parse(
                response, context=f'fetch_market_trades({token_id})'
            )
            if isinstance(data, list):
                return data
            # Some endpoints wrap results in a top-level key.
            return data.get('trades', data.get('data', []))
        except Exception:
            logger.warning('Failed to fetch trades for %s', token_id, exc_info=True)
            return []

    async def fetch_midpoint(self, token_id: str) -> Decimal:
        """GET /midpoint for a given token_id.  Returns midpoint price."""
        url = f'{self.CLOB_BASE}/midpoint?token_id={token_id}'
        try:
            response = await self.make_request(url)
            self.validate_response(response, context=f'fetch_midpoint({token_id})')
            data = self.safe_json_parse(response, context=f'fetch_midpoint({token_id})')
            if isinstance(data, dict):
                return _safe_decimal(data.get('mid', data.get('midpoint', '0')))
            return _safe_decimal(data)
        except Exception:
            logger.warning('Failed to fetch midpoint for %s', token_id, exc_info=True)
            return Decimal('0')

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def snapshot_market(self, ticker: PolyMarketTicker) -> dict[str, Any]:
        """Fetch order book, trades, and midpoint concurrently."""
        token_id = ticker.token_id
        book_coro = self.fetch_order_book(token_id)
        trades_coro = self.fetch_market_trades(token_id)
        midpoint_coro = self.fetch_midpoint(token_id)

        book, trades, midpoint = await asyncio.gather(
            book_coro, trades_coro, midpoint_coro, return_exceptions=True
        )

        # Gracefully handle individual failures.
        if isinstance(book, BaseException):
            logger.warning('Order book fetch raised: %s', book)
            book = {'bids': [], 'asks': []}
        if isinstance(trades, BaseException):
            logger.warning('Trades fetch raised: %s', trades)
            trades = []
        if isinstance(midpoint, BaseException):
            logger.warning('Midpoint fetch raised: %s', midpoint)
            midpoint = Decimal('0')

        now = datetime.now(tz=timezone.utc)
        return {
            'token_id': token_id,
            'timestamp': now,
            'book': book,
            'trades': trades,
            'midpoint': midpoint,
            'bids': book.get('bids', []) if isinstance(book, dict) else [],
            'asks': book.get('asks', []) if isinstance(book, dict) else [],
        }

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    async def detect_signals(self, ticker: PolyMarketTicker) -> list[OrderFlowSignal]:
        """Compare current snapshot with previous and detect anomalies."""
        token_id = ticker.token_id
        snapshot = await self.snapshot_market(ticker)
        signals: list[OrderFlowSignal] = []
        now = snapshot['timestamp']

        bids: list[dict[str, Any]] = snapshot['bids']
        asks: list[dict[str, Any]] = snapshot['asks']
        trades: list[dict[str, Any]] = snapshot['trades']
        midpoint: Decimal = snapshot['midpoint']

        # Append new trades to rolling history.
        self._trade_history[token_id].extend(trades)

        prev = self._prev_snapshots.get(token_id)

        # --- 1. Large orders ---------------------------------------------------
        for trade in trades:
            price = _safe_decimal(trade.get('price'))
            size = _safe_decimal(trade.get('size', trade.get('amount', '0')))
            usd_value = price * size
            if usd_value >= self.large_order_threshold:
                side = str(trade.get('side', 'unknown')).lower()
                if side in ('buy', 'b'):
                    direction = 'bullish'
                elif side in ('sell', 's'):
                    direction = 'bearish'
                else:
                    direction = 'bullish' if price >= midpoint else 'bearish'

                magnitude = _clamp(float(usd_value / self.large_order_threshold) / 10.0)
                signals.append(
                    OrderFlowSignal(
                        ticker=ticker,
                        signal_type='large_order',
                        direction=direction,
                        magnitude=magnitude,
                        details=(
                            f'Large {direction} order: {size} @ {price} '
                            f'(${usd_value:.2f}) on {ticker.symbol}'
                        ),
                        timestamp=now,
                        raw_data=trade,
                    )
                )

        # The remaining detections require a previous snapshot.
        if prev is not None:
            prev_bids: list[dict[str, Any]] = prev.get('bids', [])
            prev_asks: list[dict[str, Any]] = prev.get('asks', [])

            # --- 2. Liquidity shifts -------------------------------------------
            cur_bid_depth = _book_depth(bids)
            cur_ask_depth = _book_depth(asks)
            prev_bid_depth = _book_depth(prev_bids)
            prev_ask_depth = _book_depth(prev_asks)

            for label, cur_d, prev_d, bull_dir in [
                ('bid', cur_bid_depth, prev_bid_depth, 'bullish'),
                ('ask', cur_ask_depth, prev_ask_depth, 'bearish'),
            ]:
                if prev_d > 0:
                    change_ratio = float((cur_d - prev_d) / prev_d)
                    if abs(change_ratio) >= self.liquidity_change_threshold:
                        direction = (
                            bull_dir
                            if change_ratio > 0
                            else ('bearish' if label == 'bid' else 'bullish')
                        )
                        magnitude = _clamp(abs(change_ratio))
                        signals.append(
                            OrderFlowSignal(
                                ticker=ticker,
                                signal_type='liquidity_shift',
                                direction=direction,
                                magnitude=magnitude,
                                details=(
                                    f'{label.capitalize()} depth changed '
                                    f'{change_ratio:+.1%} on {ticker.symbol} '
                                    f'(${prev_d:.2f} -> ${cur_d:.2f})'
                                ),
                                timestamp=now,
                                raw_data={
                                    'side': label,
                                    'previous_depth': str(prev_d),
                                    'current_depth': str(cur_d),
                                    'change_ratio': change_ratio,
                                },
                            )
                        )

            # --- 3. Spread changes ---------------------------------------------
            cur_best_bid = _best_price(bids, 'bid')
            cur_best_ask = _best_price(asks, 'ask')
            prev_best_bid = _best_price(prev_bids, 'bid')
            prev_best_ask = _best_price(prev_asks, 'ask')

            if (
                cur_best_bid is not None
                and cur_best_ask is not None
                and prev_best_bid is not None
                and prev_best_ask is not None
            ):
                cur_spread = cur_best_ask - cur_best_bid
                prev_spread = prev_best_ask - prev_best_bid
                spread_delta = cur_spread - prev_spread

                if abs(float(spread_delta)) >= self.spread_change_threshold:
                    direction = 'bearish' if spread_delta > 0 else 'bullish'
                    magnitude = _clamp(
                        abs(float(spread_delta)) / self.spread_change_threshold / 5.0
                    )
                    signals.append(
                        OrderFlowSignal(
                            ticker=ticker,
                            signal_type='spread_change',
                            direction=direction,
                            magnitude=magnitude,
                            details=(
                                f'Spread {"widened" if spread_delta > 0 else "narrowed"} '
                                f'by {float(spread_delta):+.4f} on {ticker.symbol} '
                                f'({float(prev_spread):.4f} -> {float(cur_spread):.4f})'
                            ),
                            timestamp=now,
                            raw_data={
                                'previous_spread': str(prev_spread),
                                'current_spread': str(cur_spread),
                                'spread_delta': str(spread_delta),
                            },
                        )
                    )

            # --- 4. Volume spikes -----------------------------------------------
            cur_trade_volume = sum(
                float(
                    _safe_decimal(t.get('price'))
                    * _safe_decimal(t.get('size', t.get('amount', '0')))
                )
                for t in trades
            )
            prev_ma = self._volume_ma.get(token_id, 0.0)

            if (
                prev_ma > 0
                and cur_trade_volume > prev_ma * self.volume_spike_multiplier
            ):
                ratio = cur_trade_volume / prev_ma
                # Infer direction from net trade imbalance.
                buy_vol = sum(
                    float(_safe_decimal(t.get('size', t.get('amount', '0'))))
                    for t in trades
                    if str(t.get('side', '')).lower() in ('buy', 'b')
                )
                sell_vol = sum(
                    float(_safe_decimal(t.get('size', t.get('amount', '0'))))
                    for t in trades
                    if str(t.get('side', '')).lower() in ('sell', 's')
                )
                if buy_vol > sell_vol:
                    direction = 'bullish'
                elif sell_vol > buy_vol:
                    direction = 'bearish'
                else:
                    direction = 'neutral'

                magnitude = _clamp(ratio / (self.volume_spike_multiplier * 3))
                signals.append(
                    OrderFlowSignal(
                        ticker=ticker,
                        signal_type='volume_spike',
                        direction=direction,
                        magnitude=magnitude,
                        details=(
                            f'Volume spike {ratio:.1f}x average on {ticker.symbol} '
                            f'(${cur_trade_volume:,.2f} vs MA ${prev_ma:,.2f})'
                        ),
                        timestamp=now,
                        raw_data={
                            'current_volume': cur_trade_volume,
                            'moving_average': prev_ma,
                            'ratio': ratio,
                        },
                    )
                )

            # Update moving average with exponential smoothing (alpha = 0.3).
            alpha = 0.3
            self._volume_ma[token_id] = (
                alpha * cur_trade_volume + (1 - alpha) * prev_ma
                if prev_ma > 0
                else cur_trade_volume
            )
        else:
            # First snapshot: bootstrap moving average.
            cur_trade_volume = sum(
                float(
                    _safe_decimal(t.get('price'))
                    * _safe_decimal(t.get('size', t.get('amount', '0')))
                )
                for t in trades
            )
            self._volume_ma[token_id] = cur_trade_volume

        # Store current snapshot for next comparison.
        self._prev_snapshots[token_id] = snapshot

        return signals

    # ------------------------------------------------------------------
    # High-level methods
    # ------------------------------------------------------------------

    async def monitor_markets(
        self, tickers: list[PolyMarketTicker]
    ) -> list[OrderFlowSignal]:
        """Scan all *tickers* for order flow signals, rate-limiting between each."""
        all_signals: list[OrderFlowSignal] = []
        for ticker in tickers:
            try:
                signals = await self.detect_signals(ticker)
                all_signals.extend(signals)
            except Exception:
                logger.warning(
                    'Error detecting signals for %s', ticker.symbol, exc_info=True
                )
            # Rate-limit between tickers to respect API limits.
            await self._rate_limit_delay()
        return all_signals

    def summarize_signals(self, signals: list[OrderFlowSignal]) -> str:
        """Format signals into a human-readable summary grouped by ticker."""
        if not signals:
            return 'No order flow signals detected.'

        # Group by ticker symbol.
        grouped: dict[str, list[OrderFlowSignal]] = defaultdict(list)
        for sig in signals:
            grouped[sig.ticker.symbol].append(sig)

        lines: list[str] = ['=== Order Flow Summary ===']
        for symbol in sorted(grouped):
            lines.append(f'\n[{symbol}]')
            # Sort by magnitude descending within each group.
            for sig in sorted(grouped[symbol], key=lambda s: s.magnitude, reverse=True):
                arrow = {'bullish': '^', 'bearish': 'v', 'neutral': '-'}.get(
                    sig.direction, '?'
                )
                lines.append(
                    f'  {arrow} [{sig.signal_type}] (mag={sig.magnitude:.2f}) '
                    f'{sig.details}'
                )
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # BaseFetcher abstract method
    # ------------------------------------------------------------------

    async def fetch(self, tickers: list[PolyMarketTicker]) -> list[OrderFlowSignal]:  # type: ignore[override]
        """BaseFetcher.fetch implementation."""
        return await self.monitor_markets(tickers)
