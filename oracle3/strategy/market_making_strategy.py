"""Market making strategy for prediction market binary options.

Fires on OrderBookEvent. Monitors spread width.

    spread = best_ask.price - best_bid.price
    entry_price = best_bid.price + tick   (must be < best_ask)
    take_profit  = entry_price + spread * take_profit_pct
    stop_loss    = entry_price * (1 - stop_loss_pct)

- Enter BUY_YES when spread > min_spread and no position
- Exit CLOSE_TP / CLOSE_SL / CLOSE_TIMEOUT
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from oracle3.events.events import Event, OrderBookEvent
from oracle3.ticker.ticker import Ticker
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

from .quant_strategy import QuantStrategy


class MarketMakingStrategy(QuantStrategy):
    name = 'market_making'
    version = '0.2.0'
    author = 'oracle3'

    def __init__(
        self,
        tickers: list[Ticker] | None = None,
        min_spread: Decimal = Decimal('0.05'),
        take_profit_pct: float = 0.5,
        stop_loss_pct: float = 0.02,
        position_size: Decimal = Decimal('10'),
        max_hold_seconds: int = 120,
        tick: Decimal = Decimal('0.01'),
    ) -> None:
        super().__init__()
        ticker_list = tickers or []
        self.tickers: set[str] = {t.symbol for t in ticker_list}
        self._ticker_map: dict[str, Ticker] = {t.symbol: t for t in ticker_list}
        self.min_spread = min_spread
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.position_size = position_size
        self.max_hold_seconds = max_hold_seconds
        self.tick = tick
        self.logger = logging.getLogger(__name__)

        # symbol → (entry_time, entry_price, take_profit, stop_loss)
        self._entries: dict[str, tuple[datetime, Decimal, Decimal, Decimal]] = {}
        self._closing_in_progress: set[str] = set()

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, OrderBookEvent):
            return
        ticker = event.ticker
        if self.tickers and ticker.symbol not in self.tickers:
            return

        ob = trader.market_data.order_books.get(ticker)
        if ob is None:
            return

        best_bid = ob.best_bid
        best_ask = ob.best_ask
        if best_bid is None or best_ask is None or best_bid.price <= 0:
            return

        spread = best_ask.price - best_bid.price
        mid = (best_bid.price + best_ask.price) / Decimal('2')

        position = trader.position_manager.get_position(ticker)
        has_position = position is not None and position.quantity > 0
        sym = ticker.symbol
        now = datetime.now()

        if has_position:
            if sym in self._closing_in_progress:
                return
            meta = self._entries.get(sym)
            if meta is None:
                return  # No entry metadata

            entry_time, entry_price, take_profit, stop_loss = meta
            elapsed = (now - entry_time).total_seconds()

            # Current price for P&L check
            cur_price = best_bid.price

            close_reason: str | None = None
            if cur_price >= take_profit:
                close_reason = 'CLOSE_TP'
            elif cur_price <= stop_loss:
                close_reason = 'CLOSE_SL'
            elif elapsed > self.max_hold_seconds:
                close_reason = 'CLOSE_TIMEOUT'

            if close_reason:
                self._closing_in_progress.add(sym)
                try:
                    result = await trader.place_order(
                        side=TradeSide.SELL,
                        ticker=ticker,
                        limit_price=cur_price,
                        quantity=position.quantity,
                    )
                    executed = result.order is not None
                    if executed:
                        self._entries.pop(sym, None)
                    self.record_decision(
                        ticker_name=ticker.name or sym,
                        action=close_reason,
                        executed=executed,
                        reasoning=f'spread={float(spread):.4f}, cur={float(cur_price):.4f}, tp={float(take_profit):.4f}, sl={float(stop_loss):.4f}',
                        signal_values={'spread': float(spread), 'mid': float(mid)},
                    )
                finally:
                    self._closing_in_progress.discard(sym)
        else:
            if spread >= self.min_spread:
                entry_price = best_bid.price + self.tick
                # Must not cross the spread
                if entry_price >= best_ask.price:
                    self.record_decision(
                        ticker_name=ticker.name or sym,
                        action='HOLD',
                        executed=False,
                        reasoning=f'entry_price={float(entry_price):.4f} would cross spread',
                        signal_values={'spread': float(spread), 'mid': float(mid)},
                    )
                    return

                take_profit = entry_price + spread * Decimal(str(self.take_profit_pct))
                stop_loss = entry_price * Decimal(str(1 - self.stop_loss_pct))

                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=ticker,
                    limit_price=entry_price,
                    quantity=self.position_size,
                )
                executed = result.order is not None
                if executed:
                    self._entries[sym] = (now, entry_price, take_profit, stop_loss)
                self.record_decision(
                    ticker_name=ticker.name or sym,
                    action='BUY_YES',
                    executed=executed,
                    reasoning=f'spread={float(spread):.4f} >= min={float(self.min_spread):.4f}',
                    signal_values={'spread': float(spread), 'mid': float(mid)},
                )
            else:
                self.record_decision(
                    ticker_name=ticker.name or sym,
                    action='HOLD',
                    executed=False,
                    reasoning=f'spread={float(spread):.4f} < min={float(self.min_spread):.4f}',
                    signal_values={'spread': float(spread), 'mid': float(mid)},
                )
