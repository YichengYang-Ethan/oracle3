from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal

from oracle3.events.events import Event, PriceChangeEvent
from oracle3.strategy.strategy import Strategy, StrategyDecision
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class ThresholdMomentumStrategy(Strategy):
    """Buy on upward jumps and reduce on downward moves."""

    def __init__(
        self,
        up_move_pct: Decimal = Decimal('0.015'),
        down_move_pct: Decimal = Decimal('0.015'),
        trade_size: Decimal = Decimal('10'),
    ) -> None:
        self.up_move_pct = up_move_pct
        self.down_move_pct = down_move_pct
        self.trade_size = trade_size
        self._last_price: dict[str, Decimal] = {}
        self._decisions: deque[StrategyDecision] = deque(maxlen=200)
        self._stats = {
            'decisions': 0,
            'executed': 0,
            'buy_yes': 0,
            'sells': 0,
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
            elif action == 'SELL_YES':
                stats['sells'] += 1
        stats['decisions'] += 1

    async def _buy_on_up_move(
        self, event: PriceChangeEvent, trader: Trader, current: Decimal
    ) -> tuple[str, bool]:
        limit_price = min(Decimal('0.99'), current + Decimal('0.01'))
        result = await trader.place_order(
            side=TradeSide.BUY,
            ticker=event.ticker,
            limit_price=limit_price,
            quantity=self.trade_size,
        )
        executed = result.order is not None and result.order.filled_quantity > 0
        return 'BUY_YES', executed

    async def _sell_on_down_move(
        self, event: PriceChangeEvent, trader: Trader, current: Decimal
    ) -> tuple[str, bool]:
        position = trader.position_manager.get_position(event.ticker)
        if position is None or position.quantity <= 0:
            return 'HOLD', False
        qty = min(self.trade_size, position.quantity)
        limit_price = max(Decimal('0.01'), current - Decimal('0.01'))
        result = await trader.place_order(
            side=TradeSide.SELL,
            ticker=event.ticker,
            limit_price=limit_price,
            quantity=qty,
        )
        executed = result.order is not None and result.order.filled_quantity > 0
        return 'SELL_YES', executed

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        if not isinstance(event, PriceChangeEvent):
            return

        symbol = event.ticker.symbol
        current = event.price
        previous = self._last_price.get(symbol)
        self._last_price[symbol] = current
        if previous is None or previous <= 0:
            return

        move = (current - previous) / previous
        action = 'HOLD'
        executed = False
        reasoning = f'move={float(move):+.3%}'

        if move >= self.up_move_pct:
            action, executed = await self._buy_on_up_move(event, trader, current)
        elif move <= -self.down_move_pct:
            action, executed = await self._sell_on_down_move(event, trader, current)

        self._mark_stats(self._stats, action, executed)
        self._decisions.append(
            StrategyDecision(
                timestamp=datetime.now().strftime('%H:%M:%S'),
                ticker_name=(event.ticker.name or event.ticker.symbol)[:40],
                action=action,
                executed=executed,
                reasoning=reasoning,
                signal_values={
                    'current_price': float(current),
                    'previous_price': float(previous),
                    'move_pct': float(move),
                },
            )
        )

    def get_decisions(self) -> list[StrategyDecision]:
        return list(self._decisions)

    def get_decision_stats(self) -> dict[str, int | float]:
        return dict(self._stats)
