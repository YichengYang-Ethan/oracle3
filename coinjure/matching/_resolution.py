"""Stage 5: Resolution rule compatibility check."""

from __future__ import annotations

from ._types import NormalizedMarket

# Known resolution sources (normalized lowercase)
_OFFICIAL_SOURCES = frozenset({
    'ap', 'associated press', 'reuters', 'official results',
    'nba.com', 'nfl.com', 'mlb.com', 'espn',
    'federal reserve', 'bls', 'bea', 'coinmarketcap', 'coingecko',
})


def _extract_resolution_source(market: NormalizedMarket) -> str:
    """Extract resolution source from market metadata."""
    src = market.resolution_source.strip().lower()
    if src:
        return src
    # Try to infer from extra metadata
    rules = str(market.extra.get('rules', '')).lower()
    desc = str(market.extra.get('description', '')).lower()
    text = rules + ' ' + desc
    for source in _OFFICIAL_SOURCES:
        if source in text:
            return source
    return ''


def check_resolution_compatibility(
    poly_market: NormalizedMarket,
    kalshi_market: NormalizedMarket,
) -> tuple[bool | None, list[str]]:
    """Check if two markets have compatible resolution rules.

    Returns:
        (compatible, warnings) where compatible is True/False/None (unknown).
    """
    warnings: list[str] = []

    poly_src = _extract_resolution_source(poly_market)
    kalshi_src = _extract_resolution_source(kalshi_market)

    # Date difference check
    if poly_market.end_date and kalshi_market.end_date:
        delta = abs((poly_market.end_date - kalshi_market.end_date).days)
        if delta > 7:
            warnings.append(
                f'End dates differ by {delta} days '
                f'(Poly: {poly_market.end_date.date()}, '
                f'Kalshi: {kalshi_market.end_date.date()})'
            )

    # Source comparison
    if not poly_src or not kalshi_src:
        return None, warnings  # unknown

    if poly_src == kalshi_src:
        return True, warnings

    # Both have sources but they differ
    warnings.append(
        f'Different resolution sources: '
        f'Poly="{poly_src}", Kalshi="{kalshi_src}"'
    )
    return False, warnings
