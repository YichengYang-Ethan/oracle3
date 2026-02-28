# Oracle3

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/pypi/v/oracle3.svg)](https://pypi.org/project/oracle3/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](https://github.com/ulab-uiuc/oracle3/blob/main/LICENSE)

**Oracle3** is an intelligent trading agent for [Polymarket](https://polymarket.com/) and [Kalshi](https://kalshi.com/) prediction markets, powered by Social World Models and LLM-driven decision making. It combines real-time market data, news sentiment analysis, and Large Language Models to automate trading decisions.

## Key Features

- **Real-time Market Integration** — Connects to Polymarket's CLOB API and Kalshi for live order book data and trading
- **LLM-Powered Strategies** — Analyzes news events with Large Language Models to generate trading signals
- **Multi-Source Data Ingestion** — Polymarket live data, News API, and RSS feeds (WSJ, etc.)
- **Risk Management** — Configurable limits on trade size, position size, drawdown, daily loss, and more
- **Backtesting** — Test strategies against historical data before going live
- **Paper Trading** — Simulate live trading without real capital
- **Performance Analytics** — Sharpe ratio, max drawdown, win rate, profit factor, equity curve

## Architecture

```
TradingEngine
  ├── DataSource          (Live / Historical / RSS / News API)
  ├── Strategy            (LLM-based / Custom)
  └── Trader              (PaperTrader / PolymarketTrader / KalshiTrader)
       ├── RiskManager    (Standard / Conservative / Aggressive)
       └── PositionManager
```

## Installation

```bash
pip install oracle3
```

Or install from source with [Poetry](https://python-poetry.org/):

```bash
git clone https://github.com/ulab-uiuc/oracle3.git
cd oracle3
pip install poetry
poetry install
```

!!! note "Requirements"
Python >= 3.10, < 3.12

## Quick Start

### Run a Backtest

```python
import asyncio
from decimal import Decimal

from oracle3.core.trading_engine import TradingEngine
from oracle3.data.backtest.historical_data_source import HistoricalDataSource
from oracle3.data.market_data_manager import MarketDataManager
from oracle3.position.position_manager import Position, PositionManager
from oracle3.risk.risk_manager import NoRiskManager
from oracle3.strategy.test_strategy import TestStrategy
from oracle3.ticker.ticker import CashTicker, PolyMarketTicker
from oracle3.trader.paper_trader import PaperTrader

async def run():
    ticker = PolyMarketTicker(symbol="my_market", name="My Market",
                               market_id="123", event_id="456")
    data_source = HistoricalDataSource("data.jsonl", ticker)
    market_data = MarketDataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(ticker=CashTicker.POLYMARKET_USDC, quantity=Decimal("10000"),
                 average_cost=Decimal("0"), realized_pnl=Decimal("0"))
    )
    trader = PaperTrader(market_data=market_data, risk_manager=NoRiskManager(),
                         position_manager=position_manager,
                         min_fill_rate=Decimal("0.5"), max_fill_rate=Decimal("1.0"),
                         commission_rate=Decimal("0.0"))
    engine = TradingEngine(data_source=data_source, strategy=TestStrategy(), trader=trader)
    await engine.start()
    print(f"Final PnL: {position_manager.get_total_realized_pnl()}")

asyncio.run(run())
```

### Custom Strategy

```python
from oracle3.strategy.strategy import Strategy
from oracle3.events.events import Event
from oracle3.trader.trader import Trader

class MyStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Your logic here
        pass
```

## CLI Commands

```bash
# Strategy scaffolding + validation
oracle3 strategy create --output ./strategies/my_strategy.py --class-name MyStrategy
oracle3 strategy validate --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Backtest mode
oracle3 backtest run \
  --history-file ./data/history.jsonl \
  --market-id M1 --event-id E1 \
  --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Paper trading (simulation with live data)
oracle3 paper run --exchange polymarket --strategy-ref ./strategies/my_strategy.py:MyStrategy
oracle3 paper run --exchange kalshi --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Real trading
oracle3 live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY"
oracle3 live run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH"

# Operator monitor + emergency control
oracle3 monitor
oracle3 trade status
oracle3 trade pause
oracle3 trade resume
oracle3 trade stop
```

## Risk Management

Three built-in tiers:

| Setting        | Conservative | Aggressive |
| -------------- | ------------ | ---------- |
| Max Trade Size | $500         | $5,000     |
| Max Position   | $2,000       | $20,000    |
| Max Exposure   | $10,000      | $100,000   |
| Max Drawdown   | 10%          | 30%        |

## Environment Variables

```bash
export POLYMARKET_PRIVATE_KEY="your_private_key"   # Required for live trading
export KALSHI_API_KEY_ID="your_kalshi_key_id"      # Required for Kalshi live trading
export KALSHI_PRIVATE_KEY_PATH="/path/key.pem"     # Required for Kalshi live trading
export NEWS_API_KEY="your_news_api_key"             # Optional, for News API source
```

## License

[Apache 2.0](https://github.com/ulab-uiuc/oracle3/blob/main/LICENSE)

!!! warning "Disclaimer"
This software is for **educational and research purposes only**. Trading involves substantial risk of loss. Always test strategies with paper trading before using real funds.
