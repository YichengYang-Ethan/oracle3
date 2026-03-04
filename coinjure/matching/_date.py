"""Stage 2: Date proximity filtering."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ._types import NormalizedMarket

# ---- Regex patterns for date extraction from titles ----

_MONTH_MAP: dict[str, int] = {
    'january': 1, 'jan': 1,
    'february': 2, 'feb': 2,
    'march': 3, 'mar': 3,
    'april': 4, 'apr': 4,
    'may': 5,
    'june': 6, 'jun': 6,
    'july': 7, 'jul': 7,
    'august': 8, 'aug': 8,
    'september': 9, 'sep': 9, 'sept': 9,
    'october': 10, 'oct': 10,
    'november': 11, 'nov': 11,
    'december': 12, 'dec': 12,
}

# "by December 31, 2025" / "before March 15, 2026"
_FULL_DATE_RE = re.compile(
    r'(?:by|before|on|after)?\s*'
    r'(?P<month>' + '|'.join(_MONTH_MAP) + r')\s+'
    r'(?P<day>\d{1,2}),?\s+'
    r'(?P<year>20\d{2})',
    re.IGNORECASE,
)

# "March 2026" / "in March 2026"
_MONTH_YEAR_RE = re.compile(
    r'(?:in|by|before|during)?\s*'
    r'(?P<month>' + '|'.join(_MONTH_MAP) + r')\s+'
    r'(?P<year>20\d{2})',
    re.IGNORECASE,
)

# "in 2026" / "2026 NBA" / "by 2026"
_YEAR_RE = re.compile(
    r'(?:in|by|before|during)?\s*(?P<year>20\d{2})\b',
    re.IGNORECASE,
)

# "Q1 2026" / "Q3 2025"
_QUARTER_RE = re.compile(
    r'Q(?P<quarter>[1-4])\s+(?P<year>20\d{2})',
    re.IGNORECASE,
)


def extract_date(market: NormalizedMarket) -> datetime | None:
    """Extract the best date estimate from a market.

    Priority: API end_date > full date regex > month+year > quarter > year.
    """
    if market.end_date is not None:
        return market.end_date

    title = market.title

    # Full date: "December 31, 2025"
    m = _FULL_DATE_RE.search(title)
    if m:
        month = _MONTH_MAP.get(m.group('month').lower())
        if month:
            try:
                return datetime(
                    int(m.group('year')),
                    month,
                    int(m.group('day')),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                pass

    # Month + year: "March 2026"
    m = _MONTH_YEAR_RE.search(title)
    if m:
        month = _MONTH_MAP.get(m.group('month').lower())
        if month:
            return datetime(int(m.group('year')), month, 15, tzinfo=timezone.utc)

    # Quarter: "Q1 2026"
    m = _QUARTER_RE.search(title)
    if m:
        quarter = int(m.group('quarter'))
        mid_month = {1: 2, 2: 5, 3: 8, 4: 11}[quarter]
        return datetime(int(m.group('year')), mid_month, 15, tzinfo=timezone.utc)

    # Year only: "2026"
    m = _YEAR_RE.search(title)
    if m:
        return datetime(int(m.group('year')), 7, 1, tzinfo=timezone.utc)

    return None


def date_proximity_score(
    date_a: datetime | None,
    date_b: datetime | None,
    max_delta_days: int = 30,
) -> float:
    """Score date proximity between two markets.

    Returns 1.0 if within 7 days, linearly decays to 0.0 at max_delta_days.
    Returns 0.5 if either date is missing (neutral).
    """
    if date_a is None or date_b is None:
        return 0.5  # no penalty, no bonus

    delta_days = abs((date_a - date_b).days)
    if delta_days <= 7:
        return 1.0
    if delta_days >= max_delta_days:
        return 0.0
    # Linear decay from 1.0 at 7 days to 0.0 at max_delta_days
    return 1.0 - (delta_days - 7) / (max_delta_days - 7)
