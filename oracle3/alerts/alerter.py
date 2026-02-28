"""Abstract alerter base class and built-in implementations.

``Alerter`` is the abstract interface every alerter must implement.
``LogAlerter`` writes JSON lines to ``alerts.log`` in the data directory.
``CompositeAlerter`` fans out to multiple inner alerters, swallowing
individual failures so one broken alerter cannot crash the system.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from oracle3.ticker.ticker import Ticker
from oracle3.trader.types import OrderFailureReason, Trade

logger = logging.getLogger(__name__)


class Alerter(ABC):
    """Abstract base class for all alerters."""

    @abstractmethod
    async def send(self, message: str, level: str = 'info') -> None:
        """Send a raw message at the given level ('info', 'warning', 'error')."""

    async def on_trade(self, trade: Trade) -> None:
        ticker_name = getattr(trade.ticker, 'name', '') or trade.ticker.symbol
        msg = (
            f'Trade: {trade.side.value.upper()} {trade.quantity} '
            f'{ticker_name} @ {trade.price}'
        )
        await self.send(msg, level='info')

    async def on_order_rejected(
        self, reason: OrderFailureReason, ticker: Ticker
    ) -> None:
        ticker_name = getattr(ticker, 'name', '') or ticker.symbol
        msg = f'Order rejected ({reason.value}) for {ticker_name}'
        await self.send(msg, level='warning')

    async def on_risk_limit_hit(self, message: str) -> None:
        await self.send(f'Risk limit hit: {message}', level='warning')

    async def on_drawdown_alert(self, current_pct: Decimal, limit_pct: Decimal) -> None:
        msg = (
            f'Drawdown alert: {float(current_pct):.1%} '
            f'(limit: {float(limit_pct):.1%})'
        )
        await self.send(msg, level='error')

    async def on_engine_start(self) -> None:
        await self.send('Trading engine started', level='info')

    async def on_engine_stop(self, reason: str = '') -> None:
        suffix = f': {reason}' if reason else ''
        await self.send(f'Trading engine stopped{suffix}', level='info')

    async def on_error(self, error: Exception) -> None:
        await self.send(
            f'Error: {type(error).__name__}: {error}',
            level='error',
        )


# ---------------------------------------------------------------------------
# LogAlerter
# ---------------------------------------------------------------------------


class LogAlerter(Alerter):
    """Appends each alert as a JSON line to ``alerts.log`` in *data_dir*."""

    def __init__(self, data_dir: str | Path) -> None:
        self.log_path = Path(data_dir) / 'alerts.log'

    async def send(self, message: str, level: str = 'info') -> None:
        entry = {
            'timestamp': datetime.now().isoformat(),
            'level': level,
            'message': message,
        }
        try:
            with self.log_path.open('a') as fh:
                fh.write(json.dumps(entry) + '\n')
        except Exception:
            logger.debug('LogAlerter failed to write alert', exc_info=True)


# ---------------------------------------------------------------------------
# CompositeAlerter
# ---------------------------------------------------------------------------


class CompositeAlerter(Alerter):
    """Fan-out alerter: delegates every call to all inner alerters.

    Individual alerter failures are swallowed so one broken backend cannot
    crash the trading engine.
    """

    def __init__(self, alerters: list[Alerter]) -> None:
        self.alerters = alerters

    async def send(self, message: str, level: str = 'info') -> None:
        for alerter in self.alerters:
            try:
                await alerter.send(message, level)
            except Exception:
                logger.debug('CompositeAlerter: alerter.send() failed', exc_info=True)

    async def on_trade(self, trade: Trade) -> None:
        for alerter in self.alerters:
            try:
                await alerter.on_trade(trade)
            except Exception:
                logger.debug('CompositeAlerter: on_trade() failed', exc_info=True)

    async def on_order_rejected(
        self, reason: OrderFailureReason, ticker: Ticker
    ) -> None:
        for alerter in self.alerters:
            try:
                await alerter.on_order_rejected(reason, ticker)
            except Exception:
                logger.debug(
                    'CompositeAlerter: on_order_rejected() failed', exc_info=True
                )

    async def on_risk_limit_hit(self, message: str) -> None:
        for alerter in self.alerters:
            try:
                await alerter.on_risk_limit_hit(message)
            except Exception:
                logger.debug(
                    'CompositeAlerter: on_risk_limit_hit() failed', exc_info=True
                )

    async def on_drawdown_alert(self, current_pct: Decimal, limit_pct: Decimal) -> None:
        for alerter in self.alerters:
            try:
                await alerter.on_drawdown_alert(current_pct, limit_pct)
            except Exception:
                logger.debug(
                    'CompositeAlerter: on_drawdown_alert() failed', exc_info=True
                )

    async def on_engine_start(self) -> None:
        for alerter in self.alerters:
            try:
                await alerter.on_engine_start()
            except Exception:
                logger.debug(
                    'CompositeAlerter: on_engine_start() failed', exc_info=True
                )

    async def on_engine_stop(self, reason: str = '') -> None:
        for alerter in self.alerters:
            try:
                await alerter.on_engine_stop(reason)
            except Exception:
                logger.debug('CompositeAlerter: on_engine_stop() failed', exc_info=True)

    async def on_error(self, error: Exception) -> None:
        for alerter in self.alerters:
            try:
                await alerter.on_error(error)
            except Exception:
                logger.debug('CompositeAlerter: on_error() failed', exc_info=True)
