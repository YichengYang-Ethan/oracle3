"""Stage 1: Category bucketing to reduce search space."""

from __future__ import annotations

import re

from ._types import NormalizedMarket

# ---- Stopwords for keyphrase extraction ----

_KP_STOPWORDS = frozenset(
    {
        'will', 'the', 'a', 'an', 'of', 'in', 'on', 'by', 'to', 'for',
        'be', 'is', 'at', 'before', 'after', 'this', 'that', 'or', 'and',
        'does', 'do', 'it', 'its', 'than', 'what', 'which', 'who',
    }
)


def _normalize_for_keyphrases(text: str) -> str:
    """Lower, strip punctuation, remove stopwords."""
    text = re.sub(r'[^a-z0-9\s]', ' ', text.lower())
    tokens = [t for t in text.split() if t not in _KP_STOPWORDS]
    return ' '.join(tokens)


def _extract_keyphrases(text_norm: str) -> set[str]:
    """Extract single keywords (4+ chars) and bigrams (3+ chars each)."""
    words = text_norm.split()
    phrases: set[str] = set()
    for w in words:
        if len(w) >= 4:
            phrases.add(w)
    for i in range(len(words) - 1):
        if len(words[i]) >= 3 and len(words[i + 1]) >= 3:
            phrases.add(f'{words[i]} {words[i + 1]}')
    return phrases

# ---- Polymarket tag -> canonical category ----

_POLY_TAG_MAP: dict[str, str] = {
    # Sports
    'nba': 'sports_basketball',
    'basketball': 'sports_basketball',
    'nfl': 'sports_football',
    'football': 'sports_football',
    'mlb': 'sports_baseball',
    'baseball': 'sports_baseball',
    'nhl': 'sports_hockey',
    'hockey': 'sports_hockey',
    'soccer': 'sports_soccer',
    'mls': 'sports_soccer',
    'premier-league': 'sports_soccer',
    'champions-league': 'sports_soccer',
    'tennis': 'sports_tennis',
    'golf': 'sports_golf',
    'pga': 'sports_golf',
    'mma': 'sports_mma',
    'ufc': 'sports_mma',
    'boxing': 'sports_boxing',
    'f1': 'sports_f1',
    'formula-1': 'sports_f1',
    'sports': 'sports',
    # Politics
    'us-elections': 'politics_us_elections',
    'elections': 'politics_elections',
    'politics': 'politics',
    'congress': 'politics_congress',
    'senate': 'politics_congress',
    'house': 'politics_congress',
    'governor': 'politics_governor',
    'presidential': 'politics_presidential',
    'trump': 'politics',
    'biden': 'politics',
    # Crypto
    'crypto': 'crypto',
    'bitcoin': 'crypto_btc',
    'btc': 'crypto_btc',
    'ethereum': 'crypto_eth',
    'eth': 'crypto_eth',
    'solana': 'crypto_sol',
    'sol': 'crypto_sol',
    # Economy
    'fed': 'economy_fed',
    'federal-reserve': 'economy_fed',
    'interest-rates': 'economy_fed',
    'inflation': 'economy_inflation',
    'cpi': 'economy_inflation',
    'gdp': 'economy_gdp',
    'economy': 'economy',
    'jobs': 'economy_jobs',
    'unemployment': 'economy_jobs',
    # Tech / AI
    'ai': 'tech_ai',
    'artificial-intelligence': 'tech_ai',
    'tech': 'tech',
    # Entertainment
    'oscars': 'entertainment_awards',
    'grammys': 'entertainment_awards',
    'emmys': 'entertainment_awards',
    'entertainment': 'entertainment',
    # Science / weather
    'weather': 'science_weather',
    'climate': 'science_weather',
    'science': 'science',
    'space': 'science_space',
}

# ---- Kalshi series_ticker prefix -> canonical category ----

_KALSHI_PREFIX_MAP: dict[str, str] = {
    # Sports
    'KXNBA': 'sports_basketball',
    'KXNFL': 'sports_football',
    'KXMLB': 'sports_baseball',
    'KXNHL': 'sports_hockey',
    'KXMLS': 'sports_soccer',
    'KXUFC': 'sports_mma',
    'KXPGA': 'sports_golf',
    'KXF1': 'sports_f1',
    'KXTENNIS': 'sports_tennis',
    'KXBOX': 'sports_boxing',
    'KXSPORT': 'sports',
    # Politics
    'KXSENATE': 'politics_congress',
    'KXHOUSE': 'politics_congress',
    'KXGOV': 'politics_governor',
    'KXPRES': 'politics_presidential',
    'KXELECT': 'politics_elections',
    'KXPOLITICS': 'politics',
    # Crypto
    'KXBTC': 'crypto_btc',
    'KXETH': 'crypto_eth',
    'KXSOL': 'crypto_sol',
    'KXCRYPTO': 'crypto',
    # Economy
    'KXFED': 'economy_fed',
    'KXRATE': 'economy_fed',
    'KXCPI': 'economy_inflation',
    'KXINFLATION': 'economy_inflation',
    'KXGDP': 'economy_gdp',
    'KXJOBS': 'economy_jobs',
    'KXECON': 'economy',
    # Tech / AI
    'KXAI': 'tech_ai',
    'KXTECH': 'tech',
    # Entertainment
    'KXOSCARS': 'entertainment_awards',
    'KXAWARD': 'entertainment_awards',
    # Science / weather
    'KXWEATHER': 'science_weather',
    'KXTEMP': 'science_weather',
    'KXSPACE': 'science_space',
}

# ---- Kalshi series to exclude (parlays / cross-category) ----

EXCLUDED_KALSHI_SERIES: frozenset[str] = frozenset({
    'KXMVECROSSCATEGORY',
    'KXMVESPORTSMULTIGAMEEXTENDED',
    'KXMVESPORTSMULTIGAME',
    'KXMVEPOLITICSMULTI',
})


def _parent_category(cat: str) -> str:
    """Return the parent category (e.g. 'sports_basketball' -> 'sports')."""
    return cat.rsplit('_', 1)[0] if '_' in cat else cat


def categories_compatible(cat_a: str, cat_b: str) -> bool:
    """Check if two categories are compatible (same or parent-child).

    'sports_basketball' and 'sports' are compatible (parent-child).
    'sports_basketball' and 'sports_football' are NOT compatible (siblings).
    """
    if not cat_a or not cat_b:
        return True  # unknown category -> don't filter
    if cat_a == cat_b:
        return True
    # Parent-child compatibility (one is the parent of the other)
    if _parent_category(cat_a) == cat_b or _parent_category(cat_b) == cat_a:
        return True
    # Two subcategories of the same parent are NOT compatible
    return False


def categorize_poly(market: NormalizedMarket) -> str:
    """Map a Polymarket market to its canonical category.

    First tries exact match, then falls back to substring matching
    (e.g. "NBA Basketball" matches via "nba" or "basketball").
    """
    # Pass 1: exact match
    for tag in market.tags:
        tag_lower = tag.lower().strip()
        if tag_lower in _POLY_TAG_MAP:
            return _POLY_TAG_MAP[tag_lower]

    # Pass 2: substring match (normalized: remove hyphens, split tokens)
    for tag in market.tags:
        tag_normalized = tag.lower().strip().replace('-', ' ')
        for token in tag_normalized.split():
            if token in _POLY_TAG_MAP:
                return _POLY_TAG_MAP[token]
        # Also try the full normalized tag as a substring of known keys
        for known_tag, category in _POLY_TAG_MAP.items():
            known_normalized = known_tag.replace('-', ' ')
            if known_normalized in tag_normalized or tag_normalized in known_normalized:
                return category
    return ''


def categorize_kalshi(market: NormalizedMarket) -> str:
    """Map a Kalshi market to its canonical category via series_ticker prefix."""
    series = market.series_ticker.upper()
    if series in EXCLUDED_KALSHI_SERIES:
        return '__excluded__'
    # Try longest prefix first
    for prefix in sorted(_KALSHI_PREFIX_MAP, key=len, reverse=True):
        if series.startswith(prefix):
            return _KALSHI_PREFIX_MAP[prefix]
    return ''


def categorize_market(market: NormalizedMarket) -> str:
    """Assign canonical category to a market based on its platform."""
    if market.platform == 'polymarket':
        return categorize_poly(market)
    if market.platform == 'kalshi':
        return categorize_kalshi(market)
    return ''


def build_category_buckets(
    poly_markets: list[NormalizedMarket],
    kalshi_markets: list[NormalizedMarket],
) -> dict[str, tuple[list[NormalizedMarket], list[NormalizedMarket]]]:
    """Group markets into buckets by compatible category.

    Returns a dict of category -> (poly_list, kalshi_list).
    Markets with no category go into a special '' bucket that is compared
    against all Kalshi markets (and vice-versa).
    """
    poly_by_cat: dict[str, list[NormalizedMarket]] = {}
    kalshi_by_cat: dict[str, list[NormalizedMarket]] = {}

    for m in poly_markets:
        cat = categorize_market(m)
        m.category = cat
        poly_by_cat.setdefault(cat, []).append(m)

    kalshi_filtered: list[NormalizedMarket] = []
    for m in kalshi_markets:
        cat = categorize_market(m)
        m.category = cat
        if cat == '__excluded__':
            continue
        kalshi_filtered.append(m)
        kalshi_by_cat.setdefault(cat, []).append(m)

    # Build compatible buckets
    buckets: dict[str, tuple[list[NormalizedMarket], list[NormalizedMarket]]] = {}

    all_cats = set(poly_by_cat) | set(kalshi_by_cat)
    for cat in all_cats:
        if cat == '__excluded__':
            continue
        p_list = poly_by_cat.get(cat, [])
        k_list = kalshi_by_cat.get(cat, [])
        if p_list and k_list:
            buckets[cat] = (p_list, k_list)

    # Uncategorized markets: use keyphrase overlap to avoid O(n*m) blowup
    uncategorized_poly = poly_by_cat.get('', [])
    uncategorized_kalshi = kalshi_by_cat.get('', [])

    if uncategorized_poly or uncategorized_kalshi:
        # Build keyphrase inverted index on Kalshi side
        kalshi_by_phrase: dict[str, list[NormalizedMarket]] = {}
        kalshi_candidates = kalshi_filtered if uncategorized_poly else []
        all_poly_list = [m for ms in poly_by_cat.values() for m in ms] if uncategorized_kalshi else []

        for km in (kalshi_candidates if uncategorized_poly else []) + uncategorized_kalshi:
            norm = _normalize_for_keyphrases(km.title)
            for phrase in _extract_keyphrases(norm):
                kalshi_by_phrase.setdefault(phrase, []).append(km)

        matched_poly: set[int] = set()
        matched_kalshi: set[int] = set()
        uncat_poly_list: list[NormalizedMarket] = []
        uncat_kalshi_list: list[NormalizedMarket] = []

        # Match uncategorized poly against kalshi via shared keyphrases
        for pm in uncategorized_poly:
            norm = _normalize_for_keyphrases(pm.title)
            phrases = _extract_keyphrases(norm)
            # Count how many keyphrases each kalshi market shares
            kalshi_hits: dict[int, int] = {}
            for phrase in phrases:
                for km in kalshi_by_phrase.get(phrase, []):
                    kalshi_hits[id(km)] = kalshi_hits.get(id(km), 0) + 1
            # Require >=2 shared keyphrases
            has_match = False
            for km_id, count in kalshi_hits.items():
                if count >= 2:
                    matched_kalshi.add(km_id)
                    has_match = True
            if has_match:
                matched_poly.add(id(pm))
                uncat_poly_list.append(pm)

        # Match uncategorized kalshi against all poly via shared keyphrases
        poly_by_phrase: dict[str, list[NormalizedMarket]] = {}
        for pm in all_poly_list:
            norm = _normalize_for_keyphrases(pm.title)
            for phrase in _extract_keyphrases(norm):
                poly_by_phrase.setdefault(phrase, []).append(pm)

        for km in uncategorized_kalshi:
            norm = _normalize_for_keyphrases(km.title)
            phrases = _extract_keyphrases(norm)
            poly_hits: dict[int, int] = {}
            for phrase in phrases:
                for pm in poly_by_phrase.get(phrase, []):
                    poly_hits[id(pm)] = poly_hits.get(id(pm), 0) + 1
            has_match = False
            for pm_id, count in poly_hits.items():
                if count >= 2:
                    matched_poly.add(pm_id)
                    has_match = True
            if has_match:
                matched_kalshi.add(id(km))
                if id(km) not in {id(k) for k in uncat_kalshi_list}:
                    uncat_kalshi_list.append(km)

        # Collect all matched kalshi markets for the bucket
        all_kalshi_pool = (kalshi_candidates if uncategorized_poly else []) + uncategorized_kalshi
        kalshi_in_bucket = list({id(k): k for k in all_kalshi_pool if id(k) in matched_kalshi}.values())
        # Collect all matched poly markets for the bucket
        poly_in_bucket_extra = [pm for pm in all_poly_list if id(pm) in matched_poly and id(pm) not in {id(p) for p in uncat_poly_list}]
        final_poly = list({id(p): p for p in uncat_poly_list + poly_in_bucket_extra}.values())

        if final_poly and kalshi_in_bucket:
            buckets['__uncategorized__'] = (final_poly, kalshi_in_bucket)

    return buckets
