"""Stage 4: Improved text similarity matching."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_STOPWORDS = frozenset(
    {
        'will', 'the', 'a', 'an', 'of', 'in', 'on', 'by', 'to', 'for',
        'be', 'is', 'at', 'before', 'after', 'this', 'that', 'or', 'and',
        'does', 'do', 'it', 'its', 'than', 'what', 'which', 'who',
    }
)

MIN_TEXT_SIMILARITY = 0.45


def normalize_text(text: str) -> str:
    """Lower, strip punctuation, remove stopwords."""
    text = re.sub(r'[^a-z0-9\s]', ' ', text.lower())
    tokens = [t for t in text.split() if t not in _STOPWORDS]
    return ' '.join(tokens)


def _jaccard_similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity."""
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _sorted_token_ratio(a: str, b: str) -> float:
    """SequenceMatcher on sorted tokens (order-invariant)."""
    sorted_a = ' '.join(sorted(a.split()))
    sorted_b = ' '.join(sorted(b.split()))
    return SequenceMatcher(None, sorted_a, sorted_b).ratio()


def text_similarity(title_a: str, title_b: str) -> float:
    """Compute text similarity using the best of 3 methods.

    1. SequenceMatcher ratio (original text)
    2. Jaccard token-set similarity
    3. SequenceMatcher on sorted tokens (order-invariant)

    Returns the maximum score.
    """
    norm_a = normalize_text(title_a)
    norm_b = normalize_text(title_b)

    if not norm_a or not norm_b:
        return 0.0

    seq_score = SequenceMatcher(None, norm_a, norm_b).ratio()
    jaccard_score = _jaccard_similarity(norm_a, norm_b)
    sorted_score = _sorted_token_ratio(norm_a, norm_b)

    return max(seq_score, jaccard_score, sorted_score)
