"""Stage 3: Template-based structural matching."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ._types import NormalizedMarket

# ---- US states for election template ----

_US_STATES = (
    'alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|'
    'florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|'
    'louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|'
    'missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|'
    'new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|'
    'rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|'
    'virginia|washington|west virginia|wisconsin|wyoming|dc|district of columbia'
)

_RACE_TYPES = r'(?:senate|governor|gubernatorial|house|congressional|presidential)'


@dataclass
class TemplateMatch:
    """Result of matching a market title against a template."""

    template_name: str
    fields: dict[str, str] = field(default_factory=dict)


# ---- Template definitions ----

_TEMPLATES: list[tuple[str, re.Pattern[str], list[str]]] = [
    # 1. Election state race
    (
        'election_state_race',
        re.compile(
            r'(?:will\s+)?(?P<party>democrat|republican|gop|dem|rep)s?\s+'
            r'(?:win|flip|take|carry|hold)\s+'
            r'(?:the\s+)?(?P<state>' + _US_STATES + r')\s+'
            r'(?P<race_type>' + _RACE_TYPES + r')',
            re.IGNORECASE,
        ),
        ['party', 'state', 'race_type'],
    ),
    # 1b. Election state race (state first)
    (
        'election_state_race',
        re.compile(
            r'(?P<state>' + _US_STATES + r')\s+'
            r'(?P<race_type>' + _RACE_TYPES + r')\s+'
            r'(?:race|election|winner)',
            re.IGNORECASE,
        ),
        ['state', 'race_type'],
    ),
    # 2. Crypto price threshold
    (
        'crypto_price_threshold',
        re.compile(
            r'(?:will\s+)?(?P<coin>bitcoin|btc|ethereum|eth|solana|sol|'
            r'dogecoin|doge|xrp|cardano|ada)\s+'
            r'(?:reach|hit|go\s+)?(?:above|below|over|under|exceed)?\s*'
            r'\$?(?P<price>[\d,]+(?:\.\d+)?[kmb]?)',
            re.IGNORECASE,
        ),
        ['coin', 'price'],
    ),
    # 2b. Crypto price threshold (price first)
    (
        'crypto_price_threshold',
        re.compile(
            r'\$(?P<price>[\d,]+(?:\.\d+)?[kmb]?)\s+'
            r'(?P<coin>bitcoin|btc|ethereum|eth|solana|sol)',
            re.IGNORECASE,
        ),
        ['coin', 'price'],
    ),
    # 3. Sports champion
    (
        'sports_champion',
        re.compile(
            r'(?P<year>20\d{2})\s+'
            r'(?P<league>nba|nfl|mlb|nhl|mls|premier\s+league|'
            r'champions\s+league|world\s+series|super\s+bowl|'
            r'stanley\s+cup|world\s+cup)\s*'
            r'(?:finals?\s+)?(?:champion|winner|mvp)',
            re.IGNORECASE,
        ),
        ['year', 'league'],
    ),
    # 4. Sports team event
    (
        'sports_team_event',
        re.compile(
            r'(?:will\s+)?(?:the\s+)?(?P<team>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+'
            r'(?:win|make|reach)\s+'
            r'(?:the\s+)?(?P<event>(?:20\d{2}\s+)?'
            r'(?:nba|nfl|mlb|nhl|world\s+series|super\s+bowl|'
            r'stanley\s+cup|finals?|playoffs?|championship))',
            re.IGNORECASE,
        ),
        ['team', 'event'],
    ),
    # 5. Fed rate decision
    (
        'fed_rate_decision',
        re.compile(
            r'(?:will\s+)?(?:the\s+)?(?:fed|federal\s+reserve|fomc)\s+'
            r'(?P<action>cut|raise|hike|hold|lower|increase|decrease|pause)\s+'
            r'(?:interest\s+)?(?:rates?)?\s*'
            r'(?:in|at|during|by)?\s*'
            r'(?P<month>january|february|march|april|may|june|'
            r'july|august|september|october|november|december|'
            r'jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)?\s*'
            r'(?P<year>20\d{2})?',
            re.IGNORECASE,
        ),
        ['action', 'month', 'year'],
    ),
    # 5b. Fed rate (question form)
    (
        'fed_rate_decision',
        re.compile(
            r'(?P<action>rate\s+cut|rate\s+hike|rate\s+hold|rate\s+pause)\s+'
            r'(?:in|at|during|by)?\s*'
            r'(?P<month>january|february|march|april|may|june|'
            r'july|august|september|october|november|december|'
            r'jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)?\s*'
            r'(?P<year>20\d{2})?',
            re.IGNORECASE,
        ),
        ['action', 'month', 'year'],
    ),
    # 6. Person action
    (
        'person_action',
        re.compile(
            r'(?:will\s+)?(?P<person>trump|biden|harris|desantis|musk|'
            r'xi\s+jinping|putin|zelensky|netanyahu|modi)\s+'
            r'(?P<action>resign|be\s+impeached|be\s+indicted|'
            r'be\s+convicted|win|lose|drop\s+out|pardon|'
            r'be\s+arrested|be\s+removed|step\s+down|announce)',
            re.IGNORECASE,
        ),
        ['person', 'action'],
    ),
]


_PARTY_MAP: dict[str, str] = {
    'dem': 'democrat', 'dems': 'democrat', 'democrat': 'democrat',
    'democrats': 'democrat',
    'rep': 'republican', 'reps': 'republican', 'republican': 'republican',
    'republicans': 'republican', 'gop': 'republican',
}

_COIN_MAP: dict[str, str] = {
    'btc': 'bitcoin', 'eth': 'ethereum', 'sol': 'solana',
    'doge': 'dogecoin', 'ada': 'cardano',
}

_RACE_MAP: dict[str, str] = {'gubernatorial': 'governor', 'congressional': 'house'}

_ACTION_MAP: dict[str, str] = {
    'hike': 'raise', 'raise': 'raise', 'increase': 'raise',
    'cut': 'cut', 'lower': 'cut', 'decrease': 'cut',
    'hold': 'hold', 'pause': 'hold',
}

_PRICE_SUFFIX: dict[str, int] = {'k': 1_000, 'm': 1_000_000, 'b': 1_000_000_000}


def _normalize_field(key: str, value: str) -> str:
    """Normalize a template field value for comparison."""
    v = value.strip().lower()
    if key == 'party':
        return _PARTY_MAP.get(v, v)
    if key == 'coin':
        return _COIN_MAP.get(v, v)
    if key == 'race_type':
        return _RACE_MAP.get(v, v)
    if key == 'action':
        return _ACTION_MAP.get(v.replace('rate ', ''), v)
    if key == 'price':
        v = v.replace(',', '')
        for suffix, mult in _PRICE_SUFFIX.items():
            if v.endswith(suffix):
                return str(int(float(v[:-1]) * mult))
    return v


def match_template(market: NormalizedMarket) -> TemplateMatch | None:
    """Try to match a market title against known templates.

    Returns the first matching template with extracted fields, or None.
    """
    title = market.title
    for template_name, pattern, field_names in _TEMPLATES:
        m = pattern.search(title)
        if m:
            fields = {}
            for name in field_names:
                raw = m.group(name)
                if raw:
                    fields[name] = _normalize_field(name, raw)
            if fields:
                return TemplateMatch(template_name=template_name, fields=fields)
    return None


def template_score(
    poly_match: TemplateMatch | None,
    kalshi_match: TemplateMatch | None,
) -> float:
    """Score template compatibility between two markets.

    Returns:
        1.0 if both match the same template with identical fields.
        -1.0 if both match the same template but fields differ (hard reject).
        0.0 if either has no template match (fall through to text).
    """
    if poly_match is None or kalshi_match is None:
        return 0.0

    if poly_match.template_name != kalshi_match.template_name:
        return 0.0

    # Same template -- compare fields
    shared_keys = set(poly_match.fields) & set(kalshi_match.fields)
    if not shared_keys:
        return 0.0

    for key in shared_keys:
        if poly_match.fields[key] != kalshi_match.fields[key]:
            return -1.0  # hard reject: same template, different fields

    return 1.0
