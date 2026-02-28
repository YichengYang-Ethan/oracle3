"""Tests for config.Config."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from oracle3.config.config import (
    AlertConfig,
    Config,
    EngineConfig,
    RiskConfig,
    StorageConfig,
    StrategyConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / 'config.json'
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------------------
# Config.defaults()
# ---------------------------------------------------------------------------


def test_defaults_returns_valid_config():
    cfg = Config.defaults()
    assert isinstance(cfg.engine, EngineConfig)
    assert isinstance(cfg.strategy, StrategyConfig)
    assert isinstance(cfg.risk, RiskConfig)
    assert isinstance(cfg.alerts, AlertConfig)
    assert isinstance(cfg.storage, StorageConfig)
    assert cfg.engine.initial_capital == Decimal('10000')
    assert cfg.risk.max_positions == 10


# ---------------------------------------------------------------------------
# Config.from_file() — full valid JSON
# ---------------------------------------------------------------------------


def test_from_file_valid_json(tmp_path):
    data = {
        'engine': {'initial_capital': 5000, 'continuous': False},
        'strategy': {
            'type': 'simple',
            'trade_size': 2.0,
            'edge_threshold': 0.15,
            'reeval_cooldown': 600,
            'max_holding': 7200,
            'llm_provider': 'anthropic',
        },
        'risk': {
            'max_single_trade_size': 500,
            'max_position_size': 2500,
            'max_total_exposure': 25000,
            'max_drawdown_pct': 0.10,
            'daily_loss_limit': 200,
            'max_positions': 5,
        },
        'alerts': {
            'enabled': True,
            'telegram': {'bot_token': 'tok123', 'chat_id': '-100'},
            'thresholds': {'pnl_loss_alert': -50, 'drawdown_pct_alert': 0.05},
        },
        'storage': {'data_dir': '/tmp/td', 'save_interval_seconds': 30},
    }
    cfg = Config.from_file(write_config(tmp_path, data))

    assert cfg.engine.initial_capital == Decimal('5000')
    assert cfg.engine.continuous is False
    assert cfg.strategy.type == 'simple'
    assert cfg.strategy.trade_size == Decimal('2.0')
    assert cfg.strategy.llm_provider == 'anthropic'
    assert cfg.risk.max_single_trade_size == Decimal('500')
    assert cfg.risk.daily_loss_limit == Decimal('200')
    assert cfg.risk.max_positions == 5
    assert cfg.alerts.enabled is True
    assert cfg.alerts.telegram.bot_token == 'tok123'
    assert cfg.alerts.thresholds.drawdown_pct_alert == pytest.approx(0.05)
    assert cfg.storage.data_dir == '/tmp/td'
    assert cfg.storage.save_interval_seconds == 30


# ---------------------------------------------------------------------------
# Config.from_file() — missing fields use defaults (no crash)
# ---------------------------------------------------------------------------


def test_from_file_missing_fields_use_defaults(tmp_path):
    cfg = Config.from_file(write_config(tmp_path, {}))
    assert cfg.engine.initial_capital == Decimal('10000')
    assert cfg.risk.max_positions == 10
    assert cfg.strategy.type == 'simple'
    assert cfg.alerts.telegram.bot_token == ''


def test_from_file_partial_fields(tmp_path):
    data = {'engine': {'initial_capital': 999}}
    cfg = Config.from_file(write_config(tmp_path, data))
    assert cfg.engine.initial_capital == Decimal('999')
    assert cfg.engine.continuous is True  # default
    assert cfg.storage.save_interval_seconds == 60  # default


# ---------------------------------------------------------------------------
# Config.to_file() / from_file() round-trip
# ---------------------------------------------------------------------------


def test_to_file_from_file_roundtrip(tmp_path):
    original = Config(
        engine=EngineConfig(initial_capital=Decimal('7500'), continuous=False),
        strategy=StrategyConfig(type='custom', edge_threshold=Decimal('0.08')),
        risk=RiskConfig(max_positions=7, daily_loss_limit=Decimal('300')),
        storage=StorageConfig(data_dir='/data/trading', save_interval_seconds=45),
    )

    out_path = tmp_path / 'out.json'
    original.to_file(out_path)

    loaded = Config.from_file(out_path)

    assert loaded.engine.initial_capital == Decimal('7500')
    assert loaded.engine.continuous is False
    assert loaded.strategy.type == 'custom'
    assert loaded.strategy.edge_threshold == Decimal('0.08')
    assert loaded.risk.max_positions == 7
    assert loaded.risk.daily_loss_limit == Decimal('300')
    assert loaded.storage.data_dir == '/data/trading'
    assert loaded.storage.save_interval_seconds == 45


def test_to_file_daily_loss_none(tmp_path):
    cfg = Config.defaults()
    assert cfg.risk.daily_loss_limit is None
    out_path = tmp_path / 'cfg.json'
    cfg.to_file(out_path)
    data = json.loads(out_path.read_text())
    assert data['risk']['daily_loss_limit'] is None

    loaded = Config.from_file(out_path)
    assert loaded.risk.daily_loss_limit is None
