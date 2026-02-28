from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal

from oracle3.events.events import Event, OrderBookEvent
from oracle3.strategy.strategy import Strategy, StrategyDecision
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class OrderBookPressureStrategy(Strategy):
    """Trade on order-book imbalance pressure."""

    def __init__(
        self,
        depth: int = 3,
        entry_imbalance: float = 0.30,
        exit_imbalance: float = -0.10,
        trade_size: Decimal = Decimal('10'),
    ) -> None:
        self.depth = depth
        self.entry_imbalance = entry_imbalance
        self.exit_imbalance = exit_imbalance
        self.trade_size = trade_size
        self._decisions: deque[StrategyDecision] = deque(maxlen=200)
        self._stats = {
            'decisions': 0,
            'executed': 0,
            'buy_yes': 0,
            'closes': 0,
            'holds': 0,
        }

    @staticmethod
    def _mark_stats(stats: dict[str, int], action: str, executed: bool) -> None:
        if action == 'HOLD':
            stats['holds'] += 1
        if executed:
            stats['executed'] += 1
            if action == 'BUY_YES':
                stats['buy_yes'] += 1
            elif action == 'CLOSE_PRESSURE':
                stats['closes'] += 1
        stats['decisions'] += 1

    async def _try_entry(
        self, event: OrderBookEvent, trader: Trader
    ) -> tuple[str, bool]:
        best_ask = trader.market_data.get_best_ask(event.ticker)
        if best_ask is None:
            return 'HOLD', False
        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=event.ticker,
            limit_price=best_ask.price,
            quantity=self.trade_size,
        )
        executed = result.order is not None and result.order.filled_quantity > 0
        return 'BUY_YES', executed

    async def _try_exit(
        self, event: OrderBookEvent, trader: Trader
    ) -> tuple[str, bool]:
        position = trader.position_manager.get_position(event.ticker)
        if position is None or position.quantity <= 0:
            return 'HOLD', False
        best_bid = trader.market_data.get_best_bid(event.ticker)
        if best_bid is None:
            return 'HOLD', False
        qty = min(self.trade_size, position.quantity)
        result = await trader.place_order(
            side=TradeSide.SELL,
            ticker=event.ticker,
            limit_price=best_bid.price,
            quantity=qty,
        )
        executed = result.order is not None and result.order.filled_quantity > 0
        return 'CLOSE_PRESSURE', executed

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, OrderBookEvent):
            return

        ob = trader.market_data.order_books.get(event.ticker)
        if ob is None:
            return

        bids = ob.get_bids(self.depth)
        asks = ob.get_asks(self.depth)
        bid_vol = sum((lv.size for lv in bids), Decimal('0'))
        ask_vol = sum((lv.size for lv in asks), Decimal('0'))
        total = bid_vol + ask_vol
        if total <= 0:
            return

        imbalance = float((bid_vol - ask_vol) / total)
        position = trader.position_manager.get_position(event.ticker)
        has_position = position is not None and position.quantity > 0
        action = 'HOLD'
        executed = False

        if not has_position and imbalance >= self.entry_imbalance:
            action, executed = await self._try_entry(event, trader)
        elif has_position and imbalance <= self.exit_imbalance:
            action, executed = await self._try_exit(event, trader)

        self._mark_stats(self._stats, action, executed)
        self._decisions.append(
            StrategyDecision(
                timestamp=datetime.now().strftime('%H:%M:%S'),
                ticker_name=(event.ticker.name or event.ticker.symbol)[:40],
                action=action,
                executed=executed,
                reasoning=f'imbalance={imbalance:+.3f}',
                signal_values={
                    'imbalance': imbalance,
                    'bid_volume': float(bid_vol),
                    'ask_volume': float(ask_vol),
                },
            )
        )

    def get_decisions(self) -> list[StrategyDecision]:
        return list(self._decisions)

    def get_decision_stats(self) -> dict[str, int | float]:
        return dict(self._stats)
