"""Cross-platform market matching pipeline.

Usage::

    from coinjure.matching import MarketMatchingPipeline

    pipeline = MarketMatchingPipeline()
    matches = await pipeline.scan()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._pipeline import MarketMatchingPipeline
from ._types import MatchConfidence, MatchResult, NormalizedMarket, StageScores

if TYPE_CHECKING:
    from examples.strategies.cross_platform_arb_strategy import MatchedMarket

__all__ = [
    'MarketMatchingPipeline',
    'MatchConfidence',
    'MatchResult',
    'NormalizedMarket',
    'StageScores',
]


def match_result_to_matched_market(result: MatchResult) -> MatchedMarket:
    """Bridge function to convert a MatchResult to a legacy MatchedMarket.

    Requires oracle3 to be importable. Returns a MatchedMarket instance
    from ``examples.strategies.cross_platform_arb_strategy``.
    """
    from oracle3.ticker.ticker import KalshiTicker, PolyMarketTicker

    poly = result.poly_market
    kalshi = result.kalshi_market

    poly_ticker = PolyMarketTicker(
        symbol=poly.get_extra_str('token_id', poly.market_id) or poly.market_id,
        name=poly.title,
        token_id=poly.get_extra_str('token_id'),
        market_id=poly.market_id,
        event_id=poly.event_id,
        no_token_id=poly.get_extra_str('no_token_id'),
    )
    kalshi_ticker = KalshiTicker(
        symbol=kalshi.market_id,
        name=kalshi.title,
        market_ticker=kalshi.get_extra_str('market_ticker', kalshi.market_id),
        event_ticker=kalshi.get_extra_str('event_ticker', kalshi.event_id),
        series_ticker=kalshi.series_ticker,
    )

    # Import MatchedMarket from the strategy module
    from examples.strategies.cross_platform_arb_strategy import MatchedMarket

    return MatchedMarket(
        poly_ticker=poly_ticker,
        kalshi_ticker=kalshi_ticker,
        similarity=result.score,
        label=result.label,
        confidence=result.confidence.value,
    )
