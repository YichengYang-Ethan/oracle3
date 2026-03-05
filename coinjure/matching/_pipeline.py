"""Market matching pipeline orchestrator."""

from __future__ import annotations

import logging

from ._cache import TTLCache
from ._category import build_category_buckets, categories_compatible
from ._date import date_proximity_score, extract_date
from ._fetchers import fetch_kalshi_events, fetch_polymarket_events
from ._resolution import check_resolution_compatibility
from ._template import match_template, template_score
from ._text import MIN_TEXT_SIMILARITY, text_similarity
from ._types import MatchConfidence, MatchResult, NormalizedMarket, StageScores

logger = logging.getLogger(__name__)

# Stage weights
_W_CATEGORY = 0.10
_W_DATE = 0.20
_W_TEMPLATE = 0.30
_W_TEXT = 0.30
_W_RESOLUTION = 0.10


def _compute_confidence(
    scores: StageScores,
    template_passed: bool,
) -> MatchConfidence:
    """Determine confidence level from stage scores."""
    if template_passed:
        return MatchConfidence.HIGH

    # Count stages that "passed"
    passing = 0
    if scores.category > 0:
        passing += 1
    if scores.date > 0.5:  # better than neutral
        passing += 1
    if scores.text >= MIN_TEXT_SIMILARITY:
        passing += 1
    if scores.resolution > 0.5:
        passing += 1

    if passing >= 3:
        return MatchConfidence.HIGH
    if passing >= 2:
        return MatchConfidence.MEDIUM
    return MatchConfidence.LOW


def _compute_spread(
    poly: NormalizedMarket,
    kalshi: NormalizedMarket,
) -> float | None:
    """Compute the YES price spread between two markets."""
    poly_price = poly.get_extra_float('yes_price')
    kalshi_price = kalshi.get_extra_float('yes_price')
    if poly_price is not None and kalshi_price is not None:
        return abs(poly_price - kalshi_price)
    return None


def _score_pair(
    poly: NormalizedMarket,
    kalshi: NormalizedMarket,
) -> MatchResult | None:
    """Run stages 2-5 on a candidate pair and return a MatchResult or None."""
    scores = StageScores()

    # Stage 1: category already bucketed, give baseline score
    if categories_compatible(poly.category, kalshi.category):
        scores.category = 1.0
    else:
        return None

    # Stage 2: date proximity
    poly_date = extract_date(poly)
    kalshi_date = extract_date(kalshi)
    scores.date = date_proximity_score(poly_date, kalshi_date)
    # Hard filter: if both have dates and score is 0, reject
    if poly_date and kalshi_date and scores.date == 0.0:
        return None

    # Stage 3: template matching
    poly_tmpl = match_template(poly)
    kalshi_tmpl = match_template(kalshi)
    t_score = template_score(poly_tmpl, kalshi_tmpl)
    if t_score < 0:
        # Hard reject: same template, different fields
        return None
    scores.template = t_score
    template_passed = t_score > 0

    # Stage 4: text similarity
    scores.text = text_similarity(poly.title, kalshi.title)
    if scores.text < MIN_TEXT_SIMILARITY and not template_passed:
        return None

    # Stage 5: resolution check
    compatible, warnings = check_resolution_compatibility(poly, kalshi)
    if compatible is False:
        return None  # hard reject: incompatible resolution sources
    elif compatible is None:
        scores.resolution = 0.5  # unknown: neutral
    else:
        scores.resolution = 1.0

    # Weighted score
    total = (
        _W_CATEGORY * scores.category
        + _W_DATE * scores.date
        + _W_TEMPLATE * scores.template
        + _W_TEXT * scores.text
        + _W_RESOLUTION * scores.resolution
    )

    confidence = _compute_confidence(scores, template_passed)
    spread = _compute_spread(poly, kalshi)

    label = poly.title[:80]

    return MatchResult(
        poly_market=poly,
        kalshi_market=kalshi,
        confidence=confidence,
        score=round(total, 4),
        stage_scores=scores,
        label=label,
        spread=spread,
        warnings=warnings,
        resolution_compatible=compatible,
    )


class MarketMatchingPipeline:
    """Multi-stage pipeline for matching prediction markets across platforms.

    Usage::

        pipeline = MarketMatchingPipeline()
        matches = await pipeline.scan()
        for m in matches:
            print(m.label, m.confidence, m.spread)
    """

    def __init__(
        self,
        cache_ttl: float = 300.0,
        max_poly_events: int = 500,
        max_kalshi_pages: int = 15,
        min_volume: float = 100.0,
    ) -> None:
        self._cache = TTLCache(ttl_seconds=cache_ttl)
        self._max_poly_events = max_poly_events
        self._max_kalshi_pages = max_kalshi_pages
        self._min_volume = min_volume

    async def scan(self) -> list[MatchResult]:
        """Fetch markets from both platforms and run the matching pipeline.

        Returns a list of MatchResult sorted by score (descending).
        """
        # Fetch
        poly_markets = await fetch_polymarket_events(
            cache=self._cache,
            max_events=self._max_poly_events,
        )
        kalshi_markets = await fetch_kalshi_events(
            cache=self._cache,
            max_pages=self._max_kalshi_pages,
        )

        logger.info(
            'Pipeline: %d Poly x %d Kalshi markets (raw)',
            len(poly_markets),
            len(kalshi_markets),
        )

        # Volume filter
        if self._min_volume > 0:
            pre_poly = len(poly_markets)
            pre_kalshi = len(kalshi_markets)
            poly_markets = [
                m for m in poly_markets
                if (m.get_extra_float('volume') or 0.0) >= self._min_volume
            ]
            kalshi_markets = [
                m for m in kalshi_markets
                if (m.get_extra_float('volume') or 0.0) >= self._min_volume
            ]
            logger.info(
                'Volume filter (min=%.0f): Poly %d→%d, Kalshi %d→%d',
                self._min_volume,
                pre_poly,
                len(poly_markets),
                pre_kalshi,
                len(kalshi_markets),
            )

        # Stage 1: category bucketing
        buckets = build_category_buckets(poly_markets, kalshi_markets)
        total_comparisons = sum(
            len(p) * len(k) for p, k in buckets.values()
        )
        logger.info(
            'After bucketing: %d buckets, ~%d comparisons '
            '(vs %d full cross-product)',
            len(buckets),
            total_comparisons,
            len(poly_markets) * len(kalshi_markets),
        )

        # Stages 2-5: pairwise scoring within buckets
        results: list[MatchResult] = []
        seen_pairs: set[tuple[str, str]] = set()

        for _bucket_name, (p_list, k_list) in buckets.items():
            for poly in p_list:
                for kalshi in k_list:
                    pair_key = (poly.market_id, kalshi.market_id)
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)

                    match = _score_pair(poly, kalshi)
                    if match is not None:
                        results.append(match)

        # Sort by score descending
        results.sort(key=lambda m: m.score, reverse=True)

        # Deduplicate: keep best match per poly market
        best_by_poly: dict[str, MatchResult] = {}
        for r in results:
            key = r.poly_market.market_id
            if key not in best_by_poly or r.score > best_by_poly[key].score:
                best_by_poly[key] = r

        # Also deduplicate per kalshi market
        best_by_kalshi: dict[str, MatchResult] = {}
        for r in sorted(
            best_by_poly.values(), key=lambda m: m.score, reverse=True
        ):
            key = r.kalshi_market.market_id
            if key not in best_by_kalshi:
                best_by_kalshi[key] = r

        final = sorted(
            best_by_kalshi.values(), key=lambda m: m.score, reverse=True
        )
        logger.info(
            'Pipeline complete: %d matches (%d HIGH, %d MEDIUM, %d LOW)',
            len(final),
            sum(1 for m in final if m.confidence == MatchConfidence.HIGH),
            sum(1 for m in final if m.confidence == MatchConfidence.MEDIUM),
            sum(1 for m in final if m.confidence == MatchConfidence.LOW),
        )
        return final

    def clear_cache(self) -> None:
        """Clear the API response cache."""
        self._cache.clear()
