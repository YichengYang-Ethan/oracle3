# CLI Monitoring Quick Start

Get started with trading monitoring in 5 minutes.

## Installation

```bash
# Install dependencies
poetry install

# Verify installation
oracle3 --version
```

## Usage

### 1. Quick Demo (Recommended First Step)

Run the demo to see the monitor in action:

```bash
# Run with live monitoring
python examples/demo_monitor.py --watch

# Or snapshot mode
python examples/demo_monitor.py
```

### 2. Basic Monitoring Commands

```bash
# Single snapshot
oracle3 monitor

# Live updates (default: 2 second refresh)
oracle3 monitor --watch

# Faster updates (1 second refresh)
oracle3 monitor -w -r 1.0

# Slower updates (5 second refresh)
oracle3 monitor -w -r 5.0
```

### 3. Integration Patterns

#### Pattern A: Wrap Your Existing Engine

```python
from oracle3.cli.utils import add_monitoring_to_engine

# Your existing code
engine = TradingEngine(data_source, strategy, trader)

# Add monitoring (one line!)
monitored = add_monitoring_to_engine(engine, watch=True, refresh_rate=2.0)

# Start with monitoring
await monitored.start()
```

#### Pattern B: Manual Monitor Control

```python
from oracle3.cli.monitor import TradingMonitor

# After initializing trader
monitor = TradingMonitor(trader, trader.position_manager)

# Show snapshot
monitor.display_snapshot()

# Or live mode
monitor.display_live(refresh_rate=2.0)
```

#### Pattern C: Background Monitor

```python
import threading
from oracle3.cli.monitor import TradingMonitor

def run_bg_monitor(trader, pm):
    monitor = TradingMonitor(trader, pm)
    monitor.display_live(2.0)

# Start monitor thread
threading.Thread(
    target=run_bg_monitor,
    args=(trader, position_manager),
    daemon=True
).start()

# Run trading engine
await engine.start()
```

## What You'll See

```
┌────────────────────────────────────────┐
│     Oracle3 - Trading Monitor        │
└────────────────────────────────────────┘

Portfolio Summary          Active Positions
  Total Value: $10,150    Ticker  Qty  P&L
  Cash: $5,000            BTC-YES 100  +$50
  P&L: +$150

Recent Orders              Market Snapshot
  [FILLED] BUY 100         BTC-YES $0.45/$0.47
  [REJECTED] SELL 50       Spread: 4.35%

Press Ctrl+C to exit
```

## Common Workflows

### Development/Testing

```bash
# Paper trading with live monitoring
python your_strategy.py --watch --refresh 1.0
```

### Production Trading

```bash
# Conservative monitoring (less CPU)
oracle3 monitor --watch --refresh 5.0
```

### Performance Analysis

```bash
# Snapshot after session
oracle3 monitor > session_report.txt
```

## Keyboard Shortcuts

- `Ctrl+C`: Exit live monitoring
- Terminal scroll: Navigate through history

## Tips

1. **Start with the demo** to understand what monitoring shows
2. **Use watch mode** during development for immediate feedback
3. **Adjust refresh rate** based on trading frequency:
   - High frequency: 0.5-1.0s
   - Normal: 2.0-3.0s
   - Low frequency: 5.0-10.0s
4. **Snapshot mode** is great for end-of-session reports
5. **Background threads** let you monitor without blocking your code

## Troubleshooting

**Q: Monitor shows no data**

- Ensure trader and position_manager are properly initialized
- Check that trades are being applied: `position_manager.apply_trade(trade)`

**Q: Live mode not updating**

- Verify trading engine is running and processing events
- Check terminal supports Rich library (most modern terminals do)

**Q: Performance issues**

- Increase refresh rate: `-r 5.0` or higher
- Reduce terminal window size
- Use snapshot mode instead of live

## Next Steps

- Read [Full CLI Documentation](CLI_MONITORING.md) for advanced features
- Check [examples/monitor_example.py](../examples/monitor_example.py) for integration patterns
- Customize display by extending `TradingMonitor` class

## Support

Issues? Questions? See:

- [CLI Monitoring Documentation](CLI_MONITORING.md)
- [GitHub Issues](https://github.com/YichengYang-Ethan/oracle3/issues)
