"""Persistent state store — saves/loads positions, trades, orders, equity curve."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from oracle3.position.position_manager import Position, PositionManager
from oracle3.storage.serializers import (
    deserialize_equity_point,
    deserialize_order,
    deserialize_position,
    deserialize_trade,
    serialize_equity_point,
    serialize_order,
    serialize_position,
    serialize_trade,
)
from oracle3.trader.types import Order, Trade

logger = logging.getLogger(__name__)


class StateStore:
    """JSON-file-based persistence layer for trading state."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _path(self, filename: str) -> Path:
        return self.data_dir / filename

    def _read_json(self, filename: str) -> dict:
        path = self._path(filename)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            logger.warning(
                'Failed to read %s — starting fresh', filename, exc_info=True
            )
            return {}

    def _write_json_atomic(self, filename: str, data: dict) -> None:
        """Write *data* to *filename* atomically via a temp-file rename."""
        path = self._path(filename)
        tmp_path = path.parent / (path.name + '.tmp')
        try:
            tmp_path.write_text(json.dumps(data, indent=2))
            tmp_path.replace(path)
        except Exception:
            logger.exception('Failed to write %s', filename)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def save_positions(self, position_manager: PositionManager) -> None:
        """Write all positions (including cash) to positions.json."""
        all_positions = list(position_manager.positions.values())
        data = {
            'saved_at': datetime.now().isoformat(),
            'positions': [serialize_position(p) for p in all_positions],
        }
        self._write_json_atomic('positions.json', data)

    def load_positions(self) -> list[Position]:
        """Load positions from positions.json. Returns empty list if file missing."""
        data = self._read_json('positions.json')
        if not data:
            return []
        try:
            return [deserialize_position(p) for p in data.get('positions', [])]
        except Exception:
            logger.warning(
                'Failed to deserialize positions — starting fresh', exc_info=True
            )
            return []

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def append_trade(self, trade: Trade) -> None:
        """Append *trade* (with current timestamp) to trades.json."""
        data = self._read_json('trades.json')
        if not data:
            data = {'trades': []}
        data['trades'].append(serialize_trade(trade, datetime.now()))
        self._write_json_atomic('trades.json', data)

    def load_trades(self) -> list[Trade]:
        """Load all trades from trades.json. Returns empty list if file missing."""
        data = self._read_json('trades.json')
        if not data:
            return []
        try:
            return [deserialize_trade(t) for t in data.get('trades', [])]
        except Exception:
            logger.warning(
                'Failed to deserialize trades — starting fresh', exc_info=True
            )
            return []

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def append_order(self, order: Order) -> None:
        """Append *order* to orders.json."""
        data = self._read_json('orders.json')
        if not data:
            data = {'orders': []}
        data['orders'].append(serialize_order(order))
        self._write_json_atomic('orders.json', data)

    def load_orders(self) -> list[Order]:
        """Load all orders from orders.json. Returns empty list if file missing."""
        data = self._read_json('orders.json')
        if not data:
            return []
        try:
            return [deserialize_order(o) for o in data.get('orders', [])]
        except Exception:
            logger.warning(
                'Failed to deserialize orders — starting fresh', exc_info=True
            )
            return []

    # ------------------------------------------------------------------
    # Equity curve
    # ------------------------------------------------------------------

    def save_equity_curve(self, equity_curve: list) -> None:
        """Write the equity curve to equity_curve.json."""
        data = {
            'equity_curve': [serialize_equity_point(pt) for pt in equity_curve],
        }
        self._write_json_atomic('equity_curve.json', data)

    def load_equity_curve(self) -> list:
        """Load equity curve from equity_curve.json. Returns empty list if missing."""
        data = self._read_json('equity_curve.json')
        if not data:
            return []
        try:
            return [deserialize_equity_point(pt) for pt in data.get('equity_curve', [])]
        except Exception:
            logger.warning(
                'Failed to deserialize equity curve — starting fresh', exc_info=True
            )
            return []

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def save_all(self, position_manager: PositionManager, perf: object) -> None:
        """Save positions and equity curve atomically.

        *perf* is a ``PerformanceAnalyzer`` instance (typed as ``object`` to
        avoid a circular import).
        """
        self.save_positions(position_manager)
        self.save_equity_curve(perf.equity_curve)  # type: ignore[attr-defined]
