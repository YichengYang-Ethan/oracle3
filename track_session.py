"""Track Oracle3 dashboard session — log snapshots every 10 minutes."""

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / 'session_logs'
LOG_DIR.mkdir(exist_ok=True)

SESSION_ID = datetime.now().strftime('%Y%m%d_%H%M%S')
LOG_FILE = LOG_DIR / f'session_{SESSION_ID}.jsonl'
SUMMARY_FILE = LOG_DIR / f'session_{SESSION_ID}_summary.txt'

API_URL = 'http://localhost:3000/api/state'
INTERVAL = 600  # 10 minutes


def fetch_state():
    try:
        with urllib.request.urlopen(API_URL, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return None


def fmt_ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log_snapshot(s, elapsed_min):
    p = s['portfolio']
    equity = float(p['equity'])
    cash = float(p['cash'])
    total_pnl = float(p['total_pnl'])
    perf = s.get('performance', {})
    n_trades = perf.get('total_trades', 0)
    win_rate = float(perf.get('win_rate', 0))
    sharpe = float(perf.get('sharpe_ratio', 0))
    max_dd = float(perf.get('max_drawdown', 0))

    line = (
        f'[{fmt_ts()}] '
        f't={elapsed_min}min | '
        f'Equity=${equity:,.2f} | '
        f'P&L=${total_pnl:+,.2f} | '
        f'Cash=${cash:,.2f} | '
        f'Trades={n_trades} | '
        f'WinRate={win_rate*100:.1f}% | '
        f'Sharpe={sharpe:.2f} | '
        f'MaxDD={max_dd*100:.2f}% | '
        f'Events={s["event_count"]} | '
        f'Positions={len(s["positions"])} | '
        f'Decisions={len(s["decisions"])} | '
        f'OBs={len(s["order_books"])}'
    )
    print(line)

    # Append full state as JSONL
    with open(LOG_FILE, 'a') as f:
        snapshot = {
            'timestamp': fmt_ts(),
            'elapsed_min': elapsed_min,
            'equity': equity,
            'cash': cash,
            'total_pnl': total_pnl,
            'realized_pnl': float(p['realized_pnl']),
            'unrealized_pnl': float(p['unrealized_pnl']),
            'exposure_pct': p['exposure_pct'],
            'n_trades': n_trades,
            'n_positions': len(s['positions']),
            'n_decisions': len(s['decisions']),
            'n_order_books': len(s['order_books']),
            'n_events': s['event_count'],
            'win_rate': win_rate,
            'sharpe': sharpe,
            'max_dd': max_dd,
            'positions': s['positions'],
            'decisions': s['decisions'][-10:],
            'trades': s['trades'][-10:],
            'equity_curve': s.get('equity_curve', []),
            'performance': perf,
        }
        f.write(json.dumps(snapshot) + '\n')


def write_summary():
    """Generate a text summary from the JSONL log."""
    snapshots = []
    with open(LOG_FILE) as f:
        for line in f:
            snapshots.append(json.loads(line))

    if not snapshots:
        return

    first = snapshots[0]
    last = snapshots[-1]

    with open(SUMMARY_FILE, 'w') as f:
        f.write('=' * 60 + '\n')
        f.write('  Oracle3 Paper Trading Session Summary\n')
        f.write('=' * 60 + '\n\n')
        f.write(f'Session ID: {SESSION_ID}\n')
        f.write(f'Start: {first["timestamp"]}\n')
        f.write(f'End:   {last["timestamp"]}\n')
        f.write(f'Duration: {last["elapsed_min"]} minutes\n\n')

        f.write('--- Portfolio ---\n')
        f.write(f'Initial Capital: $10,000.00\n')
        f.write(f'Final Equity:    ${last["equity"]:,.2f}\n')
        f.write(f'Total P&L:       ${last["total_pnl"]:+,.2f}\n')
        f.write(f'Return:          {(last["equity"]-10000)/100:+.2f}%\n\n')

        f.write('--- Performance ---\n')
        f.write(f'Total Trades:    {last["n_trades"]}\n')
        f.write(f'Win Rate:        {last["win_rate"]*100:.1f}%\n')
        f.write(f'Sharpe Ratio:    {last["sharpe"]:.4f}\n')
        f.write(f'Max Drawdown:    {last["max_dd"]*100:.2f}%\n\n')

        f.write('--- Equity Timeline ---\n')
        for snap in snapshots:
            pnl = snap['total_pnl']
            bar = '+' * max(0, int(pnl / 10)) if pnl >= 0 else '-' * max(0, int(-pnl / 10))
            f.write(f'  t={snap["elapsed_min"]:>4}min  ${snap["equity"]:>10,.2f}  P&L: ${pnl:>+8,.2f}  {bar}\n')

        f.write('\n--- Final Positions ---\n')
        for pos in last.get('positions', []):
            f.write(f'  {pos["symbol"]}: {pos["qty"]}x @ ${float(pos["current_price"]):.4f}  P&L: ${float(pos["unrealized_pnl"]):+.2f}\n')
        if not last.get('positions'):
            f.write('  (no positions)\n')

        f.write('\n--- Recent Trades ---\n')
        for t in last.get('trades', []):
            f.write(f'  {t["time"]} {t["side"]:4s} {t["name"][:30]:30s} @ ${float(t["price"]):.4f} x{t["qty"]} [{t["status"]}]\n')
        if not last.get('trades'):
            f.write('  (no trades)\n')

        f.write('\n' + '=' * 60 + '\n')

    print(f'\nSummary written to: {SUMMARY_FILE}')


if __name__ == '__main__':
    print(f'Oracle3 Session Tracker — logging every {INTERVAL//60} min')
    print(f'Log file: {LOG_FILE}')
    print(f'Dashboard: http://localhost:3000')
    print()

    start = time.time()

    # Initial snapshot
    s = fetch_state()
    if s:
        log_snapshot(s, 0)
    else:
        print('ERROR: Cannot connect to dashboard. Is it running?')
        exit(1)

    try:
        while True:
            time.sleep(INTERVAL)
            elapsed = int((time.time() - start) / 60)
            s = fetch_state()
            if s:
                log_snapshot(s, elapsed)
                write_summary()
            else:
                print(f'[{fmt_ts()}] WARNING: Dashboard not responding')

            # Stop after 3.5 hours
            if elapsed >= 210:
                print('3.5 hour limit reached. Stopping tracker.')
                break
    except KeyboardInterrupt:
        print('\nTracker stopped by user.')
    finally:
        s = fetch_state()
        if s:
            elapsed = int((time.time() - start) / 60)
            log_snapshot(s, elapsed)
        write_summary()
        print('Done.')
