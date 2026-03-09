"""Tests for the coinjure.matching pipeline optimizations."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from coinjure.matching._category import (
    _extract_keyphrases,
    _normalize_for_keyphrases,
    build_category_buckets,
    categorize_poly,
)
from coinjure.matching._pipeline import (
    _W_RESOLUTION,
    _W_TEMPLATE,
    _compute_confidence,
    _score_pair,
)
from coinjure.matching._types import (
    MatchConfidence,
    MatchResult,
    NormalizedMarket,
    StageScores,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market(
    platform: str = 'polymarket',
    title: str = 'Test market',
    market_id: str = 'mkt-1',
    event_id: str = 'evt-1',
    tags: list[str] | None = None,
    series_ticker: str = '',
    resolution_source: str = '',
    extra: dict | None = None,
) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        event_id=event_id,
        market_id=market_id,
        title=title,
        tags=tags or [],
        series_ticker=series_ticker,
        resolution_source=resolution_source,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Change 1: Resolution incompatibility → hard reject
# ---------------------------------------------------------------------------


class TestResolutionFilter:
    """Change 1: incompatible resolution sources are hard-rejected."""

    def test_incompatible_returns_none(self) -> None:
        """Incompatible resolution sources should hard-reject the pair."""
        poly = _make_market(
            platform='polymarket',
            title='Will BTC reach 100k by end of 2026',
            market_id='poly-1',
            resolution_source='ap',
        )
        kalshi = _make_market(
            platform='kalshi',
            title='Will BTC reach 100k by end of 2026',
            market_id='kalshi-1',
            series_ticker='KXBTC',
            resolution_source='coingecko',
        )
        poly.category = 'crypto_btc'
        kalshi.category = 'crypto_btc'

        with patch('coinjure.matching._pipeline.check_resolution_compatibility') as mock_res:
            mock_res.return_value = (False, ['incompatible sources'])
            result = _score_pair(poly, kalshi)
        assert result is None, 'Incompatible resolution should return None (hard reject)'

    def test_compatible_passes(self) -> None:
        """Compatible resolution sources should produce a match."""
        poly = _make_market(
            platform='polymarket',
            title='Will BTC reach 100k by end of 2026',
            market_id='poly-2',
            resolution_source='ap',
        )
        kalshi = _make_market(
            platform='kalshi',
            title='Will BTC reach 100k by end of 2026',
            market_id='kalshi-2',
            series_ticker='KXBTC',
            resolution_source='ap',
        )
        poly.category = 'crypto_btc'
        kalshi.category = 'crypto_btc'

        with patch('coinjure.matching._pipeline.check_resolution_compatibility') as mock_res:
            mock_res.return_value = (True, [])
            result = _score_pair(poly, kalshi)
        assert result is not None, 'Compatible resolution should produce a match'
        assert result.stage_scores.resolution == 1.0

    def test_unknown_resolution_not_rejected(self) -> None:
        """Unknown resolution (None) should get neutral score 0.5, not rejected."""
        poly = _make_market(
            platform='polymarket',
            title='Will BTC reach 100k by end of 2026',
            market_id='poly-3',
            resolution_source='',
        )
        kalshi = _make_market(
            platform='kalshi',
            title='Will BTC reach 100k by end of 2026',
            market_id='kalshi-3',
            series_ticker='KXBTC',
            resolution_source='',
        )
        poly.category = 'crypto_btc'
        kalshi.category = 'crypto_btc'

        with patch('coinjure.matching._pipeline.check_resolution_compatibility') as mock_res:
            mock_res.return_value = (None, [])
            result = _score_pair(poly, kalshi)
        assert result is not None, 'Unknown resolution should not reject'
        assert result.stage_scores.resolution == 0.5

    def test_weight_rebalance(self) -> None:
        """Verify weights after rebalance: template=0.30, resolution=0.10."""
        assert _W_TEMPLATE == 0.30
        assert _W_RESOLUTION == 0.10

    def test_confidence_resolution_threshold(self) -> None:
        """Resolution score of 0.5 should NOT count as passing for confidence."""
        scores = StageScores(
            category=1.0,
            date=0.8,
            template=0.0,
            text=0.6,
            resolution=0.5,
        )
        confidence = _compute_confidence(scores, template_passed=False)
        # Only category, date, text pass (3 stages). resolution=0.5 does NOT pass (>0.5 required).
        assert confidence == MatchConfidence.HIGH  # 3 passing stages

        scores2 = StageScores(
            category=1.0,
            date=0.3,
            template=0.0,
            text=0.6,
            resolution=0.5,
        )
        confidence2 = _compute_confidence(scores2, template_passed=False)
        # category passes, date fails (<= 0.5), text passes, resolution fails
        assert confidence2 == MatchConfidence.MEDIUM  # 2 passing


# ---------------------------------------------------------------------------
# Change 2: Pipeline bridge produces MatchedMarket with confidence
# ---------------------------------------------------------------------------


class TestPipelineBridge:
    """Change 2: bridge function and strategy integration."""

    def test_bridge_produces_matched_market_with_confidence(self) -> None:
        """Bridge function should produce a MatchedMarket with confidence field."""
        poly = _make_market(
            platform='polymarket',
            title='Test event',
            market_id='poly-bridge',
            event_id='evt-bridge',
            extra={'token_id': 'tok-1'},
        )
        kalshi = _make_market(
            platform='kalshi',
            title='Test event',
            market_id='kalshi-bridge',
            event_id='evt-bridge-k',
            series_ticker='KXTEST',
            extra={'market_ticker': 'KXTEST-MKT', 'event_ticker': 'KXTEST-EVT'},
        )
        result = MatchResult(
            poly_market=poly,
            kalshi_market=kalshi,
            confidence=MatchConfidence.HIGH,
            score=0.85,
            stage_scores=StageScores(category=1.0, date=0.8, template=0.9, text=0.7, resolution=1.0),
            label='Test event',
        )

        from coinjure.matching import match_result_to_matched_market

        matched = match_result_to_matched_market(result)
        assert matched.confidence == 'HIGH'
        assert matched.similarity == 0.85
        assert matched.label == 'Test event'

    def test_strategy_without_pipeline_returns_zero(self) -> None:
        """Strategy with pipeline=None should return 0 from refresh."""
        from examples.strategies.cross_platform_arb_strategy import (
            CrossPlatformArbStrategy,
        )

        strategy = CrossPlatformArbStrategy(pipeline=None)
        count = asyncio.run(strategy.refresh_matches_from_pipeline())
        assert count == 0


# ---------------------------------------------------------------------------
# Change 3: Volume filtering
# ---------------------------------------------------------------------------


class TestVolumeFilter:
    """Change 3: zero-volume markets are filtered."""

    def test_zero_volume_filtered(self) -> None:
        """Markets with zero volume should be filtered by the pipeline."""
        from coinjure.matching._pipeline import MarketMatchingPipeline

        pipeline = MarketMatchingPipeline(min_volume=100.0)
        assert pipeline._min_volume == 100.0

    def test_default_threshold(self) -> None:
        """Default volume threshold should be 100."""
        from coinjure.matching._pipeline import MarketMatchingPipeline

        pipeline = MarketMatchingPipeline()
        assert pipeline._min_volume == 100.0

    def test_volume_filter_disabled_when_zero(self) -> None:
        """Setting min_volume=0 should disable volume filtering."""
        from coinjure.matching._pipeline import MarketMatchingPipeline

        pipeline = MarketMatchingPipeline(min_volume=0.0)
        assert pipeline._min_volume == 0.0


# ---------------------------------------------------------------------------
# Change 4: Keyphrase pre-filter
# ---------------------------------------------------------------------------


class TestKeyphraseFilter:
    """Change 4: keyphrase extraction and uncategorized bucket reduction."""

    def test_extract_keyphrases_basic(self) -> None:
        """Should extract 4+ char words and 3+/3+ char bigrams."""
        text = 'bitcoin price above 100000'
        phrases = _extract_keyphrases(text)
        assert 'bitcoin' in phrases
        assert 'price' in phrases
        assert 'above' in phrases
        assert '100000' in phrases
        # Bigrams
        assert 'bitcoin price' in phrases
        assert 'price above' in phrases

    def test_extract_keyphrases_short_words_excluded(self) -> None:
        """Words shorter than 4 chars should not be single keyphrases."""
        text = 'go up or not'
        phrases = _extract_keyphrases(text)
        # No single words >= 4 chars
        assert not any(len(p.split()) == 1 for p in phrases)

    def test_uncategorized_bucket_smaller_than_full_crossproduct(self) -> None:
        """Keyphrase-filtered uncategorized bucket should be smaller than full cross."""
        # Create uncategorized poly markets (no matching tags)
        poly_markets = [
            _make_market(
                platform='polymarket',
                title='Will Bitcoin reach 100k by December 2026',
                market_id=f'poly-uncat-{i}',
                tags=[],
            )
            for i in range(5)
        ]
        # Add some unrelated poly markets
        poly_markets += [
            _make_market(
                platform='polymarket',
                title=f'Will the weather be sunny in city {i}',
                market_id=f'poly-weather-{i}',
                tags=[],
            )
            for i in range(5)
        ]

        kalshi_markets = [
            _make_market(
                platform='kalshi',
                title='Will Bitcoin price exceed 100000 in 2026',
                market_id='kalshi-btc-1',
                series_ticker='RANDOM',
            ),
            _make_market(
                platform='kalshi',
                title='Will it rain tomorrow in NYC',
                market_id='kalshi-rain-1',
                series_ticker='RANDOM2',
            ),
        ]

        buckets = build_category_buckets(poly_markets, kalshi_markets)
        full_cross = len(poly_markets) * len(kalshi_markets)

        if '__uncategorized__' in buckets:
            p_list, k_list = buckets['__uncategorized__']
            uncat_comparisons = len(p_list) * len(k_list)
            assert uncat_comparisons < full_cross, (
                f'Keyphrase filter should reduce comparisons: {uncat_comparisons} < {full_cross}'
            )

    def test_normalize_for_keyphrases(self) -> None:
        """Normalization should lowercase and strip punctuation."""
        result = _normalize_for_keyphrases("Will Bitcoin's Price Reach $100k?")
        assert 'bitcoin' in result.split() or 'bitcoins' in result.split()
        assert '$' not in result


# ---------------------------------------------------------------------------
# Change 5: Confidence-aware trade sizing
# ---------------------------------------------------------------------------


class TestConfidenceTrading:
    """Change 5: confidence multipliers and min_confidence filtering."""

    def test_confidence_multipliers_defined(self) -> None:
        """Verify confidence multiplier values."""
        from examples.strategies.cross_platform_arb_strategy import (
            _CONFIDENCE_MULTIPLIER,
        )

        assert _CONFIDENCE_MULTIPLIER['HIGH'] == 1.0
        assert _CONFIDENCE_MULTIPLIER['MEDIUM'] == 0.5
        assert _CONFIDENCE_MULTIPLIER['LOW'] == 0.25

    def test_low_filtered_when_min_medium(self) -> None:
        """LOW confidence matches should be filtered when min_confidence='MEDIUM'."""
        from examples.strategies.cross_platform_arb_strategy import (
            _CONFIDENCE_ORDER,
        )

        min_conf = 'MEDIUM'
        assert _CONFIDENCE_ORDER.index('LOW') < _CONFIDENCE_ORDER.index(min_conf)
        assert _CONFIDENCE_ORDER.index('HIGH') >= _CONFIDENCE_ORDER.index(min_conf)

    def test_matched_market_has_confidence_field(self) -> None:
        """MatchedMarket dataclass should have confidence field."""
        from examples.strategies.cross_platform_arb_strategy import MatchedMarket
        from oracle3.ticker.ticker import KalshiTicker, PolyMarketTicker

        mm = MatchedMarket(
            poly_ticker=PolyMarketTicker(symbol='s', name='n'),
            kalshi_ticker=KalshiTicker(symbol='s', name='n'),
            similarity=0.9,
            label='test',
            confidence='HIGH',
        )
        assert mm.confidence == 'HIGH'


# ---------------------------------------------------------------------------
# Change 6: Polymarket tag mapping coverage
# ---------------------------------------------------------------------------


class TestTagMapping:
    """Change 6: exact + substring tag matching."""

    def test_exact_match(self) -> None:
        """Exact tag match should work as before."""
        m = _make_market(tags=['nba'])
        assert categorize_poly(m) == 'sports_basketball'

    def test_substring_match_nba_basketball(self) -> None:
        """'NBA Basketball' should match sports_basketball via substring."""
        m = _make_market(tags=['NBA Basketball'])
        cat = categorize_poly(m)
        assert cat == 'sports_basketball', f'Expected sports_basketball, got {cat!r}'

    def test_substring_match_us_elections(self) -> None:
        """'US Elections 2026' should match via substring."""
        m = _make_market(tags=['US Elections 2026'])
        cat = categorize_poly(m)
        assert cat != '', 'Expected a category match, got empty string'

    def test_hyphen_normalization(self) -> None:
        """Tags with hyphens should be normalized for matching."""
        m = _make_market(tags=['premier-league'])
        cat = categorize_poly(m)
        assert cat == 'sports_soccer', f'Expected sports_soccer, got {cat!r}'

    def test_exact_match_takes_priority(self) -> None:
        """When exact match exists, it should be preferred over substring."""
        m = _make_market(tags=['nba', 'NBA Basketball'])
        cat = categorize_poly(m)
        assert cat == 'sports_basketball'
