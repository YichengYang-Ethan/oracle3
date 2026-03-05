"""Data types for the market matching pipeline."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class MatchConfidence(enum.Enum):
    """Confidence level for a cross-platform market match."""

    HIGH = 'HIGH'
    MEDIUM = 'MEDIUM'
    LOW = 'LOW'


@dataclass
class NormalizedMarket:
    """Platform-agnostic representation of a prediction market."""

    platform: str  # 'polymarket' or 'kalshi'
    event_id: str
    market_id: str
    title: str
    normalized_title: str = ''
    category: str = ''
    end_date: datetime | None = None
    tags: list[str] = field(default_factory=list)
    series_ticker: str = ''
    resolution_source: str = ''
    extra: dict[str, object] = field(default_factory=dict)

    def get_extra_str(self, key: str, default: str = '') -> str:
        """Get a string value from extra metadata."""
        return str(self.extra.get(key, default))

    def get_extra_float(self, key: str) -> float | None:
        """Get a float value from extra metadata, or None."""
        val = self.extra.get(key)
        if val is None:
            return None
        try:
            return float(val)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return None


@dataclass
class StageScores:
    """Per-stage scores for a match pair."""

    category: float = 0.0
    date: float = 0.0
    template: float = 0.0
    text: float = 0.0
    resolution: float = 0.0


@dataclass
class MatchResult:
    """A scored match between a Polymarket and Kalshi market."""

    poly_market: NormalizedMarket
    kalshi_market: NormalizedMarket
    confidence: MatchConfidence
    score: float
    stage_scores: StageScores
    label: str = ''
    spread: float | None = None
    warnings: list[str] = field(default_factory=list)
    resolution_compatible: bool | None = None
