"""Solana/DFlow-optimized agent strategy for Oracle3."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from oracle3.events.events import Event, NewsEvent, OrderBookEvent, PriceChangeEvent
from oracle3.strategy.agent_strategy import AgentStrategy
from oracle3.trader.trader import Trader
from oracle3.trader.types import TradeSide


class SolanaAgentStrategy(AgentStrategy):
    """LLM-driven strategy optimized for DFlow prediction markets on Solana.

    Uses the OpenAI Agents SDK to analyze Solana-tokenized prediction markets,
    incorporating on-chain liquidity signals and DFlow-specific market structure.
    """

    name = 'SolanaAgentStrategy'
    version = '1.0.0'
    author = 'Oracle3 Team'

    agent_name = 'oracle3-solana-agent'
    agent_model = 'gpt-4.1-mini'
    agent_max_turns = 8

    def __init__(
        self,
        trade_size: float = 10.0,
        confidence_threshold: float = 0.6,
        max_position_pct: float = 0.15,
    ) -> None:
        super().__init__()
        self.trade_size = Decimal(str(trade_size))
        self.confidence_threshold = confidence_threshold
        self.max_position_pct = max_position_pct

    def get_prompt_guide(self) -> str:
        return (
            'You are an AI trading agent running inside Oracle3, specialized in '
            'Solana-based prediction markets via DFlow. '
            'DFlow tokenizes Kalshi prediction markets on Solana mainnet-beta, '
            'allowing on-chain settlement with SPL tokens. '
            'Use the bound StrategyContext to inspect market data: '
            'context.ticker_history() for price history, '
            'context.order_books() for current bid/ask, '
            'context.positions() for portfolio exposure, '
            'context.recent_news() for market-moving news. '
            'Consider the following when making decisions: '
            '1. DFlow markets mirror Kalshi — watch for cross-exchange pricing gaps. '
            '2. Solana settlement is near-instant — no need to worry about settlement risk. '
            '3. On-chain liquidity may be thinner — prefer smaller position sizes. '
            '4. Focus on high-conviction trades with clear catalysts. '
            'Place trades via trader.place_order(side, ticker, price, quantity). '
            'Do not use future information. Only act on visible context data.'
        )

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        context = self.require_context()

        if isinstance(event, NewsEvent):
            # Use the OpenAI agent for news-driven analysis
            if self.sdk_available():
                try:
                    result = await self.run_openai_agent(context=context)
                    output = self.get_run_final_output(result)
                    self.record_decision(
                        ticker_name=str(event.ticker) if hasattr(event, 'ticker') else 'unknown',
                        action='ANALYZE',
                        executed=False,
                        reasoning=output[:500],
                        signal_values={'source': 'openai_agent'},
                    )
                except Exception as e:
                    self.record_decision(
                        ticker_name='unknown',
                        action='ERROR',
                        executed=False,
                        reasoning=f'Agent error: {e}',
                        signal_values={},
                    )
            else:
                prompt = self.build_prompt_context(context)
                self.record_decision(
                    ticker_name=str(event.ticker) if hasattr(event, 'ticker') else 'unknown',
                    action='HOLD',
                    executed=False,
                    reasoning=f'SDK not available. Context: {prompt[:200]}',
                    signal_values={},
                )
            return

        if isinstance(event, OrderBookEvent):
            # Monitor order book for significant moves
            return

        if isinstance(event, PriceChangeEvent):
            return
