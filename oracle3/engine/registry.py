"""Strategy Registry -- persistent store for the multi-strategy portfolio.

Tracks all strategy instances across their lifecycle (paper_trading ->
live_trading -> retired) with JSON-file persistence and atomic writes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REGISTRY_DIR = Path.home() / ".oracle3"
REGISTRY_PATH = REGISTRY_DIR / "portfolio.json"

VALID_LIFECYCLES = frozenset({"paper_trading", "live_trading", "retired"})

_FIELDS = {
    "strategy_id",
    "strategy_ref",
    "lifecycle",
    "pnl",
    "socket_path",
    "kwargs",
    "created_at",
    "updated_at",
    "retired_at",
    "retired_reason",
    "pid",
    "exchange",
    "notes",
}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class StrategyEntry:
    """A single strategy instance tracked by the portfolio registry.

    Attributes:
        strategy_id: Unique identifier for this strategy instance.
        strategy_ref: Module path / reference for loading the strategy
            (e.g. ``"strategies/mean_revert.py:MeanRevert"``).
        lifecycle: Current lifecycle stage.  One of ``paper_trading``,
            ``live_trading``, or ``retired``.
        pnl: Most recently recorded PnL as a string (supports Decimal
            round-trip).
        socket_path: Path to the Unix control socket for this instance
            (if running).
        kwargs: Constructor keyword arguments passed when instantiating
            the strategy class.
        created_at: ISO-8601 timestamp of when this entry was created.
        updated_at: ISO-8601 timestamp of the most recent update.
        retired_at: ISO-8601 timestamp of when this entry was retired
            (None if still active).
        retired_reason: Human-readable reason for retirement.
        pid: OS process ID of the running engine (None when not running).
        exchange: Exchange / venue this strategy operates on.
        notes: Free-form notes.
    """

    strategy_id: str
    strategy_ref: str
    lifecycle: str = "paper_trading"
    pnl: str | None = None
    socket_path: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    retired_at: str | None = None
    retired_reason: str | None = None
    pid: int | None = None
    exchange: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON encoding."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyEntry:
        """Deserialize from a dict, ignoring unknown keys."""
        return cls(**{k: v for k, v in d.items() if k in _FIELDS})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class StrategyRegistry:
    """JSON-file-based registry of all portfolio strategies.

    Supports concurrent reads; writes are atomic (write to tmp, then
    rename).

    Usage::

        registry = StrategyRegistry()
        registry.add(StrategyEntry(
            strategy_id="mean-revert-btc-1",
            strategy_ref="strategies/mean_revert.py:MeanRevert",
            lifecycle="paper_trading",
            exchange="polymarket",
        ))

        for entry in registry.list():
            print(entry.strategy_id, entry.lifecycle)

        registry.retire("mean-revert-btc-1", reason="poor performance")
    """

    def __init__(self, path: Path = REGISTRY_PATH) -> None:
        self.path = path
        self._entries: dict[str, StrategyEntry] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load the registry from disk (if it exists)."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            for entry_dict in data.get("strategies", []):
                try:
                    entry = StrategyEntry.from_dict(entry_dict)
                    self._entries[entry.strategy_id] = entry
                except Exception:
                    logger.warning(
                        "Skipping malformed registry entry: %s", entry_dict
                    )
        except Exception:
            logger.warning(
                "Failed to load registry from %s", self.path, exc_info=True
            )

    def _save(self) -> None:
        """Atomically persist the registry to disk (tmp -> rename)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "saved_at": datetime.now().isoformat(),
            "strategies": [e.to_dict() for e in self._entries.values()],
        }
        tmp = self.path.parent / (self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)
        except Exception:
            logger.exception("Failed to save registry to %s", self.path)
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Public API -- CRUD
    # ------------------------------------------------------------------

    def list(self, lifecycle: str | None = None) -> list[StrategyEntry]:
        """Return all entries, optionally filtered by lifecycle stage.

        Args:
            lifecycle: If provided, only return entries matching this
                lifecycle value.
        """
        entries = list(self._entries.values())
        if lifecycle is not None:
            entries = [e for e in entries if e.lifecycle == lifecycle]
        return entries

    def get(self, strategy_id: str) -> StrategyEntry | None:
        """Return a single entry by its strategy_id, or None."""
        return self._entries.get(strategy_id)

    def add(self, entry: StrategyEntry) -> None:
        """Add a new strategy entry.

        Raises:
            ValueError: If a strategy with the same ID already exists.
        """
        if entry.strategy_id in self._entries:
            raise ValueError(
                f"Strategy already exists: {entry.strategy_id!r}"
            )
        if entry.lifecycle not in VALID_LIFECYCLES:
            raise ValueError(
                f"Invalid lifecycle {entry.lifecycle!r}; "
                f"must be one of {sorted(VALID_LIFECYCLES)}"
            )
        entry.created_at = datetime.now().isoformat()
        entry.updated_at = entry.created_at
        self._entries[entry.strategy_id] = entry
        self._save()
        logger.info("Registry: added %s (%s)", entry.strategy_id, entry.lifecycle)

    def update(self, entry: StrategyEntry) -> None:
        """Update an existing entry (upsert semantics).

        The ``updated_at`` timestamp is refreshed automatically.
        """
        if entry.lifecycle not in VALID_LIFECYCLES:
            raise ValueError(
                f"Invalid lifecycle {entry.lifecycle!r}; "
                f"must be one of {sorted(VALID_LIFECYCLES)}"
            )
        entry.updated_at = datetime.now().isoformat()
        self._entries[entry.strategy_id] = entry
        self._save()
        logger.info("Registry: updated %s", entry.strategy_id)

    def retire(self, strategy_id: str, *, reason: str = "") -> None:
        """Move a strategy to the ``retired`` lifecycle.

        Args:
            strategy_id: ID of the strategy to retire.
            reason: Human-readable reason for retirement.

        Raises:
            KeyError: If no strategy with the given ID exists.
        """
        entry = self._entries.get(strategy_id)
        if entry is None:
            raise KeyError(f"Strategy not found: {strategy_id!r}")
        entry.lifecycle = "retired"
        entry.retired_at = datetime.now().isoformat()
        entry.retired_reason = reason
        entry.updated_at = entry.retired_at
        entry.pid = None
        entry.socket_path = None
        self._save()
        logger.info(
            "Registry: retired %s (reason: %s)", strategy_id, reason or "none"
        )

    def remove(self, strategy_id: str) -> None:
        """Permanently remove a strategy entry from the registry.

        This is a hard delete; prefer :meth:`retire` for audit trails.
        """
        removed = self._entries.pop(strategy_id, None)
        if removed is not None:
            self._save()
            logger.info("Registry: removed %s", strategy_id)

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    def promote(self, strategy_id: str) -> None:
        """Promote a strategy to the next lifecycle stage.

        paper_trading -> live_trading

        Raises:
            KeyError: If no strategy with the given ID exists.
            ValueError: If the strategy cannot be promoted further.
        """
        entry = self._entries.get(strategy_id)
        if entry is None:
            raise KeyError(f"Strategy not found: {strategy_id!r}")

        transitions = {
            "paper_trading": "live_trading",
        }

        next_stage = transitions.get(entry.lifecycle)
        if next_stage is None:
            raise ValueError(
                f"Cannot promote strategy in lifecycle {entry.lifecycle!r}"
            )

        entry.lifecycle = next_stage
        entry.updated_at = datetime.now().isoformat()
        self._save()
        logger.info(
            "Registry: promoted %s -> %s", strategy_id, next_stage
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Generate a summary report of all strategies with health checks.

        Returns a dict with overall stats and per-strategy details.
        """
        entries = list(self._entries.values())

        # Lifecycle counts.
        lifecycle_counts: dict[str, int] = {}
        for entry in entries:
            lifecycle_counts[entry.lifecycle] = (
                lifecycle_counts.get(entry.lifecycle, 0) + 1
            )

        # Per-strategy details with basic health checks.
        strategy_details: list[dict[str, Any]] = []
        for entry in entries:
            health = "ok"
            health_issues: list[str] = []

            # Check: running strategies should have a pid.
            if entry.lifecycle in ("paper_trading", "live_trading"):
                if entry.pid is None:
                    health = "warning"
                    health_issues.append("no pid recorded (may not be running)")

                # Check: socket should exist for running strategies.
                if entry.socket_path:
                    sock = Path(entry.socket_path)
                    if not sock.exists():
                        health = "warning"
                        health_issues.append(
                            f"socket not found: {entry.socket_path}"
                        )
                else:
                    health = "warning"
                    health_issues.append("no socket_path configured")

            strategy_details.append(
                {
                    "strategy_id": entry.strategy_id,
                    "strategy_ref": entry.strategy_ref,
                    "lifecycle": entry.lifecycle,
                    "pnl": entry.pnl,
                    "exchange": entry.exchange,
                    "pid": entry.pid,
                    "health": health,
                    "health_issues": health_issues,
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                }
            )

        return {
            "total": len(entries),
            "lifecycle_counts": lifecycle_counts,
            "strategies": strategy_details,
            "generated_at": datetime.now().isoformat(),
        }
