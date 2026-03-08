"""Tests for the RelationStore and MarketRelation classes.

Covers: CRUD, list_by_type, list_by_status, lifecycle transitions,
JSON persistence, find_by_market graph queries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oracle3.market.relations import (
    SPREAD_TYPES,
    STATUS_LIFECYCLE,
    MarketRelation,
    RelationStore,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path: Path) -> RelationStore:
    """Create a RelationStore backed by a temp file."""
    return RelationStore(path=tmp_path / 'relations.json')


def _make_relation(
    relation_id: str = 'r1',
    spread_type: str = 'exclusivity',
    status: str = 'discovered',
    confidence: float = 0.8,
    market_a: dict | None = None,
    market_b: dict | None = None,
) -> MarketRelation:
    return MarketRelation(
        relation_id=relation_id,
        spread_type=spread_type,
        status=status,
        confidence=confidence,
        market_a=market_a or {'symbol': 'A', 'market_id': 'mkt_a'},
        market_b=market_b or {'symbol': 'B', 'market_id': 'mkt_b'},
        reasoning='Test relation',
    )


# ===================================================================
# MarketRelation dataclass tests
# ===================================================================


class TestMarketRelation:
    """Test MarketRelation construction and validation."""

    def test_valid_construction(self):
        r = _make_relation()
        assert r.relation_id == 'r1'
        assert r.spread_type == 'exclusivity'
        assert r.status == 'discovered'
        assert r.confidence == 0.8
        assert r.hedge_ratio == 1.0
        assert r.lead_lag == 0

    def test_all_spread_types_accepted(self):
        for st in SPREAD_TYPES:
            r = _make_relation(spread_type=st)
            assert r.spread_type == st

    def test_invalid_spread_type_raises(self):
        with pytest.raises(ValueError, match='Invalid spread_type'):
            _make_relation(spread_type='invalid_type')

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match='Invalid status'):
            _make_relation(status='unknown_status')

    def test_all_statuses_accepted(self):
        for status in STATUS_LIFECYCLE:
            r = _make_relation(status=status)
            assert r.status == status

    def test_to_dict(self):
        r = _make_relation()
        d = r.to_dict()
        assert d['relation_id'] == 'r1'
        assert d['spread_type'] == 'exclusivity'
        assert isinstance(d['market_a'], dict)
        assert isinstance(d['market_b'], dict)

    def test_from_dict_roundtrip(self):
        r1 = _make_relation()
        d = r1.to_dict()
        r2 = MarketRelation.from_dict(d)
        assert r2.relation_id == r1.relation_id
        assert r2.spread_type == r1.spread_type
        assert r2.confidence == r1.confidence

    def test_from_dict_ignores_unknown_keys(self):
        d = _make_relation().to_dict()
        d['future_field'] = 'some value'
        r = MarketRelation.from_dict(d)
        assert r.relation_id == 'r1'
        assert not hasattr(r, 'future_field')

    def test_unknown_spread_type_allowed(self):
        """The 'unknown' spread_type is a valid default."""
        r = MarketRelation(relation_id='x', spread_type='unknown')
        assert r.spread_type == 'unknown'


# ===================================================================
# ValidationResult tests
# ===================================================================


class TestValidationResult:

    def test_structural_is_always_valid(self):
        vr = ValidationResult(analysis_type='structural')
        assert vr.is_valid is True

    def test_lead_lag_valid_when_significant(self):
        vr = ValidationResult(
            analysis_type='lead_lag', lead_lag_significant=True
        )
        assert vr.is_valid is True

    def test_lead_lag_invalid_when_not_significant(self):
        vr = ValidationResult(
            analysis_type='lead_lag', lead_lag_significant=False
        )
        assert vr.is_valid is False

    def test_cointegrated_valid(self):
        vr = ValidationResult(is_cointegrated=True)
        assert vr.is_valid is True

    def test_stationary_valid(self):
        vr = ValidationResult(is_stationary=True)
        assert vr.is_valid is True

    def test_neither_set_invalid(self):
        vr = ValidationResult()
        assert vr.is_valid is False


class TestMarketRelationValidation:
    """Test set_validation and get_validation."""

    def test_set_validation_valid(self):
        r = _make_relation()
        vr = ValidationResult(
            analysis_type='structural',
            constraint='A + B <= 1',
            constraint_holds=True,
            violation_count=0,
        )
        r.set_validation(vr)
        assert r.status == 'validated'
        assert r.last_validated == vr.validated_at
        assert r.validation['analysis_type'] == 'structural'

    def test_set_validation_invalid(self):
        r = _make_relation()
        vr = ValidationResult(
            analysis_type='cointegration',
            is_cointegrated=False,
            is_stationary=False,
        )
        r.set_validation(vr)
        assert r.status == 'invalidated'

    def test_set_validation_propagates_hedge_ratio(self):
        r = _make_relation()
        vr = ValidationResult(
            analysis_type='structural',
            hedge_ratio=0.85,
        )
        r.set_validation(vr)
        assert r.hedge_ratio == 0.85

    def test_set_validation_propagates_lead_lag(self):
        r = _make_relation()
        vr = ValidationResult(
            analysis_type='lead_lag',
            lead_lag=3,
            lead_lag_significant=True,
        )
        r.set_validation(vr)
        assert r.lead_lag == 3

    def test_get_validation_roundtrip(self):
        r = _make_relation()
        vr = ValidationResult(
            analysis_type='structural',
            constraint='A <= B',
            violation_count=5,
            mean_arb=0.03,
        )
        r.set_validation(vr)
        recovered = r.get_validation()
        assert recovered is not None
        assert recovered.analysis_type == 'structural'
        assert recovered.constraint == 'A <= B'
        assert recovered.violation_count == 5
        assert recovered.mean_arb == 0.03

    def test_get_validation_none_when_empty(self):
        r = _make_relation()
        assert r.get_validation() is None


# ===================================================================
# RelationStore CRUD tests
# ===================================================================


class TestRelationStoreCRUD:

    def test_add_and_get(self, tmp_store: RelationStore):
        r = _make_relation('r1')
        tmp_store.add(r)
        retrieved = tmp_store.get('r1')
        assert retrieved is not None
        assert retrieved.relation_id == 'r1'
        assert retrieved.spread_type == 'exclusivity'

    def test_get_nonexistent_returns_none(self, tmp_store: RelationStore):
        assert tmp_store.get('nonexistent') is None

    def test_add_deduplicates(self, tmp_store: RelationStore):
        r1 = _make_relation('r1', confidence=0.5)
        r2 = _make_relation('r1', confidence=0.9)
        tmp_store.add(r1)
        tmp_store.add(r2)
        # Should have one entry with the latest values
        all_rels = tmp_store.list()
        assert len(all_rels) == 1
        assert all_rels[0].confidence == 0.9

    def test_update_existing(self, tmp_store: RelationStore):
        r = _make_relation('r1', confidence=0.5)
        tmp_store.add(r)
        r.confidence = 0.95
        r.reasoning = 'Updated'
        tmp_store.update(r)
        retrieved = tmp_store.get('r1')
        assert retrieved is not None
        assert retrieved.confidence == 0.95
        assert retrieved.reasoning == 'Updated'

    def test_update_adds_if_not_found(self, tmp_store: RelationStore):
        r = _make_relation('r_new')
        tmp_store.update(r)
        assert tmp_store.get('r_new') is not None

    def test_remove_existing(self, tmp_store: RelationStore):
        r = _make_relation('r1')
        tmp_store.add(r)
        assert tmp_store.remove('r1') is True
        assert tmp_store.get('r1') is None

    def test_remove_nonexistent(self, tmp_store: RelationStore):
        assert tmp_store.remove('nonexistent') is False

    def test_list_all(self, tmp_store: RelationStore):
        tmp_store.add(_make_relation('r1'))
        tmp_store.add(_make_relation('r2'))
        tmp_store.add(_make_relation('r3'))
        all_rels = tmp_store.list()
        assert len(all_rels) == 3

    def test_list_empty_store(self, tmp_store: RelationStore):
        assert tmp_store.list() == []


# ===================================================================
# RelationStore filter tests
# ===================================================================


class TestRelationStoreFilters:

    def test_list_by_type(self, tmp_store: RelationStore):
        tmp_store.add(_make_relation('r1', spread_type='exclusivity'))
        tmp_store.add(_make_relation('r2', spread_type='implication'))
        tmp_store.add(_make_relation('r3', spread_type='exclusivity'))

        exclusivity = tmp_store.list_by_type('exclusivity')
        assert len(exclusivity) == 2
        assert all(r.spread_type == 'exclusivity' for r in exclusivity)

        implication = tmp_store.list_by_type('implication')
        assert len(implication) == 1

    def test_list_by_status(self, tmp_store: RelationStore):
        r1 = _make_relation('r1', status='discovered')
        r2 = _make_relation('r2', status='validated')
        r3 = _make_relation('r3', status='deployed')
        tmp_store.add(r1)
        tmp_store.add(r2)
        tmp_store.add(r3)

        discovered = tmp_store.list_by_status('discovered')
        assert len(discovered) == 1
        assert discovered[0].relation_id == 'r1'

        validated = tmp_store.list_by_status('validated')
        assert len(validated) == 1

    def test_list_with_both_filters(self, tmp_store: RelationStore):
        tmp_store.add(
            _make_relation('r1', spread_type='exclusivity', status='validated')
        )
        tmp_store.add(
            _make_relation('r2', spread_type='implication', status='validated')
        )
        tmp_store.add(
            _make_relation(
                'r3', spread_type='exclusivity', status='discovered'
            )
        )

        result = tmp_store.list(
            spread_type='exclusivity', status='validated'
        )
        assert len(result) == 1
        assert result[0].relation_id == 'r1'


# ===================================================================
# Lifecycle transition tests
# ===================================================================


class TestRelationStoreLifecycle:

    def test_deploy_from_validated(self, tmp_store: RelationStore):
        r = _make_relation('r1', status='validated')
        tmp_store.add(r)
        assert tmp_store.deploy('r1') is True
        retrieved = tmp_store.get('r1')
        assert retrieved is not None
        assert retrieved.status == 'deployed'

    def test_deploy_from_discovered_fails(self, tmp_store: RelationStore):
        r = _make_relation('r1', status='discovered')
        tmp_store.add(r)
        assert tmp_store.deploy('r1') is False
        retrieved = tmp_store.get('r1')
        assert retrieved is not None
        assert retrieved.status == 'discovered'

    def test_deploy_nonexistent_fails(self, tmp_store: RelationStore):
        assert tmp_store.deploy('nonexistent') is False

    def test_invalidate(self, tmp_store: RelationStore):
        r = _make_relation('r1')
        tmp_store.add(r)
        assert tmp_store.invalidate('r1', reason='bad data') is True
        retrieved = tmp_store.get('r1')
        assert retrieved is not None
        assert retrieved.status == 'invalidated'
        assert 'bad data' in retrieved.reasoning

    def test_invalidate_nonexistent(self, tmp_store: RelationStore):
        assert tmp_store.invalidate('nonexistent') is False

    def test_retire(self, tmp_store: RelationStore):
        r = _make_relation('r1', status='deployed')
        tmp_store.add(r)
        assert tmp_store.retire('r1') is True
        retrieved = tmp_store.get('r1')
        assert retrieved is not None
        assert retrieved.status == 'retired'

    def test_retire_nonexistent(self, tmp_store: RelationStore):
        assert tmp_store.retire('nonexistent') is False

    def test_full_lifecycle(self, tmp_store: RelationStore):
        """discovered -> validated -> deployed -> retired."""
        r = _make_relation('r1', status='discovered')
        tmp_store.add(r)

        # Validate
        r = tmp_store.get('r1')
        assert r is not None
        vr = ValidationResult(analysis_type='structural')
        r.set_validation(vr)
        tmp_store.update(r)
        assert tmp_store.get('r1').status == 'validated'

        # Deploy
        assert tmp_store.deploy('r1') is True
        assert tmp_store.get('r1').status == 'deployed'

        # Retire
        assert tmp_store.retire('r1') is True
        assert tmp_store.get('r1').status == 'retired'


# ===================================================================
# JSON persistence tests
# ===================================================================


class TestRelationStorePersistence:

    def test_data_persists_across_instances(self, tmp_path: Path):
        path = tmp_path / 'relations.json'

        store1 = RelationStore(path=path)
        store1.add(_make_relation('r1'))
        store1.add(_make_relation('r2'))

        # Create a new instance pointing to the same file
        store2 = RelationStore(path=path)
        all_rels = store2.list()
        assert len(all_rels) == 2
        ids = {r.relation_id for r in all_rels}
        assert ids == {'r1', 'r2'}

    def test_file_is_valid_json(self, tmp_path: Path):
        path = tmp_path / 'relations.json'
        store = RelationStore(path=path)
        store.add(_make_relation('r1'))

        raw = json.loads(path.read_text())
        assert isinstance(raw, list)
        assert len(raw) == 1
        assert raw[0]['relation_id'] == 'r1'

    def test_handles_missing_file(self, tmp_path: Path):
        path = tmp_path / 'nonexistent' / 'relations.json'
        store = RelationStore(path=path)
        assert store.list() == []

    def test_handles_corrupt_file(self, tmp_path: Path):
        path = tmp_path / 'relations.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not valid json {{{')
        store = RelationStore(path=path)
        assert store.list() == []

    def test_atomic_write(self, tmp_path: Path):
        """After successful write, no .tmp file should remain."""
        path = tmp_path / 'relations.json'
        store = RelationStore(path=path)
        store.add(_make_relation('r1'))
        tmp_file = path.with_suffix('.tmp')
        assert not tmp_file.exists()


# ===================================================================
# Graph query tests
# ===================================================================


class TestRelationStoreGraphQueries:

    def test_find_by_market_id(self, tmp_store: RelationStore):
        r1 = _make_relation(
            'r1',
            market_a={'symbol': 'A', 'market_id': 'mkt_a'},
            market_b={'symbol': 'B', 'market_id': 'mkt_b'},
        )
        r2 = _make_relation(
            'r2',
            market_a={'symbol': 'C', 'market_id': 'mkt_c'},
            market_b={'symbol': 'A', 'market_id': 'mkt_a'},
        )
        r3 = _make_relation(
            'r3',
            market_a={'symbol': 'D', 'market_id': 'mkt_d'},
            market_b={'symbol': 'E', 'market_id': 'mkt_e'},
        )
        tmp_store.add(r1)
        tmp_store.add(r2)
        tmp_store.add(r3)

        # mkt_a appears in r1 (market_a) and r2 (market_b)
        results = tmp_store.find_by_market('mkt_a')
        assert len(results) == 2
        ids = {r.relation_id for r in results}
        assert ids == {'r1', 'r2'}

    def test_find_by_symbol(self, tmp_store: RelationStore):
        r = _make_relation(
            'r1',
            market_a={'symbol': 'MY_TICKER'},
            market_b={'symbol': 'OTHER'},
        )
        tmp_store.add(r)
        results = tmp_store.find_by_market('MY_TICKER')
        assert len(results) == 1

    def test_find_by_token_id(self, tmp_store: RelationStore):
        r = _make_relation(
            'r1',
            market_a={'symbol': 'X', 'token_id': 'tok_123'},
            market_b={'symbol': 'Y'},
        )
        tmp_store.add(r)
        results = tmp_store.find_by_market('tok_123')
        assert len(results) == 1

    def test_find_no_match(self, tmp_store: RelationStore):
        tmp_store.add(_make_relation('r1'))
        results = tmp_store.find_by_market('nonexistent_id')
        assert len(results) == 0

    def test_strongest(self, tmp_store: RelationStore):
        for i in range(5):
            tmp_store.add(
                _make_relation(f'r{i}', confidence=float(i) / 10)
            )
        top3 = tmp_store.strongest(n=3)
        assert len(top3) == 3
        # Should be in descending confidence order
        assert top3[0].confidence >= top3[1].confidence >= top3[2].confidence

    def test_validated_shortcut(self, tmp_store: RelationStore):
        tmp_store.add(_make_relation('r1', status='validated'))
        tmp_store.add(_make_relation('r2', status='discovered'))
        tmp_store.add(_make_relation('r3', status='validated'))
        result = tmp_store.validated()
        assert len(result) == 2

    def test_deployed_shortcut(self, tmp_store: RelationStore):
        tmp_store.add(_make_relation('r1', status='deployed'))
        tmp_store.add(_make_relation('r2', status='validated'))
        result = tmp_store.deployed()
        assert len(result) == 1
        assert result[0].relation_id == 'r1'
