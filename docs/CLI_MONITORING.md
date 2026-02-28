# Trading Monitor CLI Documentation

The Oracle3 CLI provides comprehensive monitoring capabilities for your trading activities, positions, and portfolio status.

## Features

The monitoring CLI displays:

- **Portfolio Summary**: Total portfolio value, cash positions, realized/unrealized P&L
- **Active Positions**: Open positions with current prices and P&L breakdown
- **Recent Orders**: Latest order history with status, prices, and commissions
- **Market Snapshot**: Real-time bid/ask prices and spreads
- **Statistics**: Trading session stats including success rate and position count

## Installation

After installing dependencies:

```bash
poetry install
```

The CLI is available via the `oracle3` command.

## CLI Commands

### Monitor Command

```bash
oracle3 monitor [OPTIONS]
```

#### Options

- `--watch`, `-w`: Enable live monitoring mode with continuous updates
- `--refresh FLOAT`, `-r FLOAT`: Refresh rate in seconds for watch mode (default: 2.0)
- `--config PATH`, `-c PATH`: Path to trading engine config file (optional)

#### Examples

**Single snapshot:**

```bash
oracle3 monitor
```

**Live monitoring mode:**

```bash
oracle3 monitor --watch
```

**Live mode with custom refresh rate:**

```bash
oracle3 monitor -w -r 1.0  # Update every 1 second
```

## Integration with Trading Engine

### Method 1: Using MonitoredTradingEngine Wrapper

The simplest way to add monitoring to your trading workflow:

```python
from oracle3.core.trading_engine import TradingEngine
from oracle3.cli.utils import add_monitoring_to_engine

# Create your trading engine
engine = TradingEngine(
    data_source=data_source,
    strategy=strategy,
    trader=trader
)

# Add monitoring
monitored_engine = add_monitoring_to_engine(
    engine,
    watch=True,        # Enable live monitoring
    refresh_rate=2.0   # Update every 2 seconds
)

# Start trading with live monitoring
await monitored_engine.start()
```

### Method 2: Manual Integration

For more control, use the `TradingMonitor` class directly:

```python
from oracle3.cli.monitor import TradingMonitor

# After initializing your trader and position manager
monitor = TradingMonitor(
    trader=trader,
    position_manager=position_manager
)

# Display a single snapshot
monitor.display_snapshot()

# Or run in live mode
monitor.display_live(refresh_rate=2.0)
```

### Method 3: Background Monitoring

Run the monitor in a separate thread while your trading engine runs:

```python
import asyncio
import threading
from oracle3.cli.monitor import TradingMonitor

def run_monitor(trader, position_manager, refresh_rate=2.0):
    monitor = TradingMonitor(trader, position_manager)
    monitor.display_live(refresh_rate=refresh_rate)

# Start monitor in background thread
monitor_thread = threading.Thread(
    target=run_monitor,
    args=(trader, position_manager),
    daemon=True
)
monitor_thread.start()

# Run trading engine
await trading_engine.start()
```

## Monitor Display Sections

### Portfolio Summary

Shows overall portfolio health:

- Total portfolio value across all collaterals
- Cash positions (e.g., USDC balance)
- Realized P&L (closed positions)
- Unrealized P&L (open positions)
- Total P&L (sum of realized + unrealized)

Values are color-coded:

- Green: Positive P&L
- Red: Negative P&L
- White: Neutral

### Active Positions

Displays all open (non-cash) positions:

- Ticker symbol
- Quantity held
- Average cost basis
- Current market price (best bid)
- Unrealized P&L
- Realized P&L (from partial closes)

### Recent Orders

Shows the last 10 orders with:

- Status (FILLED, PARTIALLY_FILLED, REJECTED, etc.)
- Side (BUY/SELL)
- Ticker symbol
- Limit price
- Filled quantity
- Average fill price
- Total commission

Status color coding:

- Green: FILLED
- Yellow: PARTIALLY_FILLED
- Red: REJECTED
- Dim: CANCELLED

### Market Snapshot

Real-time market data for active positions:

- Best bid price
- Best ask price
- Spread (ask - bid)
- Spread percentage

### Statistics

Session statistics:

- Runtime (how long the session has been active)
- Total orders placed
- Number of filled orders
- Number of rejected orders
- Success rate (filled / total)
- Active positions count

## Example Output

```
┌─────────────────────────────────────────────────────────────────┐
│            Oracle3 - Trading Monitor                          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────┐  ┌──────────────────────────────────┐
│ Portfolio Summary       │  │ Active Positions                  │
│                         │  │                                   │
│ Total Portfolio Value   │  │ Ticker    Qty  Avg Cost  Current │
│   $10,150.25            │  │ TRUMP-YES 100  $0.4500   $0.4800 │
│   USDC    $5,000.00     │  │ BIDEN-NO   50  $0.6000   $0.5800 │
│ Realized P&L   -$50.00  │  │                                   │
│ Unrealized P&L +$200.25 │  │ Unrealized P&L: +$3.00, -$1.00   │
│ Total P&L      +$150.25 │  │                                   │
└─────────────────────────┘  └──────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Recent Orders                                                   │
│                                                                 │
│ Status  Side  Ticker     Limit    Filled  Avg Price  Commission│
│ FILLED  BUY   TRUMP-YES  $0.4500  100.00  $0.4500    $0.45     │
│ FILLED  SELL  BIDEN-NO   $0.5800   50.00  $0.5800    $0.29     │
└─────────────────────────────────────────────────────────────────┘

┌──────────────────────┐  ┌──────────────────────┐
│ Market Snapshot      │  │ Statistics           │
│                      │  │                      │
│ Ticker    Bid   Ask  │  │ Runtime       1:23:45│
│ TRUMP-YES $0.47 $0.49│  │ Total Orders      42 │
│ BIDEN-NO  $0.57 $0.59│  │ Filled Orders     36 │
│                      │  │ Rejected Orders    6 │
│ Spread: 0.42%, 3.39% │  │ Success Rate  85.7% │
└──────────────────────┘  └──────────────────────┘

Press Ctrl+C to exit | Last updated: 2025-02-06 14:30:15
```

## Performance Considerations

- The monitor reads state from the trader and position manager without modifying it
- Watch mode uses Rich's `Live` display for efficient terminal updates
- Recommended refresh rates:
  - High-frequency trading: 0.5-1.0 seconds
  - Normal trading: 2.0-5.0 seconds
  - Low-frequency trading: 5.0-10.0 seconds
- The monitor runs in a separate thread/process and won't block trading execution

## Troubleshooting

### Monitor shows "No positions" but I have trades

The monitor reads from the `PositionManager`. Ensure your trades are being properly processed through `position_manager.apply_trade()`.

### Live mode not updating

Check that:

1. The trading engine is running and processing events
2. The trader and position manager are being updated
3. Your terminal supports Rich's live display features

### Performance issues in watch mode

Try increasing the refresh rate (e.g., `-r 5.0` for 5 second updates) to reduce CPU usage.

## Advanced Usage

### Custom Monitoring Periods

To monitor specific trading sessions:

```python
from datetime import datetime

# Record start time
monitor.start_time = datetime.now()

# Run your trading session
await trading_engine.start()

# Display final snapshot
monitor.display_snapshot()
```

### Monitoring Multiple Engines

You can create separate monitors for different trading strategies:

```python
monitor_strategy_a = TradingMonitor(trader_a, position_manager_a)
monitor_strategy_b = TradingMonitor(trader_b, position_manager_b)

# Display side by side or sequentially
monitor_strategy_a.display_snapshot()
monitor_strategy_b.display_snapshot()
```

## Future Enhancements

Planned features:

- Export monitoring data to CSV/JSON
- Historical P&L charts
- Performance metrics (Sharpe ratio, max drawdown)
- Alert system for position limits
- Multi-strategy comparison view
- WebSocket-based real-time updates
