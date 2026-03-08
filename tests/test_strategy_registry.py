"""Tests for StrategyRegistry -- persistent portfolio strategy tracker.

Covers: add/get/list/remove, lifecycle transitions (promote, retire),
report generation, JSON persistence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oracle3.engine.registry import (
    VALID_LIFECYCLES,
    StrategyEntry,
    StrategyRegistry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_registry(tmp_path: Path) -> StrategyRegistry:
    """Create a StrategyRegistry backed by a temp file."""
    return StrategyRegistry(path=tmp_path / 'portfolio.json')


def _make_entry(
    strategy_id: str = 's1',
    strategy_ref: str = 'strategies/test.py:Test',
    lifecycle: str = 'paper_trading',
    exchange: str = 'polymarket',
    pnl: str | None = None,
    pid: int | None = None,
    socket_path: str | None = None,
) -> StrategyEntry:
    return StrategyEntry(
        strategy_id=strategy_id,
        strategy_ref=strategy_ref,
        lifecycle=lifecycle,
        exchange=exchange,
        pnl=pnl,
        pid=pid,
        socket_path=socket_path,
    )


# ===================================================================
# StrategyEntry dataclass tests
# ===================================================================


class TestStrategyEntry:

    def test_construction(self):
        e = _make_entry()
        assert e.strategy_id == 's1'
        assert e.strategy_ref == 'strategies/test.py:Test'
        assert e.lifecycle == 'paper_trading'
        assert e.exchange == 'polymarket'
        assert e.pnl is None
        assert e.pid is None
        assert e.retired_at is None
        assert e.retired_reason is None

    def test_to_dict(self):
        e = _make_entry()
        d = e.to_dict()
        assert d['strategy_id'] == 's1'
        assert d['lifecycle'] == 'paper_trading'
        assert isinstance(d['kwargs'], dict)

    def test_from_dict_roundtrip(self):
        e1 = _make_entry(pnl='42.50', exchange='kalshi')
        d = e1.to_dict()
        e2 = StrategyEntry.from_dict(d)
        assert e2.strategy_id == e1.strategy_id
        assert e2.pnl == '42.50'
        assert e2.exchange == 'kalshi'

    def test_from_dict_ignores_unknown(self):
        d = _make_entry().to_dict()
        d['future_field'] = 'hello'
        e = StrategyEntry.from_dict(d)
        assert e.strategy_id == 's1'
        assert not hasattr(e, 'future_field')

    def test_default_timestamps(self):
        e = _make_entry()
        assert e.created_at is not None
        assert e.updated_at is not None


# ===================================================================
# Registry CRUD: add, get, list, remove
# ===================================================================


class TestRegistryCRUD:

    def test_add_and_get(self, tmp_registry: StrategyRegistry):
        e = _make_entry('s1')
        tmp_registry.add(e)
        retrieved = tmp_registry.get('s1')
        assert retrieved is not None
        assert retrieved.strategy_id == 's1'

    def test_get_nonexistent(self, tmp_registry: StrategyRegistry):
        assert tmp_registry.get('nonexistent') is None

    def test_add_duplicate_raises(self, tmp_registry: StrategyRegistry):
        e1 = _make_entry('s1')
        tmp_registry.add(e1)
        e2 = _make_entry('s1')
        with pytest.raises(ValueError, match='already exists'):
            tmp_registry.add(e2)

    def test_add_invalid_lifecycle_raises(
        self, tmp_registry: StrategyRegistry
    ):
        e = _make_entry('s1', lifecycle='invalid')
        with pytest.raises(ValueError, match='Invalid lifecycle'):
            tmp_registry.add(e)

    def test_list_all(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1'))
        tmp_registry.add(_make_entry('s2'))
        tmp_registry.add(_make_entry('s3'))
        all_entries = tmp_registry.list()
        assert len(all_entries) == 3

    def test_list_by_lifecycle(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1', lifecycle='paper_trading'))
        tmp_registry.add(_make_entry('s2', lifecycle='live_trading'))
        tmp_registry.add(_make_entry('s3', lifecycle='paper_trading'))

        paper = tmp_registry.list(lifecycle='paper_trading')
        assert len(paper) == 2

        live = tmp_registry.list(lifecycle='live_trading')
        assert len(live) == 1

    def test_list_empty(self, tmp_registry: StrategyRegistry):
        assert tmp_registry.list() == []

    def test_remove_existing(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1'))
        tmp_registry.remove('s1')
        assert tmp_registry.get('s1') is None
        assert len(tmp_registry.list()) == 0

    def test_remove_nonexistent_is_silent(
        self, tmp_registry: StrategyRegistry
    ):
        # Should not raise
        tmp_registry.remove('nonexistent')

    def test_update_existing(self, tmp_registry: StrategyRegistry):
        e = _make_entry('s1', pnl='0.00')
        tmp_registry.add(e)
        e.pnl = '42.00'
        e.notes = 'Updated note'
        tmp_registry.update(e)
        retrieved = tmp_registry.get('s1')
        assert retrieved is not None
        assert retrieved.pnl == '42.00'
        assert retrieved.notes == 'Updated note'

    def test_update_invalid_lifecycle_raises(
        self, tmp_registry: StrategyRegistry
    ):
        e = _make_entry('s1')
        tmp_registry.add(e)
        e.lifecycle = 'invalid'
        with pytest.raises(ValueError, match='Invalid lifecycle'):
            tmp_registry.update(e)

    def test_update_upserts(self, tmp_registry: StrategyRegistry):
        """Update with an entry that doesn't exist yet should add it."""
        e = _make_entry('s_new')
        tmp_registry.update(e)
        assert tmp_registry.get('s_new') is not None


# ===================================================================
# Lifecycle transitions: promote, retire
# ===================================================================


class TestRegistryLifecycle:

    def test_promote_paper_to_live(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1', lifecycle='paper_trading'))
        tmp_registry.promote('s1')
        e = tmp_registry.get('s1')
        assert e is not None
        assert e.lifecycle == 'live_trading'

    def test_promote_live_raises(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1', lifecycle='live_trading'))
        with pytest.raises(ValueError, match='Cannot promote'):
            tmp_registry.promote('s1')

    def test_promote_retired_raises(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1', lifecycle='retired'))
        with pytest.raises(ValueError, match='Cannot promote'):
            tmp_registry.promote('s1')

    def test_promote_nonexistent_raises(
        self, tmp_registry: StrategyRegistry
    ):
        with pytest.raises(KeyError, match='not found'):
            tmp_registry.promote('nonexistent')

    def test_retire_from_paper(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1', lifecycle='paper_trading'))
        tmp_registry.retire('s1', reason='poor performance')
        e = tmp_registry.get('s1')
        assert e is not None
        assert e.lifecycle == 'retired'
        assert e.retired_at is not None
        assert e.retired_reason == 'poor performance'
        assert e.pid is None
        assert e.socket_path is None

    def test_retire_from_live(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(
            _make_entry('s1', lifecycle='live_trading', pid=1234)
        )
        tmp_registry.retire('s1', reason='market closed')
        e = tmp_registry.get('s1')
        assert e is not None
        assert e.lifecycle == 'retired'
        assert e.pid is None

    def test_retire_nonexistent_raises(
        self, tmp_registry: StrategyRegistry
    ):
        with pytest.raises(KeyError, match='not found'):
            tmp_registry.retire('nonexistent')

    def test_full_lifecycle(self, tmp_registry: StrategyRegistry):
        """paper_trading -> live_trading -> retired."""
        tmp_registry.add(_make_entry('s1', lifecycle='paper_trading'))
        tmp_registry.promote('s1')
        assert tmp_registry.get('s1').lifecycle == 'live_trading'
        tmp_registry.retire('s1', reason='done')
        assert tmp_registry.get('s1').lifecycle == 'retired'


# ===================================================================
# Report generation
# ===================================================================


class TestRegistryReport:

    def test_empty_report(self, tmp_registry: StrategyRegistry):
        report = tmp_registry.report()
        assert report['total'] == 0
        assert report['lifecycle_counts'] == {}
        assert report['strategies'] == []
        assert 'generated_at' in report

    def test_report_with_entries(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(_make_entry('s1', lifecycle='paper_trading'))
        tmp_registry.add(
            _make_entry('s2', lifecycle='live_trading', pid=1234)
        )
        tmp_registry.add(_make_entry('s3', lifecycle='retired'))

        report = tmp_registry.report()
        assert report['total'] == 3
        assert report['lifecycle_counts']['paper_trading'] == 1
        assert report['lifecycle_counts']['live_trading'] == 1
        assert report['lifecycle_counts']['retired'] == 1
        assert len(report['strategies']) == 3

    def test_report_health_check_no_pid(
        self, tmp_registry: StrategyRegistry
    ):
        """Running strategies without pid should show warning."""
        tmp_registry.add(
            _make_entry('s1', lifecycle='paper_trading', pid=None)
        )
        report = tmp_registry.report()
        s = report['strategies'][0]
        assert s['health'] == 'warning'
        assert any(
            'no pid' in issue for issue in s['health_issues']
        )

    def test_report_health_check_no_socket(
        self, tmp_registry: StrategyRegistry
    ):
        """Running strategies without socket_path should show warning."""
        tmp_registry.add(
            _make_entry(
                's1', lifecycle='paper_trading', pid=1234,
                socket_path=None,
            )
        )
        report = tmp_registry.report()
        s = report['strategies'][0]
        assert s['health'] == 'warning'
        assert any(
            'socket' in issue.lower() for issue in s['health_issues']
        )

    def test_report_health_check_missing_socket_file(
        self, tmp_registry: StrategyRegistry, tmp_path: Path,
    ):
        """Socket path that doesn't exist should show warning."""
        sock_path = str(tmp_path / 'nonexistent.sock')
        tmp_registry.add(
            _make_entry(
                's1', lifecycle='live_trading', pid=1234,
                socket_path=sock_path,
            )
        )
        report = tmp_registry.report()
        s = report['strategies'][0]
        assert s['health'] == 'warning'
        assert any(
            'socket not found' in issue for issue in s['health_issues']
        )

    def test_report_health_retired_ok(
        self, tmp_registry: StrategyRegistry
    ):
        """Retired strategies should have health=ok."""
        tmp_registry.add(_make_entry('s1', lifecycle='retired'))
        report = tmp_registry.report()
        s = report['strategies'][0]
        assert s['health'] == 'ok'

    def test_report_includes_pnl(self, tmp_registry: StrategyRegistry):
        tmp_registry.add(
            _make_entry('s1', lifecycle='retired', pnl='123.45')
        )
        report = tmp_registry.report()
        assert report['strategies'][0]['pnl'] == '123.45'


# ===================================================================
# JSON persistence
# ===================================================================


class TestRegistryPersistence:

    def test_data_persists_across_instances(self, tmp_path: Path):
        path = tmp_path / 'portfolio.json'

        reg1 = StrategyRegistry(path=path)
        reg1.add(_make_entry('s1'))
        reg1.add(_make_entry('s2'))

        # New instance from same file
        reg2 = StrategyRegistry(path=path)
        assert len(reg2.list()) == 2
        ids = {e.strategy_id for e in reg2.list()}
        assert ids == {'s1', 's2'}

    def test_file_is_valid_json(self, tmp_path: Path):
        path = tmp_path / 'portfolio.json'
        reg = StrategyRegistry(path=path)
        reg.add(_make_entry('s1'))

        raw = json.loads(path.read_text())
        assert 'strategies' in raw
        assert 'saved_at' in raw
        assert len(raw['strategies']) == 1

    def test_handles_missing_file(self, tmp_path: Path):
        path = tmp_path / 'nonexistent_dir' / 'portfolio.json'
        reg = StrategyRegistry(path=path)
        assert reg.list() == []

    def test_handles_corrupt_file(self, tmp_path: Path):
        path = tmp_path / 'portfolio.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('corrupted {{{')
        reg = StrategyRegistry(path=path)
        assert reg.list() == []

    def test_atomic_write_no_tmp_file(self, tmp_path: Path):
        """After a successful write, no .tmp file should remain."""
        path = tmp_path / 'portfolio.json'
        reg = StrategyRegistry(path=path)
        reg.add(_make_entry('s1'))
        tmp_file = path.parent / (path.name + '.tmp')
        assert not tmp_file.exists()

    def test_retire_persists(self, tmp_path: Path):
        path = tmp_path / 'portfolio.json'
        reg1 = StrategyRegistry(path=path)
        reg1.add(_make_entry('s1', lifecycle='paper_trading'))
        reg1.retire('s1', reason='test')

        reg2 = StrategyRegistry(path=path)
        e = reg2.get('s1')
        assert e is not None
        assert e.lifecycle == 'retired'
        assert e.retired_reason == 'test'

    def test_promote_persists(self, tmp_path: Path):
        path = tmp_path / 'portfolio.json'
        reg1 = StrategyRegistry(path=path)
        reg1.add(_make_entry('s1', lifecycle='paper_trading'))
        reg1.promote('s1')

        reg2 = StrategyRegistry(path=path)
        e = reg2.get('s1')
        assert e is not None
        assert e.lifecycle == 'live_trading'

    def test_remove_persists(self, tmp_path: Path):
        path = tmp_path / 'portfolio.json'
        reg1 = StrategyRegistry(path=path)
        reg1.add(_make_entry('s1'))
        reg1.remove('s1')

        reg2 = StrategyRegistry(path=path)
        assert reg2.get('s1') is None
        assert len(reg2.list()) == 0


# ===================================================================
# Edge cases
# ===================================================================


class TestRegistryEdgeCases:

    def test_all_valid_lifecycles(self, tmp_registry: StrategyRegistry):
        """All valid lifecycle values should be accepted."""
        for lc in VALID_LIFECYCLES:
            sid = f's_{lc}'
            tmp_registry.add(_make_entry(sid, lifecycle=lc))
            assert tmp_registry.get(sid).lifecycle == lc

    def test_kwargs_preserved(self, tmp_registry: StrategyRegistry):
        """Strategy kwargs should persist through serialization."""
        e = _make_entry('s1')
        e.kwargs = {'min_edge': 0.03, 'trade_size': 50}
        tmp_registry.add(e)
        retrieved = tmp_registry.get('s1')
        assert retrieved.kwargs['min_edge'] == 0.03
        assert retrieved.kwargs['trade_size'] == 50

    def test_notes_field(self, tmp_registry: StrategyRegistry):
        e = _make_entry('s1')
        e.notes = 'Testing a new approach'
        tmp_registry.add(e)
        retrieved = tmp_registry.get('s1')
        assert retrieved.notes == 'Testing a new approach'

    def test_multiple_operations_sequence(
        self, tmp_registry: StrategyRegistry
    ):
        """Test a complex sequence of operations."""
        # Add 3 strategies
        tmp_registry.add(_make_entry('s1', lifecycle='paper_trading'))
        tmp_registry.add(_make_entry('s2', lifecycle='paper_trading'))
        tmp_registry.add(_make_entry('s3', lifecycle='paper_trading'))

        # Promote s1 and s2
        tmp_registry.promote('s1')
        tmp_registry.promote('s2')

        # Retire s2
        tmp_registry.retire('s2', reason='underperformance')

        # Remove s3
        tmp_registry.remove('s3')

        # Verify final state
        assert len(tmp_registry.list()) == 2
        assert tmp_registry.get('s1').lifecycle == 'live_trading'
        assert tmp_registry.get('s2').lifecycle == 'retired'
        assert tmp_registry.get('s3') is None

        paper = tmp_registry.list(lifecycle='paper_trading')
        assert len(paper) == 0

        live = tmp_registry.list(lifecycle='live_trading')
        assert len(live) == 1
