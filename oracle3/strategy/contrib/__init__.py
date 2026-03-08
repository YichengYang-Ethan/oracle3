"""Contributed strategies requiring optional dependencies (e.g. litellm)."""

from oracle3.strategy.contrib.coint_spread_strategy import CointSpreadStrategy
from oracle3.strategy.contrib.conditional_arb_strategy import ConditionalArbStrategy
from oracle3.strategy.contrib.cross_market_arbitrage_strategy import (
    CrossMarketArbitrageStrategy,
)
from oracle3.strategy.contrib.event_sum_arb_strategy import EventSumArbStrategy
from oracle3.strategy.contrib.exclusivity_arb_strategy import ExclusivityArbStrategy
from oracle3.strategy.contrib.implication_arb_strategy import ImplicationArbStrategy
from oracle3.strategy.contrib.lead_lag_strategy import LeadLagStrategy
from oracle3.strategy.contrib.multi_agent_strategy import MultiAgentStrategy
from oracle3.strategy.contrib.structural_arb_strategy import StructuralArbStrategy

__all__ = [
    'CointSpreadStrategy',
    'ConditionalArbStrategy',
    'CrossMarketArbitrageStrategy',
    'EventSumArbStrategy',
    'ExclusivityArbStrategy',
    'ImplicationArbStrategy',
    'LeadLagStrategy',
    'MultiAgentStrategy',
    'StructuralArbStrategy',
]
