"""Standalone preview server replaying real DFlow Solana backtest results.

Loads data/backtest_solana_results.json (produced by run_dflow_backtest.py)
and progressively reveals the equity curve, trades, and decisions to simulate
a live session.

Usage: python3 preview_dashboard.py
Then open http://localhost:3456 in your browser.
"""

import json
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

STATIC_DIR = Path(__file__).parent / 'oracle3' / 'dashboard' / 'static'
RESULTS_FILE = Path(__file__).parent / 'data' / 'backtest_solana_results.json'
PORT = 3456

# ---- Load backtest results ----

with open(RESULTS_FILE) as f:
    BT = json.load(f)

start_time = time.time()
TOTAL_EQUITY_POINTS = len(BT.get('equity_curve', []))
TOTAL_DECISIONS = len(BT.get('decisions', []))
TOTAL_TRADES = len(BT.get('trades', []))

# Replay speed: reveal all data over ~90 seconds
REPLAY_DURATION = 90.0


def _ts():
    elapsed = int(time.time() - start_time)
    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    return f'{h:02d}:{m:02d}:{s:02d}'


def _progress():
    """Return 0.0 to 1.0 based on elapsed time."""
    elapsed = time.time() - start_time
    return min(1.0, elapsed / REPLAY_DURATION)


def build_state():
    """Build state from real backtest data, progressively revealed."""
    p = _progress()
    idx_eq = max(1, int(p * TOTAL_EQUITY_POINTS))
    idx_dec = max(0, int(p * TOTAL_DECISIONS))
    idx_tr = max(0, int(p * TOTAL_TRADES))

    # Equity curve slice
    eq_slice = BT['equity_curve'][:idx_eq]
    current_equity = float(eq_slice[-1]['equity']) if eq_slice else 10000.0
    initial = float(BT['initial_capital'])
    pnl = current_equity - initial

    # Positions: show all once replay > 50%
    positions = BT['positions'] if p > 0.5 else BT['positions'][:max(1, int(p * 2 * len(BT['positions'])))]

    # Compute unrealized from visible positions
    unrealized = sum(float(pos.get('unrealized_pnl', 0)) for pos in positions)
    realized = pnl - unrealized

    # Cash estimate
    pos_value = sum(float(pos['qty']) * float(pos['avg_cost']) for pos in positions)
    cash = initial - pos_value

    # Performance stats (from final backtest)
    perf = BT.get('performance', {})
    scaled_trades = max(0, int(p * int(perf.get('total_trades', 0))))
    scaled_wins = max(0, int(p * int(perf.get('winning_trades', 0))))

    # Order books: show top 20 with highest spread
    all_obs = BT.get('order_books', [])
    sorted_obs = sorted(all_obs, key=lambda o: float(o.get('spread', 0)), reverse=True)
    order_books = sorted_obs[:20]

    return {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'running': True,
        'paused': False,
        'uptime': _ts(),
        'event_count': int(p * BT.get('event_count', 0)),
        'initial_capital': BT['initial_capital'],
        'network': 'Solana Mainnet',
        'portfolio': {
            'equity': str(round(current_equity, 2)),
            'cash': str(round(cash, 2)),
            'realized_pnl': str(round(realized, 2)),
            'unrealized_pnl': str(round(unrealized, 2)),
            'total_pnl': str(round(pnl, 2)),
            'exposure_pct': round(pos_value / current_equity * 100, 1) if current_equity > 0 else 0,
        },
        'positions': positions,
        'order_books': order_books,
        'decisions': BT['decisions'][:idx_dec],
        'trades': BT['trades'][:idx_tr],
        'performance': {
            'total_trades': scaled_trades,
            'winning_trades': scaled_wins,
            'losing_trades': scaled_trades - scaled_wins,
            'win_rate': perf.get('win_rate', '0'),
            'average_profit': perf.get('average_profit', '0'),
            'average_loss': perf.get('average_loss', '0'),
            'max_drawdown': perf.get('max_drawdown', '0'),
            'sharpe_ratio': perf.get('sharpe_ratio', '0'),
            'profit_factor': perf.get('profit_factor', '0'),
            'total_pnl': str(round(pnl, 2)),
            'max_consecutive_wins': perf.get('max_consecutive_wins', 0),
            'max_consecutive_losses': perf.get('max_consecutive_losses', 0),
        },
        'equity_curve': [e['equity'] for e in eq_slice],
        'activity_log': [],
        'news': [
            {'timestamp': _ts(), 'source': 'DFlow', 'title': 'Solana prediction markets: 2500+ active tickers'},
            {'timestamp': _ts(), 'source': 'Oracle3', 'title': f'Backtest replay: {BT["source"]}'},
            {'timestamp': _ts(), 'source': 'Strategy', 'title': 'AdaptiveOnChain: OB imbalance + EMA momentum'},
        ],
        'wallet': BT.get('wallet', '7RQ3YL4cLNbQbwAUHBP6GzdRbG6NRng8qBcHbiDrf8Ae'),
        'wallet_short': '7RQ3...f8Ae',
    }


# ---- Patched index.html that uses polling instead of WebSocket ----

def build_polling_html():
    """Read index.html and patch it to use polling /api/state instead of WebSocket."""
    html = (STATIC_DIR / 'index.html').read_text()

    old_connect = """function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    document.getElementById('wsDot').classList.add('connected');
    document.getElementById('wsLabel').textContent = 'Live';
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };
  ws.onmessage = (e) => {
    try { updateDashboard(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
  ws.onclose = () => {
    document.getElementById('wsDot').classList.remove('connected');
    document.getElementById('wsLabel').textContent = 'Reconnecting';
    reconnectTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => { ws.close(); };
}"""

    new_connect = """function connect() {
  document.getElementById('wsDot').classList.add('connected');
  document.getElementById('wsLabel').textContent = 'Live';
  setInterval(() => {
    fetch('/api/state').then(r => r.json()).then(d => {
      try { updateDashboard(d); } catch(e) { console.error(e); }
    }).catch(() => {});
  }, 1500);
}"""

    html = html.replace(old_connect, new_connect)
    return html


# ---- HTTP Server ----

class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            content = build_polling_html().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == '/api/state':
            state = build_state()
            content = json.dumps(state).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.startswith('/api/command/'):
            cmd = self.path.split('/')[-1]
            result = json.dumps({'ok': True, 'status': cmd}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # Suppress logs


if __name__ == '__main__':
    print()
    print('  \033[38;5;75m\033[1mOracle3 Dashboard — DFlow Backtest Replay\033[0m')
    print('  \033[2m──────────────────────────────────────────\033[0m')
    print(f'  \033[38;5;84mData:\033[0m {RESULTS_FILE.name}')
    print(f'  \033[38;5;84mEquity points:\033[0m {TOTAL_EQUITY_POINTS}  |  Decisions: {TOTAL_DECISIONS}  |  Trades: {TOTAL_TRADES}')
    print(f'  \033[38;5;84mOpen in browser:\033[0m http://localhost:{PORT}')
    print(f'  \033[2mReplay duration: {REPLAY_DURATION:.0f}s — Press Ctrl+C to stop\033[0m')
    print()

    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')
        server.server_close()
