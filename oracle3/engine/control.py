"""Unix socket control server/client for the oracle3 trading engine.

Protocol
--------
Newline-delimited JSON over a Unix domain socket.

    Request:   {"cmd": "pause"}
    Response:  {"ok": true, "status": "paused"}

Supported commands
------------------
pause       Stop data ingestion and strategy decision-making.
resume      Restart data ingestion and strategy decision-making.
stop        Gracefully stop the engine event loop.
status      Return current engine runtime stats.
killswitch  Immediately halt all trading and stop the engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from oracle3.core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)

# Default socket location; callers can override via constructor argument.
SOCKET_DIR = Path.home() / ".oracle3"
SOCKET_PATH = SOCKET_DIR / "engine.sock"


# ── Server ─────────────────────────────────────────────────────────────────


class ControlServer:
    """Async Unix domain socket server that accepts engine control commands.

    Designed to run *inside* the engine process, started as an asyncio task
    alongside the engine's main event loop.

    Example::

        server = ControlServer(engine)
        await server.start()
        # ... engine runs ...
        await server.stop()
    """

    def __init__(
        self,
        engine: TradingEngine,
        socket_path: Path = SOCKET_PATH,
    ) -> None:
        self.engine = engine
        self.socket_path = socket_path
        self.paused: bool = False
        self._start_time: datetime = datetime.now()
        self._server: asyncio.AbstractServer | None = None

    async def start(self, socket_path: Path | None = None) -> None:
        """Bind the Unix socket and begin accepting connections.

        Args:
            socket_path: Override the default socket path set at construction.
        """
        if socket_path is not None:
            self.socket_path = socket_path

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket from a previous run.
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        logger.info("Control server ready on %s", self.socket_path)

    async def stop(self) -> None:
        """Shut down the server and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            self.socket_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one client connection (one request -> one response)."""
        response: dict[str, Any] = {"ok": False, "error": "internal error"}
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw:
                return
            request = json.loads(raw.decode())
            response = await self._dispatch(request)
        except (json.JSONDecodeError, asyncio.TimeoutError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("Control server error: %s", exc)
            response = {"ok": False, "error": str(exc)}
        finally:
            try:
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, req: dict) -> dict[str, Any]:
        """Route a command string to the appropriate handler."""
        cmd = req.get("cmd", "")

        if cmd == "pause":
            return self._cmd_pause()

        if cmd == "resume":
            return self._cmd_resume()

        if cmd == "stop":
            return await self._cmd_stop()

        if cmd == "status":
            return self._cmd_status()

        if cmd == "killswitch":
            return await self._cmd_killswitch()

        return {"ok": False, "error": f"Unknown command: {cmd!r}"}

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    def _cmd_pause(self) -> dict[str, Any]:
        """Pause data ingestion and strategy decision-making."""
        self.paused = True
        self.engine._data_paused = True

        strategy = getattr(self.engine, "strategy", None)
        trader = getattr(self.engine, "trader", None)
        if strategy is not None:
            strategy.set_paused(True)
        if trader is not None:
            trader.set_read_only(True)

        logger.info("Engine paused via control server")
        return {"ok": True, "status": "paused"}

    def _cmd_resume(self) -> dict[str, Any]:
        """Resume data ingestion and strategy decision-making."""
        self.paused = False
        self.engine._data_paused = False

        strategy = getattr(self.engine, "strategy", None)
        trader = getattr(self.engine, "trader", None)
        if strategy is not None:
            strategy.set_paused(False)
        if trader is not None:
            trader.set_read_only(False)

        logger.info("Engine resumed via control server")
        return {"ok": True, "status": "running"}

    async def _cmd_stop(self) -> dict[str, Any]:
        """Gracefully stop the engine.

        The stop is scheduled via ``call_soon`` so we can still send the
        response before the event loop shuts down.
        """
        loop = asyncio.get_event_loop()
        loop.call_soon(lambda: asyncio.ensure_future(self.engine.stop()))
        logger.info("Engine stop requested via control server")
        return {"ok": True, "status": "stopping"}

    def _cmd_status(self) -> dict[str, Any]:
        """Return current engine runtime statistics."""
        strategy = getattr(self.engine, "strategy", None)
        trader = getattr(self.engine, "trader", None)

        runtime = str(datetime.now() - self._start_time).split(".")[0]

        decision_stats: dict[str, Any] = {}
        if strategy is not None and hasattr(strategy, "get_decision_stats"):
            decision_stats = strategy.get_decision_stats()

        order_count = len(list(getattr(trader, "orders", [])))
        activity_log = list(getattr(self.engine, "_activity_log", []))
        last_activity = activity_log[-1][1] if activity_log else ""

        return {
            "ok": True,
            "paused": self.paused,
            "data_paused": getattr(self.engine, "_data_paused", False),
            "runtime": runtime,
            "engine_running": self.engine.running,
            "event_count": getattr(self.engine, "_event_count", 0),
            "decisions": int(decision_stats.get("decisions", 0)),
            "executed": int(decision_stats.get("executed", 0)),
            "orders": order_count,
            "last_activity": last_activity,
        }

    async def _cmd_killswitch(self) -> dict[str, Any]:
        """Emergency stop: immediately disable all trading and halt the engine.

        1. Force trader into read-only mode.
        2. Pause the strategy.
        3. Write a kill-switch sentinel file so the trader's built-in
           ``_kill_switch_active()`` check prevents any further orders
           even if the process somehow continues.
        4. Stop the engine.
        """
        trader = getattr(self.engine, "trader", None)
        strategy = getattr(self.engine, "strategy", None)

        if trader is not None:
            trader.set_read_only(True)
        if strategy is not None:
            strategy.set_paused(True)

        # Write the sentinel file that Trader._kill_switch_active() checks.
        kill_file = Path.home() / ".oracle3" / "kill.switch"
        try:
            kill_file.parent.mkdir(parents=True, exist_ok=True)
            kill_file.write_text(
                f"Kill switch activated at {datetime.now().isoformat()}\n"
            )
            logger.warning("Kill switch file written: %s", kill_file)
        except Exception:
            logger.exception("Failed to write kill switch file")

        # Schedule engine stop.
        loop = asyncio.get_event_loop()
        loop.call_soon(lambda: asyncio.ensure_future(self.engine.stop()))

        logger.warning("KILLSWITCH activated via control server")
        return {"ok": True, "status": "killed"}


# ── Client ─────────────────────────────────────────────────────────────────


class ControlClient:
    """Client for connecting to a running engine's control socket.

    Usage::

        client = ControlClient()
        client.connect()  # verifies socket exists
        response = await client.send_command("status")
        print(response)
    """

    def __init__(self, socket_path: Path = SOCKET_PATH) -> None:
        self.socket_path = socket_path

    def connect(self, socket_path: Path | None = None) -> None:
        """Verify the control socket exists (engine must be running).

        Args:
            socket_path: Override the socket path set at construction.

        Raises:
            FileNotFoundError: If no socket file exists at the path.
        """
        if socket_path is not None:
            self.socket_path = socket_path

        if not self.socket_path.exists():
            raise FileNotFoundError(
                f"No engine running -- socket not found: {self.socket_path}"
            )

    async def send_command(self, cmd: str, **kwargs: Any) -> dict[str, Any]:
        """Send a JSON control command and return the parsed response.

        Args:
            cmd: Command name (pause, resume, stop, status, killswitch).
            **kwargs: Additional key-value pairs to include in the request.

        Returns:
            Parsed JSON response dict from the server.

        Raises:
            FileNotFoundError: If the socket does not exist.
            asyncio.TimeoutError: If the server does not respond within 5 s.
        """
        if not self.socket_path.exists():
            raise FileNotFoundError(
                f"No engine running -- socket not found: {self.socket_path}"
            )

        payload = {"cmd": cmd, **kwargs}
        reader, writer = await asyncio.open_unix_connection(
            str(self.socket_path)
        )
        try:
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            writer.write_eof()
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            return json.loads(raw.decode())
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def send_command_sync(self, cmd: str, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper around :meth:`send_command`.

        Convenience for CLI scripts and interactive debugging.
        """
        return asyncio.run(self.send_command(cmd, **kwargs))


# ── Module-level convenience functions ─────────────────────────────────────


async def send_command(
    cmd: str,
    socket_path: Path = SOCKET_PATH,
    **kwargs: Any,
) -> dict[str, Any]:
    """Send a control command to a running engine (module-level helper).

    Raises ``FileNotFoundError`` when no engine is running.
    """
    client = ControlClient(socket_path)
    client.connect()
    return await client.send_command(cmd, **kwargs)


def run_command(
    cmd: str,
    socket_path: Path = SOCKET_PATH,
    **kwargs: Any,
) -> dict[str, Any]:
    """Synchronous wrapper around :func:`send_command` for CLI use."""
    return asyncio.run(send_command(cmd, socket_path=socket_path, **kwargs))
