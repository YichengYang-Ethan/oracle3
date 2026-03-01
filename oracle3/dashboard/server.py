"""FastAPI WebSocket server for the Oracle3 web dashboard.

Runs in-process alongside the TradingEngine, providing real-time state
via WebSocket and a single-page HTML dashboard.

NOTE: This module intentionally does NOT use ``from __future__ import
annotations`` because FastAPI relies on runtime annotation evaluation
for dependency injection (e.g. WebSocket parameter type resolution).
"""

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from oracle3.core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / 'static'

# Wallet address for Solscan links
SOLANA_WALLET = '7RQ3YL4cLNbQbwAUHBP6GzdRbG6NRng8qBcHbiDrf8Ae'


def _serialize_snapshot(engine: 'TradingEngine') -> dict[str, Any]:
    """Build a JSON-safe state dict from the engine snapshot.

    Re-uses the same data as ControlServer._cmd_get_state() but reads
    directly from the engine's get_snapshot() method for cleaner access.
    """
    snap = engine.get_snapshot()

    # Positions
    positions = [
        {
            'symbol': p.ticker_symbol,
            'name': p.ticker_name,
            'qty': str(p.quantity),
            'avg_cost': str(p.average_cost),
            'current_price': str(p.current_price),
            'unrealized_pnl': str(p.unrealized_pnl),
        }
        for p in snap.positions
    ]

    # Order books
    order_books = []
    for ob in snap.orderbooks:
        best_bid = float(ob.bids[0][0]) if ob.bids else 0.0
        best_ask = float(ob.asks[0][0]) if ob.asks else 0.0
        spread = best_ask - best_bid if (best_bid > 0 and best_ask > 0) else 0.0
        mid = (best_bid + best_ask) / 2 if (best_bid > 0 and best_ask > 0) else 0.0
        order_books.append({
            'symbol': ob.ticker_symbol,
            'bid': f'{best_bid:.4f}',
            'ask': f'{best_ask:.4f}',
            'spread': f'{spread:.4f}',
            'mid_pct': f'{mid * 100:.0f}',
        })

    # Recent trades
    trades = [
        {
            'time': t.time,
            'side': t.side,
            'name': t.ticker_name,
            'price': str(t.price),
            'qty': str(t.quantity),
            'status': t.status,
        }
        for t in snap.recent_trades
    ]

    # AI decisions from strategy
    decisions: list[dict[str, Any]] = []
    strategy = getattr(engine, 'strategy', None)
    if strategy is not None:
        try:
            raw_decisions = list(strategy.get_decisions())
            for d in raw_decisions[-30:]:
                decisions.append({
                    'timestamp': d.timestamp,
                    'action': d.action,
                    'ticker_name': (d.ticker_name or '')[:40],
                    'confidence': float(getattr(d, 'confidence', 0.0) or 0.0),
                    'reasoning': (getattr(d, 'reasoning', '') or '')[:80],
                    'executed': bool(d.executed),
                })
        except Exception:
            logger.debug('Failed to serialize decisions', exc_info=True)

    # Activity log
    activity_log = list(getattr(engine, '_activity_log', []))

    # News
    news = list(getattr(engine, '_news', []))

    # Performance stats from analyzer
    performance: dict[str, Any] = {}
    analyzer = getattr(engine, '_perf', None) or getattr(engine, 'analyzer', None)
    if analyzer is not None:
        try:
            stats = analyzer.get_stats()
            performance = {
                'total_trades': stats.total_trades,
                'winning_trades': stats.winning_trades,
                'losing_trades': stats.losing_trades,
                'win_rate': str(stats.win_rate),
                'average_profit': str(stats.average_profit),
                'average_loss': str(stats.average_loss),
                'max_drawdown': str(stats.max_drawdown),
                'sharpe_ratio': str(stats.sharpe_ratio),
                'profit_factor': str(stats.profit_factor),
                'total_pnl': str(stats.total_pnl),
                'max_consecutive_wins': stats.max_consecutive_wins,
                'max_consecutive_losses': stats.max_consecutive_losses,
            }
        except Exception:
            logger.debug('Failed to serialize performance stats', exc_info=True)

    # Equity curve from analyzer (reuse resolved reference)
    equity_curve: list[str] = []
    if analyzer is not None:
        try:
            curve = analyzer.get_equity_curve()
            equity_curve = [str(pt.equity) for pt in curve]
        except Exception:
            logger.debug('Failed to serialize equity curve', exc_info=True)

    # Initial capital for return % calculation
    initial_capital = str(getattr(engine, '_initial_capital', '10000'))

    # Truncated wallet for display (e.g. "7RQ3...f8Ae")
    wallet_short = (
        f'{SOLANA_WALLET[:4]}...{SOLANA_WALLET[-4:]}'
        if len(SOLANA_WALLET) >= 8
        else SOLANA_WALLET
    )

    return {
        'timestamp': datetime.now().isoformat(),
        'running': snap.engine_running,
        'paused': getattr(engine, '_data_paused', False),
        'uptime': snap.uptime,
        'event_count': snap.event_count,
        'initial_capital': initial_capital,
        'network': 'Solana Mainnet',
        'portfolio': {
            'equity': str(snap.equity),
            'cash': str(snap.cash),
            'realized_pnl': str(snap.realized_pnl),
            'unrealized_pnl': str(snap.unrealized_pnl),
            'total_pnl': str(snap.total_pnl),
            'exposure_pct': snap.exposure_pct,
        },
        'positions': positions,
        'order_books': order_books,
        'decisions': decisions,
        'trades': trades,
        'performance': performance,
        'equity_curve': equity_curve,
        'activity_log': activity_log[-50:],
        'news': news[-20:],
        'wallet': SOLANA_WALLET,
        'wallet_short': wallet_short,
    }


def _build_dashboard_app(engine: 'TradingEngine'):  # noqa: C901
    """Build the FastAPI app with WebSocket and REST endpoints."""
    try:
        from fastapi import FastAPI, WebSocket
        from fastapi.responses import FileResponse, JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            'FastAPI not installed. Install with: pip install fastapi uvicorn'
        ) from exc

    app = FastAPI(title='Oracle3 Dashboard', version='1.0.0')

    # Track active WebSocket connections for broadcasting
    ws_clients: set[Any] = set()

    @app.get('/')
    async def index():
        """Serve the dashboard HTML."""
        return FileResponse(STATIC_DIR / 'index.html', media_type='text/html')

    @app.get('/api/state')
    async def get_state():
        """One-shot full state snapshot."""
        return JSONResponse(_serialize_snapshot(engine))

    @app.post('/api/command/{cmd}')
    async def send_command(cmd: str):
        """Execute a control command (pause/resume/stop)."""
        if cmd == 'pause':
            engine._data_paused = True
            strategy = getattr(engine, 'strategy', None)
            trader = getattr(engine, 'trader', None)
            if strategy is not None:
                strategy.set_paused(True)
            if trader is not None:
                trader.set_read_only(True)
            return JSONResponse({'ok': True, 'status': 'paused'})
        elif cmd == 'resume':
            engine._data_paused = False
            strategy = getattr(engine, 'strategy', None)
            trader = getattr(engine, 'trader', None)
            if strategy is not None:
                strategy.set_paused(False)
            if trader is not None:
                trader.set_read_only(False)
            return JSONResponse({'ok': True, 'status': 'running'})
        elif cmd == 'stop':
            asyncio.ensure_future(engine.stop())
            return JSONResponse({'ok': True, 'status': 'stopping'})
        else:
            return JSONResponse(
                {'ok': False, 'error': f'Unknown command: {cmd}'}, status_code=400
            )

    @app.get('/api/markets')
    async def get_markets():
        """Return active market tickers from the engine's order books."""
        from oracle3.ticker.ticker import CashTicker as CT

        md = getattr(engine, 'market_data', None)
        if md is None:
            return JSONResponse({'markets': []})
        tickers = []
        for ticker in list(md.order_books.keys()):
            if isinstance(ticker, CT):
                continue
            tickers.append({
                'symbol': ticker.symbol,
                'name': getattr(ticker, 'name', '') or ticker.symbol,
            })
        return JSONResponse({'markets': tickers})

    @app.websocket('/ws')
    async def websocket_endpoint(websocket: WebSocket):
        from starlette.websockets import WebSocketDisconnect

        await websocket.accept()
        ws_clients.add(websocket)
        try:
            while True:
                try:
                    state = _serialize_snapshot(engine)
                    await websocket.send_json(state)
                except Exception:
                    logger.debug('WebSocket send failed', exc_info=True)
                    break
                await asyncio.sleep(2.0)
        except WebSocketDisconnect:
            pass
        finally:
            ws_clients.discard(websocket)

    return app


class DashboardServer:
    """Manages the FastAPI dashboard server lifecycle.

    Runs uvicorn in a background thread so the main asyncio loop
    remains free for the TradingEngine.
    """

    def __init__(
        self,
        engine: 'TradingEngine',
        host: str = '0.0.0.0',
        port: int = 3000,
    ):
        self.engine = engine
        self.host = host
        self.port = port
        self._server_thread: threading.Thread | None = None
        self._uvicorn_server: Any = None

    def start(self) -> None:
        """Start the dashboard server in a background thread."""
        import uvicorn

        app = _build_dashboard_app(self.engine)
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level='warning',
            ws='wsproto',
        )
        self._uvicorn_server = uvicorn.Server(config)

        def _run():
            asyncio.run(self._uvicorn_server.serve())

        self._server_thread = threading.Thread(target=_run, daemon=True, name='dashboard')
        self._server_thread.start()
        logger.info('Dashboard server started on http://%s:%d', self.host, self.port)

    def stop(self) -> None:
        """Signal the uvicorn server to shut down."""
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
