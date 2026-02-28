"""Configuration dataclasses with JSON load/save support."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass
class EngineConfig:
    initial_capital: Decimal = Decimal('10000')
    continuous: bool = True


@dataclass
class StrategyConfig:
    type: str = 'simple'
    trade_size: Decimal = Decimal('1.0')
    edge_threshold: Decimal = Decimal('0.10')
    reeval_cooldown: int = 300
    max_holding: int = 3600
    llm_provider: str = 'openai'


@dataclass
class RiskConfig:
    max_single_trade_size: Decimal = Decimal('1000')
    max_position_size: Decimal = Decimal('5000')
    max_total_exposure: Decimal = Decimal('50000')
    max_drawdown_pct: Decimal = Decimal('0.20')
    daily_loss_limit: Decimal | None = None
    max_positions: int = 10


@dataclass
class TelegramConfig:
    bot_token: str = ''
    chat_id: str = ''


@dataclass
class AlertThresholds:
    pnl_loss_alert: float | None = None
    drawdown_pct_alert: float | None = None


@dataclass
class AlertConfig:
    enabled: bool = True
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    thresholds: AlertThresholds = field(default_factory=AlertThresholds)


@dataclass
class StorageConfig:
    data_dir: str = './trading_data'
    save_interval_seconds: int = 60


@dataclass
class Config:
    engine: EngineConfig = field(default_factory=EngineConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def defaults(cls) -> Config:
        """Return a Config with sensible defaults (no file required)."""
        return cls()

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        """Load Config from a JSON file, falling back to defaults for missing keys."""
        data = json.loads(Path(path).read_text())

        engine_d = data.get('engine', {})
        engine = EngineConfig(
            initial_capital=Decimal(str(engine_d.get('initial_capital', 10000))),
            continuous=bool(engine_d.get('continuous', True)),
        )

        strat_d = data.get('strategy', {})
        strategy = StrategyConfig(
            type=strat_d.get('type', 'simple'),
            trade_size=Decimal(str(strat_d.get('trade_size', '1.0'))),
            edge_threshold=Decimal(str(strat_d.get('edge_threshold', '0.10'))),
            reeval_cooldown=int(strat_d.get('reeval_cooldown', 300)),
            max_holding=int(strat_d.get('max_holding', 3600)),
            llm_provider=strat_d.get('llm_provider', 'openai'),
        )

        risk_d = data.get('risk', {})
        daily_loss_raw = risk_d.get('daily_loss_limit')
        risk = RiskConfig(
            max_single_trade_size=Decimal(
                str(risk_d.get('max_single_trade_size', '1000'))
            ),
            max_position_size=Decimal(str(risk_d.get('max_position_size', '5000'))),
            max_total_exposure=Decimal(str(risk_d.get('max_total_exposure', '50000'))),
            max_drawdown_pct=Decimal(str(risk_d.get('max_drawdown_pct', '0.20'))),
            daily_loss_limit=Decimal(str(daily_loss_raw))
            if daily_loss_raw is not None
            else None,
            max_positions=int(risk_d.get('max_positions', 10)),
        )

        alerts_d = data.get('alerts', {})
        tg_d = alerts_d.get('telegram', {})
        tg = TelegramConfig(
            bot_token=tg_d.get('bot_token', ''),
            chat_id=tg_d.get('chat_id', ''),
        )
        thresh_d = alerts_d.get('thresholds', {})
        thresholds = AlertThresholds(
            pnl_loss_alert=thresh_d.get('pnl_loss_alert'),
            drawdown_pct_alert=thresh_d.get('drawdown_pct_alert'),
        )
        alerts = AlertConfig(
            enabled=bool(alerts_d.get('enabled', True)),
            telegram=tg,
            thresholds=thresholds,
        )

        storage_d = data.get('storage', {})
        storage = StorageConfig(
            data_dir=storage_d.get('data_dir', './trading_data'),
            save_interval_seconds=int(storage_d.get('save_interval_seconds', 60)),
        )

        return cls(
            engine=engine, strategy=strategy, risk=risk, alerts=alerts, storage=storage
        )

    def to_file(self, path: str | Path) -> None:
        """Write this Config to a JSON file."""
        data = {
            'engine': {
                'initial_capital': float(self.engine.initial_capital),
                'continuous': self.engine.continuous,
            },
            'strategy': {
                'type': self.strategy.type,
                'trade_size': float(self.strategy.trade_size),
                'edge_threshold': float(self.strategy.edge_threshold),
                'reeval_cooldown': self.strategy.reeval_cooldown,
                'max_holding': self.strategy.max_holding,
                'llm_provider': self.strategy.llm_provider,
            },
            'risk': {
                'max_single_trade_size': float(self.risk.max_single_trade_size),
                'max_position_size': float(self.risk.max_position_size),
                'max_total_exposure': float(self.risk.max_total_exposure),
                'max_drawdown_pct': float(self.risk.max_drawdown_pct),
                'daily_loss_limit': (
                    float(self.risk.daily_loss_limit)
                    if self.risk.daily_loss_limit is not None
                    else None
                ),
                'max_positions': self.risk.max_positions,
            },
            'alerts': {
                'enabled': self.alerts.enabled,
                'telegram': {
                    'bot_token': self.alerts.telegram.bot_token,
                    'chat_id': self.alerts.telegram.chat_id,
                },
                'thresholds': {
                    'pnl_loss_alert': self.alerts.thresholds.pnl_loss_alert,
                    'drawdown_pct_alert': self.alerts.thresholds.drawdown_pct_alert,
                },
            },
            'storage': {
                'data_dir': self.storage.data_dir,
                'save_interval_seconds': self.storage.save_interval_seconds,
            },
        }
        Path(path).write_text(json.dumps(data, indent=2))
