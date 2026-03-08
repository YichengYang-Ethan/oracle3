"""Persistent market relation graph -- stores discovered spread pairs.

Adapted from prediction-market-cli's relation system for oracle3's
prediction-market trading engine.  Stores a graph of relationships
between markets (tickers) that can be validated, traded, and retired
through a well-defined lifecycle.

Relation types (the 4+4 taxonomy):
  - same_event      : identical outcome across platforms (cross-platform arb)
  - cross_platform  : same underlying, different platform mechanics
  - implication     : A implies B  (p_A <= p_B always)
  - exclusivity     : A and B mutually exclusive  (p_A + p_B <= 1)
  - conditional     : p(A|B) is structurally constrained
  - structural      : other deterministic constraint
  - cointegration   : statistically mean-reverting spread
  - complement      : outcomes sum to 1 within an event

Status lifecycle:
  discovered -> validated -> deployed -> retired
                         \\-> invalidated
"""

from __future__ import annotations

import builtins
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

RELATIONS_DIR = Path.home() / ".oracle3"
RELATIONS_PATH = RELATIONS_DIR / "relations.json"

SPREAD_TYPES = frozenset(
    {
        "same_event",
        "cross_platform",
        "implication",
        "exclusivity",
        "conditional",
        "structural",
        "cointegration",
        "complement",
    }
)

STATUS_LIFECYCLE = frozenset(
    {
        "discovered",
        "validated",
        "deployed",
        "retired",
        "invalidated",
    }
)


# ── ValidationResult ─────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Quantitative validation result for a market relation.

    Fields are optional because different analysis types populate
    different subsets (structural vs. cointegration vs. lead-lag).
    """

    # Analysis type that produced this result
    analysis_type: str | None = None  # 'structural', 'cointegration', 'lead_lag'

    # Structural analysis (same_event, complement, implication, exclusivity)
    constraint: str | None = None  # e.g. 'A <= B', 'A + B <= 1'
    constraint_holds: bool | None = None
    violation_count: int | None = None
    violation_rate: float | None = None  # fraction of observations violating
    current_arb: float | None = None  # current constraint violation size
    mean_arb: float | None = None  # mean violation size when violated

    # Stationarity (ADF test on spread)
    adf_statistic: float | None = None
    adf_pvalue: float | None = None
    is_stationary: bool | None = None

    # Cointegration (Engle-Granger)
    coint_statistic: float | None = None
    coint_pvalue: float | None = None
    is_cointegrated: bool | None = None

    # Spread characteristics
    half_life: float | None = None  # bars to mean-revert
    hedge_ratio: float | None = None  # beta from OLS
    correlation: float | None = None
    mean_spread: float | None = None
    std_spread: float | None = None

    # Lead-lag
    lead_lag: int | None = None  # positive = A leads B by N steps
    lead_lag_corr: float | None = None  # cross-correlation at optimal lag
    lead_lag_significant: bool | None = None  # |corr| > threshold

    validated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def is_valid(self) -> bool:
        """Check validity based on the analysis type.

        For structural relations, the logical relationship is always valid --
        constraint violations are trading opportunities, not evidence that the
        relation is wrong.
        """
        if self.analysis_type == "structural":
            return True
        if self.analysis_type == "lead_lag":
            return self.lead_lag_significant is True
        if self.is_cointegrated is not None:
            return self.is_cointegrated
        if self.is_stationary is not None:
            return self.is_stationary
        return False


# ── MarketRelation ───────────────────────────────────────────────────────


@dataclass
class MarketRelation:
    """A discovered relationship between two prediction markets.

    ``market_a`` and ``market_b`` are dicts that hold ticker-identifying
    information (symbol, token_id, market_id, platform, name, etc.).
    This is intentionally untyped to support heterogeneous ticker types
    across Polymarket, Kalshi, DFlow, etc.
    """

    relation_id: str

    # Market references (serialized ticker info)
    market_a: dict[str, Any] = field(default_factory=dict)
    market_b: dict[str, Any] = field(default_factory=dict)

    # Relation classification
    spread_type: str = "unknown"
    confidence: float = 0.0
    reasoning: str = ""

    # Quantitative hypothesis (set by discovery / analysis)
    hypothesis: str = ""  # e.g. "p_A - p_B ~ 0"
    hedge_ratio: float = 1.0  # beta from OLS: p_A = alpha + beta * p_B
    lead_lag: int = 0  # positive = A leads B by N steps

    # Per-market analysis snapshots (set by analyze commands)
    analysis_a: dict[str, Any] = field(default_factory=dict)
    analysis_b: dict[str, Any] = field(default_factory=dict)

    # Quantitative validation (set by validate command)
    validation: dict[str, Any] = field(default_factory=dict)

    # Lifecycle
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_validated: str | None = None
    valid_until: str | None = None
    status: str = "discovered"  # discovered -> validated -> deployed -> retired | invalidated

    def __post_init__(self) -> None:
        if self.spread_type != "unknown" and self.spread_type not in SPREAD_TYPES:
            raise ValueError(
                f"Invalid spread_type {self.spread_type!r}. "
                f"Must be one of: {sorted(SPREAD_TYPES)}"
            )
        if self.status not in STATUS_LIFECYCLE:
            raise ValueError(
                f"Invalid status {self.status!r}. "
                f"Must be one of: {sorted(STATUS_LIFECYCLE)}"
            )

    # ── Validation helpers ────────────────────────────────────────────

    def set_validation(self, result: ValidationResult) -> None:
        """Store a validation result and update lifecycle + trading fields."""
        self.validation = asdict(result)
        self.last_validated = result.validated_at
        if result.is_valid:
            self.status = "validated"
        else:
            self.status = "invalidated"
        # Propagate hedge ratio and lead-lag (relatively stable across windows)
        if result.hedge_ratio is not None:
            self.hedge_ratio = result.hedge_ratio
        if result.lead_lag is not None:
            self.lead_lag = result.lead_lag

    def get_validation(self) -> ValidationResult | None:
        """Reconstruct a ValidationResult from the stored dict, if any."""
        if not self.validation:
            return None
        known = {f.name for f in ValidationResult.__dataclass_fields__.values()}
        return ValidationResult(
            **{k: v for k, v in self.validation.items() if k in known}
        )

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MarketRelation:
        """Construct from a dict, ignoring unknown keys for forward compat."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


# ── RelationStore ────────────────────────────────────────────────────────


class RelationStore:
    """JSON-backed persistent store for market relations.

    Data is stored at ``~/.oracle3/relations.json`` by default,
    following oracle3's ``StateStore`` pattern of atomic writes
    via temp-file rename.
    """

    def __init__(self, path: Path = RELATIONS_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    # ── Low-level I/O (atomic write) ──────────────────────────────────

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load relations from %s: %s", self._path, exc)
            return []

    def _save(self, data: list[dict[str, Any]]) -> None:
        """Atomic write: write to .tmp then rename."""
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.rename(self._path)
        except Exception:
            logger.exception("Failed to write relations to %s", self._path)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ── CRUD ──────────────────────────────────────────────────────────

    def add(self, relation: MarketRelation) -> None:
        """Add a relation, deduplicating by relation_id."""
        data = self._load()
        data = [d for d in data if d.get("relation_id") != relation.relation_id]
        data.append(relation.to_dict())
        self._save(data)

    def get(self, relation_id: str) -> MarketRelation | None:
        """Retrieve a single relation by ID, or None if not found."""
        for d in self._load():
            if d.get("relation_id") == relation_id:
                return MarketRelation.from_dict(d)
        return None

    def update(self, relation: MarketRelation) -> None:
        """Update an existing relation in-place, or add if not found."""
        data = self._load()
        for i, d in enumerate(data):
            if d.get("relation_id") == relation.relation_id:
                data[i] = relation.to_dict()
                self._save(data)
                return
        # Not found -- add
        data.append(relation.to_dict())
        self._save(data)

    def remove(self, relation_id: str) -> bool:
        """Remove a relation by ID. Returns True if something was removed."""
        data = self._load()
        before = len(data)
        data = [d for d in data if d.get("relation_id") != relation_id]
        if len(data) < before:
            self._save(data)
            return True
        return False

    # ── List / Filter ─────────────────────────────────────────────────

    def list(
        self,
        spread_type: str | None = None,
        status: str | None = None,
    ) -> list[MarketRelation]:
        """List all relations, optionally filtered by type and/or status."""
        raw = self._load()
        relations = [MarketRelation.from_dict(d) for d in raw]
        if spread_type is not None:
            relations = [r for r in relations if r.spread_type == spread_type]
        if status is not None:
            relations = [r for r in relations if r.status == status]
        return relations

    def list_by_type(self, spread_type: str) -> builtins.list[MarketRelation]:
        """Convenience: list relations of a specific spread type."""
        return self.list(spread_type=spread_type)

    def list_by_status(self, status: str) -> builtins.list[MarketRelation]:
        """Convenience: list relations with a specific status."""
        return self.list(status=status)

    # ── Graph queries ─────────────────────────────────────────────────

    def find_by_market(self, market_id: str) -> builtins.list[MarketRelation]:
        """Return all relations involving a given market (by any id field)."""
        results: builtins.list[MarketRelation] = []
        for r in self.list():
            a_ids = {
                r.market_a.get("market_id", ""),
                r.market_a.get("id", ""),
                r.market_a.get("symbol", ""),
                r.market_a.get("ticker", ""),
                r.market_a.get("token_id", ""),
            }
            b_ids = {
                r.market_b.get("market_id", ""),
                r.market_b.get("id", ""),
                r.market_b.get("symbol", ""),
                r.market_b.get("ticker", ""),
                r.market_b.get("token_id", ""),
            }
            if market_id in a_ids or market_id in b_ids:
                results.append(r)
        return results

    def strongest(
        self, n: int = 10, status: str | None = None
    ) -> builtins.list[MarketRelation]:
        """Return the N highest-confidence relations."""
        relations = self.list(status=status)
        relations.sort(key=lambda r: r.confidence, reverse=True)
        return relations[:n]

    def validated(self) -> builtins.list[MarketRelation]:
        """Return relations that passed quantitative validation."""
        return self.list(status="validated")

    def deployed(self) -> builtins.list[MarketRelation]:
        """Return relations currently deployed for trading."""
        return self.list(status="deployed")

    # ── Lifecycle transitions ─────────────────────────────────────────

    def deploy(self, relation_id: str) -> bool:
        """Transition a validated relation to deployed status."""
        r = self.get(relation_id)
        if r is None:
            return False
        if r.status != "validated":
            logger.warning(
                "Cannot deploy relation %s: status is %r (expected 'validated')",
                relation_id,
                r.status,
            )
            return False
        r.status = "deployed"
        self.update(r)
        return True

    def invalidate(self, relation_id: str, reason: str = "") -> bool:
        """Mark a relation as invalidated."""
        r = self.get(relation_id)
        if r is None:
            return False
        r.status = "invalidated"
        if reason:
            r.reasoning = f"{r.reasoning} [invalidated: {reason}]"
        self.update(r)
        return True

    def retire(self, relation_id: str) -> bool:
        """Mark a relation as retired (end of lifecycle)."""
        r = self.get(relation_id)
        if r is None:
            return False
        r.status = "retired"
        self.update(r)
        return True
