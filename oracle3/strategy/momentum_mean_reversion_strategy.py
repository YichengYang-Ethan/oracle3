"""Momentum + Mean-Reversion hybrid strategy for prediction markets.

Entry logic (BUY_YES):
  - Waits for a price dip: z-score of recent prices falls below ``entry_z``
    (i.e. the market is temporarily oversold vs its own recent history)
  - Requires short-term momentum confirmation: the last ``momentum_bars``
    prices must be consecutive rising ticks (dip is ending)

Exit logic:
  - Take profit: z-score recovers above ``exit_z`` (mean reversion complete)
  - Stop loss: price drops more than ``stop_loss_pct`` below entry
  - Time stop: position held longer than ``max_hold_bars`` price ticks

Rationale for prediction markets:
  - Binary market prices often overshoot on news then revert when participants
    digest information — this is the dip we're buying.
  - Momentum confirmation prevents catching falling knives.
  - Short holding periods protect against macro-event resolution risk.
"""

from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal

from oracle3.events.events import Event, OrderBookEvent, PriceChangeEvent
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

from .quant_strategy import QuantStrategy


class MomentumMeanReversionStrategy(QuantStrategy):
    def __init__(
        self,
        lookback: int = 20,
        momentum_bars: int = 3,
        entry_z: float = -1.2,
        exit_z: float = 0.5,
        stop_loss_pct: float = 0.06,
        position_size: Decimal = Decimal('10'),
        max_hold_bars: int = 60,
    ) -> None:
        self.lookback = lookback
        self.momentum_bars = momentum_bars
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_loss_pct = stop_loss_pct
        self.position_size = Decimal(str(position_size))
        self.max_hold_bars = max_hold_bars
        self.logger = logging.getLogger(__name__)

        # symbol -> rolling price window
        self._prices: dict[str, deque[float]] = {}
        # symbol -> entry price (float)
        self._entry_price: dict[str, float] = {}
        # symbol -> bars held
        self._hold_bars: dict[str, int] = {}
        # guard against concurrent close coroutines
        self._closing_in_progress: set[str] = set()

    # ------------------------------------------------------------------
    # Signal helpers
    # ------------------------------------------------------------------

    def _z_score(self, prices: deque[float]) -> float:
        n = len(prices)
        if n < 2:
            return 0.0
        mean = sum(prices) / n
        variance = sum((p - mean) ** 2 for p in prices) / n
        std = variance**0.5
        if std < 1e-10:
            return 0.0
        return (prices[-1] - mean) / std

    def _momentum_not_falling(self, prices: deque[float]) -> bool:
        """Return True if recent price action is flat-or-rising (not still dropping).

        Prediction market prices move in step-jumps with long flat periods.
        We just require: last bar >= bar N steps ago (the dip is not accelerating).
        """
        if len(prices) < self.momentum_bars + 1:
            return False
        recent = list(prices)[-(self.momentum_bars + 1) :]
        # Last price >= first price in the window (not still in free-fall)
        return recent[-1] >= recent[0]

    # ------------------------------------------------------------------
    # Main event loop
    # ------------------------------------------------------------------

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        # Accept both historical PriceChangeEvent and live OrderBookEvent
        if isinstance(event, PriceChangeEvent):
            ticker = event.ticker
            current_price = float(event.price)
        elif isinstance(event, OrderBookEvent):
            ticker = event.ticker
            # Derive mid-price from best bid/ask
            bid = trader.market_data.get_best_bid(ticker)
            ask = trader.market_data.get_best_ask(ticker)
            if bid is None or ask is None:
                return
            current_price = float(bid.price + ask.price) / 2.0
        else:
            return

        sym = ticker.symbol

        buf = self._prices.setdefault(
            sym, deque(maxlen=self.lookback + self.momentum_bars + 5)
        )
        buf.append(current_price)

        z = self._z_score(buf)
        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity > 0

        if has_position:
            await self._manage_exit(ticker, trader, sym, current_price, z, position)
        else:
            await self._check_entry(ticker, trader, sym, current_price, z, buf)

    async def _manage_exit(self, ticker, trader, sym, current_price, z, position):
        if sym in self._closing_in_progress:
            return

        self._hold_bars[sym] = self._hold_bars.get(sym, 0) + 1
        entry_price = self._entry_price.get(sym, current_price)
        pnl = current_price - entry_price

        if z > self.exit_z:
            action = 'CLOSE_TP'
            reason = f'z={z:.2f} > exit_z={self.exit_z}, pnl={pnl:+.4f}'
        elif pnl < -self.stop_loss_pct:
            action = 'CLOSE_SL'
            reason = f'stop-loss: pnl={pnl:+.4f} < -{self.stop_loss_pct}'
        elif self._hold_bars.get(sym, 0) >= self.max_hold_bars:
            action = 'CLOSE_TIMEOUT'
            reason = f'timeout: {self._hold_bars.get(sym, 0)} bars, pnl={pnl:+.4f}'
        else:
            self.record_decision(
                ticker_name=ticker.name or sym,
                action='HOLD',
                executed=False,
                reasoning=f'z={z:.2f}, bars={self._hold_bars.get(sym, 0)}, pnl={pnl:+.4f}',
                signal_values={'z_score': z, 'price': current_price, 'pnl': pnl},
            )
            return

        self._closing_in_progress.add(sym)
        try:
            bid = trader.market_data.get_best_bid(ticker)
            if bid is None:
                return
            result = await trader.place_order(
                side=TradeSide.SELL,
                ticker=ticker,
                limit_price=bid.price,
                quantity=position.quantity,
            )
            executed = result.order is not None
            if executed:
                self._entry_price.pop(sym, None)
                self._hold_bars.pop(sym, None)
            self.record_decision(
                ticker_name=ticker.name or sym,
                action=action,
                executed=executed,
                reasoning=reason,
                signal_values={'z_score': z, 'price': current_price, 'pnl': pnl},
            )
        finally:
            self._closing_in_progress.discard(sym)

    async def _check_entry(self, ticker, trader, sym, current_price, z, buf):
        if len(buf) < self.lookback:
            return

        if z < self.entry_z and self._momentum_not_falling(buf):
            ask = trader.market_data.get_best_ask(ticker)
            if ask is None:
                return
            result = await trader.place_order(
                side=TradeSide.BUY,
                ticker=ticker,
                limit_price=ask.price,
                quantity=self.position_size,
            )
            executed = result.order is not None
            if executed:
                self._entry_price[sym] = float(ask.price)
                self._hold_bars[sym] = 0
            self.record_decision(
                ticker_name=ticker.name or sym,
                action='BUY_YES',
                executed=executed,
                reasoning=f'z={z:.2f} < entry_z={self.entry_z}, not_falling=True',
                signal_values={'z_score': z, 'price': current_price},
            )
        else:
            self.record_decision(
                ticker_name=ticker.name or sym,
                action='HOLD',
                executed=False,
                reasoning=f'z={z:.2f} (entry_z={self.entry_z}, not_falling={self._momentum_not_falling(buf)})',
                signal_values={'z_score': z, 'price': current_price},
            )
