"""Contributed strategies requiring optional dependencies (e.g. litellm)."""

from oracle3.strategy.contrib.cross_market_arbitrage_strategy import (
    CrossMarketArbitrageStrategy,
)
from oracle3.strategy.contrib.multi_agent_strategy import MultiAgentStrategy

__all__ = ['CrossMarketArbitrageStrategy', 'MultiAgentStrategy']
