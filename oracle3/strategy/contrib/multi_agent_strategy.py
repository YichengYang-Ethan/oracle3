"""Multi-agent strategy — delegates to AgentCoordinator pipeline.

Wraps the SignalAgent → RiskAgent → ExecutionAgent pipeline into a
Strategy interface compatible with the TradingEngine.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from oracle3.agent.coordinator import AgentCoordinator
from oracle3.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.agent_strategy import AgentStrategy
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide

logger = logging.getLogger(__name__)


class MultiAgentStrategy(AgentStrategy):
    """Strategy that delegates to a multi-agent coordinator pipeline.

    Instead of a single LLM call, this strategy routes events through
    a pipeline of specialized agents:
      SignalAgent → RiskAgent → ExecutionAgent
    """

    name = 'multi_agent'
    version = '1.0.0'
    author = 'oracle3'

    def __init__(
        self,
        coordinator: AgentCoordinator | None = None,
        trade_size: float = 10.0,
        min_confidence: float = 0.5,
    ) -> None:
        super().__init__()
        self.coordinator = coordinator or AgentCoordinator()
        self.trade_size = Decimal(str(trade_size))
        self.min_confidence = min_confidence

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        context = self.require_context()
        task = self._event_to_task(event, context)
        if task is None:
            return

        try:
            result = await self.coordinator.run_pipeline(task)

            if result.success and result.side in ('buy', 'sell'):
                ticker = context.resolve_ticker(result.ticker)
                if ticker is not None:
                    side = TradeSide.BUY if result.side == 'buy' else TradeSide.SELL
                    price = Decimal(str(result.price)) if result.price > 0 else Decimal('0.50')
                    await trader.place_order(
                        side=side,
                        ticker=ticker,
                        limit_price=price,
                        quantity=self.trade_size,
                    )
                    self.record_decision(
                        ticker_name=result.ticker[:40],
                        action=f'MULTI_AGENT_{result.side.upper()}',
                        executed=True,
                        reasoning=f'Pipeline result: {result.side} (sig={result.signature[:16]})',
                    )
            else:
                self.record_decision(
                    ticker_name=task.get('ticker', '')[:40],
                    action='HOLD',
                    executed=False,
                    reasoning=f'Pipeline: no action (error={result.error})',
                )
        except Exception:
            logger.debug('Multi-agent pipeline error', exc_info=True)

    @staticmethod
    def _event_to_task(event: Event, context: Any) -> dict[str, Any] | None:
        """Convert an event to a coordinator task dict."""
        if isinstance(event, PriceChangeEvent):
            return {
                'ticker': event.ticker.symbol,
                'price': float(event.price),
                'context': f'Price change: {event.ticker.symbol} -> {event.price}',
            }
        if isinstance(event, OrderBookEvent):
            return {
                'ticker': event.ticker.symbol,
                'price': float(event.price),
                'context': f'Order book: {event.ticker.symbol} {event.side} {event.price}',
            }
        if isinstance(event, NewsEvent):
            ticker_sym = event.ticker.symbol if event.ticker else ''
            return {
                'ticker': ticker_sym,
                'price': 0.0,
                'context': f'News: {event.title[:200]}',
            }
        return None
