"""Utility functions for CLI integration with trading engine."""

from __future__ import annotations

import asyncio
import json
import logging

import click

from oracle3.cli.control import ControlServer
from oracle3.cli.monitor import TradingMonitor
from oracle3.core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, default=str))
        return
    if isinstance(payload, dict):
        click.echo(payload.get('message', str(payload)))
        return
    click.echo(str(payload))


class MonitoredTradingEngine:
    """Wrapper for TradingEngine with optional Textual live monitor.

    Architecture when monitor is enabled
    ─────────────────────────────────────
    Main thread runs Textual (via ``app.run_async()``) so that Textual's
    LinuxDriver can register signal handlers (SIGTSTP / SIGCONT).
    The trading engine and the Unix-socket control server both run *inside*
    Textual's asyncio event loop as Textual Workers — no extra threads, no
    signal-handler conflicts.

    Architecture when monitor is disabled
    ──────────────────────────────────────
    The engine and the control server run directly in the asyncio event loop
    (``asyncio.run(_main(...))``).  The process can still be controlled via
    ``oracle3 trade pause/resume/stop/status`` from another terminal.
    """

    def __init__(
        self,
        engine: TradingEngine,
        refresh_rate: float = 2.0,  # kept for API compatibility
        enabled: bool = True,
        exchange_name: str = '',
    ) -> None:
        self.engine = engine
        self.refresh_rate = refresh_rate
        self.enabled = enabled
        self.exchange_name = exchange_name
        self.monitor: TradingMonitor | None = None  # used by display_snapshot()
        self.control_server: ControlServer = ControlServer(engine)

    async def start(self) -> None:
        """Start the engine (and monitor if enabled), plus the control server."""
        if self.enabled:
            from oracle3.cli.textual_monitor import TradingMonitorApp

            app = TradingMonitorApp(
                engine=self.engine,
                exchange_name=self.exchange_name,
                control_server=self.control_server,
            )
            # run_async() keeps Textual on the main thread (required for signals).
            # Engine + control server are started as Textual workers inside the app.
            await app.run_async()
        else:
            # Non-interactive mode: engine + control server run directly.
            await asyncio.gather(
                self.control_server.start(),
                self.engine.start(),
                return_exceptions=True,
            )
            await self.control_server.stop()

    async def stop(self) -> None:
        """Stop engine and control server."""
        await self.engine.stop()
        await self.control_server.stop()

    def display_snapshot(self) -> None:
        """One-shot Rich snapshot (non-watch mode)."""
        if self.monitor is None:
            self.monitor = TradingMonitor(
                trader=self.engine.trader,
                position_manager=self.engine.trader.position_manager,
                exchange_name=self.exchange_name,
            )
        self.monitor.display_snapshot()


def add_monitoring_to_engine(
    engine: TradingEngine,
    watch: bool = False,
    refresh_rate: float = 2.0,
    exchange_name: str = '',
) -> MonitoredTradingEngine:
    """Wrap a TradingEngine with optional Textual live monitoring.

    Args:
        engine: The trading engine to wrap.
        watch: Enable the interactive Textual monitor.
        refresh_rate: UI refresh interval hint (seconds).
        exchange_name: Exchange name shown in the monitor header.

    Returns:
        A :class:`MonitoredTradingEngine` wrapping the original engine.
    """
    return MonitoredTradingEngine(
        engine=engine,
        refresh_rate=refresh_rate,
        enabled=watch,
        exchange_name=exchange_name,
    )
