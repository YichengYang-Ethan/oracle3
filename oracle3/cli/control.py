"""Unix socket control server/client for the oracle3 trading engine.

Protocol: newline-delimited JSON over a Unix domain socket.

Request:  {"cmd": "pause"}
Response: {"ok": true, "status": "paused"}

Supported commands
------------------
pause     — stop data ingestion and LLM decision-making
resume    — restart data ingestion and LLM decision-making
stop      — gracefully stop the engine
status    — return current engine stats
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

# Default socket location; callers can override by subclassing or passing path.
SOCKET_DIR = Path.home() / '.oracle3'
SOCKET_PATH = SOCKET_DIR / 'engine.sock'


# ── Server ─────────────────────────────────────────────────────────────


class ControlServer:
    """Async Unix domain socket server that accepts engine control commands.

    Designed to run *inside* the engine process, started as an asyncio task.
    """

    def __init__(self, engine: TradingEngine, socket_path: Path = SOCKET_PATH) -> None:
        self.engine = engine
        self.socket_path = socket_path
        self.paused: bool = False
        self._start_time: datetime = datetime.now()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Start the Unix socket server."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove stale socket from a previous run
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        logger.info('Control server ready on %s', self.socket_path)

    async def stop(self) -> None:
        """Shut down the server and remove the socket file."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            self.socket_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one client connection (one request → one response)."""
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw:
                return
            request = json.loads(raw.decode())
            response = await self._dispatch(request)
        except (json.JSONDecodeError, asyncio.TimeoutError) as exc:
            response = {'ok': False, 'error': str(exc)}
        except Exception as exc:
            logger.warning('Control server error: %s', exc)
            response = {'ok': False, 'error': str(exc)}
        finally:
            try:
                writer.write((json.dumps(response) + '\n').encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, req: dict) -> dict[str, Any]:  # noqa: C901
        """Route a command and return a response dict."""
        cmd = req.get('cmd', '')

        if cmd == 'pause':
            return self._cmd_pause()

        if cmd == 'resume':
            return self._cmd_resume()

        if cmd == 'stop':
            # Schedule stop so we can still send the response first
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self.engine.stop())
            )
            return {'ok': True, 'status': 'stopping'}

        if cmd == 'status':
            return self._cmd_status()

        if cmd == 'get_state':
            return self._cmd_get_state()

        if cmd == 'swap_strategy':
            return await self._cmd_swap_strategy(req)

        return {'ok': False, 'error': f'Unknown command: {cmd!r}'}

    async def _cmd_swap_strategy(self, req: dict) -> dict:
        """Hot-swap the engine's strategy without restarting.

        Request keys:
          strategy_ref  — required, e.g. 'strategies/foo.py:Foo'
          kwargs        — optional dict of constructor keyword arguments
        """
        from oracle3.strategy.loader import load_strategy_class

        strategy_ref = req.get('strategy_ref', '')
        kwargs = req.get('kwargs') or {}

        if not strategy_ref:
            return {'ok': False, 'error': 'strategy_ref is required'}

        # Remember whether we were running so we can restore state after swap.
        was_paused = self.paused

        # Pause while we swap to avoid partial-state decisions.
        self._cmd_pause()

        try:
            strategy_cls = load_strategy_class(strategy_ref)
        except ValueError as exc:
            # Restore previous pause state before returning error.
            if not was_paused:
                self._cmd_resume()
            return {'ok': False, 'error': str(exc)}

        try:
            new_strategy = strategy_cls(**kwargs)
        except TypeError as exc:
            if not was_paused:
                self._cmd_resume()
            return {
                'ok': False,
                'error': f'Could not instantiate {strategy_ref!r} with kwargs={kwargs}: {exc}',
            }

        # Assign new strategy; existing positions on the trader are preserved.
        self.engine.strategy = new_strategy
        logger.info('Strategy hot-swapped to %s', strategy_ref)

        if not was_paused:
            self._cmd_resume()

        return {'ok': True, 'status': 'swapped', 'strategy_ref': strategy_ref}

    def _cmd_pause(self) -> dict:
        self.paused = True
        # Stop data flow: engine will sleep instead of polling the data source.
        self.engine._data_paused = True
        # Propagate to strategy so LLM decisions are skipped
        strategy = getattr(self.engine, 'strategy', None)
        trader = getattr(self.engine, 'trader', None)
        if strategy is not None:
            strategy.set_paused(True)
        if trader is not None:
            trader.set_read_only(True)
        return {'ok': True, 'status': 'paused'}

    def _cmd_resume(self) -> dict:
        self.paused = False
        # Restore data flow.
        self.engine._data_paused = False
        strategy = getattr(self.engine, 'strategy', None)
        trader = getattr(self.engine, 'trader', None)
        if strategy is not None:
            strategy.set_paused(False)
        if trader is not None:
            trader.set_read_only(False)
        return {'ok': True, 'status': 'running'}

    def _cmd_get_state(self) -> dict:  # noqa: C901  (deliberately comprehensive)
        """Serialize the full engine state for the standalone socket monitor."""
        from decimal import Decimal as D

        state: dict[str, Any] = {
            'ok': True,
            'paused': self.paused,
            'data_paused': getattr(self.engine, '_data_paused', False),
        }
        state['runtime'] = str(datetime.now() - self._start_time).split('.')[0]

        strategy = getattr(self.engine, 'strategy', None)
        trader = getattr(self.engine, 'trader', None)
        md = getattr(trader, 'market_data', None)

        # ── Stats ────────────────────────────────────────────────────
        orders_list = list(getattr(trader, 'orders', []))
        decision_stats = strategy.get_decision_stats() if strategy is not None else {}
        strategy_news_buf = int(getattr(strategy, 'news_buffer_count', 0) or 0)
        engine_news_buf = len(getattr(self.engine, '_news', []))
        state['stats'] = {
            'event_count': getattr(self.engine, '_event_count', 0),
            'order_books': len(getattr(md, 'order_books', {})),
            'news_buffered': max(strategy_news_buf, engine_news_buf),
            'decision_stats': decision_stats,
            'decisions': int(decision_stats.get('decisions', 0)),
            'executed': int(decision_stats.get('executed', 0)),
            'orders_total': len(orders_list),
            'orders_filled': sum(1 for o in orders_list if o.status.value == 'filled'),
        }

        # ── Portfolio ────────────────────────────────────────────────
        try:
            pm = trader.position_manager
            pv = pm.get_portfolio_value(md)
            total = float(sum(pv.values(), D('0')))
            realized = float(pm.get_total_realized_pnl())
            unrealized = float(pm.get_total_unrealized_pnl(md))
            state['portfolio'] = {
                'total': total,
                'cash_positions': [
                    {'symbol': p.ticker.symbol, 'qty': float(p.quantity)}
                    for p in pm.get_cash_positions()
                ],
                'realized_pnl': realized,
                'unrealized_pnl': unrealized,
            }
        except Exception:
            state['portfolio'] = {
                'total': 0,
                'cash_positions': [],
                'realized_pnl': 0,
                'unrealized_pnl': 0,
            }

        # ── Strategy decisions ───────────────────────────────────────
        try:
            decisions = list(strategy.get_decisions()) if strategy is not None else []
            state['decisions'] = [
                {
                    'timestamp': d.timestamp,
                    'action': d.action,
                    'confidence': float(getattr(d, 'confidence', 0.0) or 0.0),
                    'signal_values': {
                        str(k): float(v)
                        for k, v in (getattr(d, 'signal_values', {}) or {}).items()
                    },
                    'ticker_name': (d.ticker_name or '')[:30],
                    'reasoning': (getattr(d, 'reasoning', '') or '')[:60],
                    'executed': bool(d.executed),
                }
                for d in decisions[-40:]
            ]
        except Exception:
            state['decisions'] = []

        # ── Positions ────────────────────────────────────────────────
        try:
            pm = trader.position_manager
            pos_list = []
            for p in pm.get_non_cash_positions():
                if p.quantity <= 0:
                    continue
                bid = md.get_best_bid(p.ticker)
                cur = float(bid.price) if bid else 0.0
                pnl = (
                    (cur - float(p.average_cost)) * float(p.quantity)
                    if cur > 0
                    else 0.0
                )
                pos_list.append(
                    {
                        'name': (getattr(p.ticker, 'name', '') or p.ticker.symbol)[:30],
                        'qty': str(p.quantity),
                        'avg_cost': str(p.average_cost),
                        'bid': f'{cur:.4f}',
                        'pnl': f'{pnl:+.2f}',
                    }
                )
            state['positions'] = pos_list
        except Exception:
            state['positions'] = []

        # ── Recent orders ────────────────────────────────────────────
        try:
            state['orders'] = [
                {
                    'side': o.side.value,
                    'name': (getattr(o.ticker, 'name', '') or o.ticker.symbol)[:28],
                    'limit_price': str(o.limit_price),
                    'status': o.status.value,
                }
                for o in orders_list[-8:]
            ]
        except Exception:
            state['orders'] = []

        # ── Activity log & news (full, client tracks offset) ─────────
        state['activity_log'] = list(getattr(self.engine, '_activity_log', []))
        state['news'] = list(getattr(self.engine, '_news', []))

        # ── Order books ──────────────────────────────────────────────
        try:
            from oracle3.ticker.ticker import CashTicker

            books = []
            for ticker, ob in list(md.order_books.items()):
                if isinstance(ticker, CashTicker):
                    continue
                bid_lvl, ask_lvl = ob.best_bid, ob.best_ask
                if not (bid_lvl and ask_lvl and bid_lvl.price > 0):
                    continue
                mid = float(bid_lvl.price + ask_lvl.price) / 2
                spread = float(ask_lvl.price - bid_lvl.price)
                books.append(
                    {
                        'name': (getattr(ticker, 'name', '') or ticker.symbol)[:32],
                        'bid': f'{float(bid_lvl.price):.4f}',
                        'ask': f'{float(ask_lvl.price):.4f}',
                        'spread': f'{spread:.4f}',
                        'mid': f'{mid * 100:.0f}',
                        '_sort': abs(mid - 0.5),
                    }
                )
            books.sort(key=lambda x: x.pop('_sort'))
            state['order_books'] = books[:40]
        except Exception:
            state['order_books'] = []

        return state

    def _cmd_status(self) -> dict:
        strategy = getattr(self.engine, 'strategy', None)
        trader = getattr(self.engine, 'trader', None)
        decision_stats = strategy.get_decision_stats() if strategy is not None else {}
        runtime = str(datetime.now() - self._start_time).split('.')[0]
        activity_log = list(getattr(self.engine, '_activity_log', []))
        last_activity = activity_log[-1][1] if activity_log else ''

        return {
            'ok': True,
            'paused': self.paused,
            'data_paused': getattr(self.engine, '_data_paused', False),
            'runtime': runtime,
            'event_count': getattr(self.engine, '_event_count', 0),
            'decision_stats': decision_stats,
            'decisions': int(decision_stats.get('decisions', 0)),
            'executed': int(decision_stats.get('executed', 0)),
            'order_books': len(
                getattr(getattr(self.engine, 'market_data', None), 'order_books', {})
            ),
            'orders': len(list(getattr(trader, 'orders', []))),
            'last_activity': last_activity,
        }


# ── Client helpers ──────────────────────────────────────────────────────


async def send_command(
    cmd: str,
    socket_path: Path = SOCKET_PATH,
    **kwargs: Any,
) -> dict:
    """Send a JSON control command and return the parsed response.

    Raises ``FileNotFoundError`` when no engine is running.
    """
    if not socket_path.exists():
        raise FileNotFoundError(
            f'No engine running — socket not found: {socket_path}\n'
            f'Start an engine first with:  python scripts/run_paper_trading.py -e polymarket'
        )
    payload = {'cmd': cmd, **kwargs}
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write((json.dumps(payload) + '\n').encode())
        await writer.drain()
        # Signal EOF so the server knows we're done writing
        writer.write_eof()
        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        return json.loads(raw.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def run_command(cmd: str, socket_path: Path = SOCKET_PATH, **kwargs: Any) -> dict:
    """Synchronous wrapper around :func:`send_command` for Click commands."""
    return asyncio.run(send_command(cmd, socket_path=socket_path, **kwargs))
