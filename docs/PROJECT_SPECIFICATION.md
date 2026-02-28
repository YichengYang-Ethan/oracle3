# Oracle3 Project Specification

## 1. Core Features and Problems Solved

### 1.1 Core Features

**Oracle3** (Social World Model Trading Agent) is an intelligent trading agent system designed for the **Polymarket prediction market**, built on the Social World Model concept. Its core features include:

| Module                           | Description                                                                          |
| -------------------------------- | ------------------------------------------------------------------------------------ |
| **Real-time Market Integration** | Connects to Polymarket's CLOB (Central Limit Order Book) API for live trading        |
| **News Sentiment Analysis**      | Integrates news APIs and RSS feeds to analyze market-relevant news events            |
| **LLM-driven Decision Making**   | Uses large language models to analyze news content and generate trading signals      |
| **Risk Management**              | Multi-level risk managers with configurable position, drawdown, and per-trade limits |
| **Backtesting Framework**        | Strategy validation using historical data                                            |
| **Paper Trading**                | Simulated trading mode for strategy testing without real capital risk                |
| **Performance Analytics**        | Provides Sharpe ratio, maximum drawdown, win rate, and other metrics                 |

### 1.2 Problems Solved

1. **Automated prediction market trading**: Automates the connection between news, order book events, and trading decisions
2. **Risk-controlled live and simulated trading**: Reduces live-trading trial-and-error cost through risk managers and paper trading
3. **Reusable and extensible strategies**: Unified strategy interface for easy customization and backtesting
4. **Unified multi-source data ingestion**: Abstract data source interface supporting historical, news, RSS, and Polymarket live data

---

## 2. Technology Stack

### 2.1 Language and Runtime

- **Python**: >= 3.10, < 3.12

### 2.2 Core Frameworks and Libraries

| Category             | Technology     | Purpose                                |
| -------------------- | -------------- | -------------------------------------- |
| **CLI**              | Click          | Command-line interface                 |
| **Terminal Display** | Rich           | Monitoring dashboards, tables, layouts |
| **Data Validation**  | Pydantic       | Data model validation                  |
| **Type Checking**    | Beartype       | Runtime type checking                  |
| **Polymarket**       | py-clob-client | Polymarket CLOB API interaction        |
| **HTTP Client**      | httpx          | Asynchronous HTTP requests             |
| **RSS Parsing**      | feedparser     | RSS feed parsing                       |

### 2.3 Databases and Middleware

- **No dedicated database**: Uses local JSONL files to cache events and news (e.g., `events_cache.jsonl`, `news_cache.jsonl`)
- **No message queue**: Uses Python `asyncio.Queue` for event streaming

### 2.4 Development and Testing Tools

- **Package management**: Poetry
- **Code style**: Ruff (replaces Black / isort)
- **Type checking**: mypy (strict mode)
- **Testing**: pytest, pytest-asyncio, pytest-cov, pytest-mock, hypothesis
- **Pre-commit**: pre-commit

---

## 3. Project Directory Structure

```
qfj/
├── oracle3/                    # Main package
│   ├── cli/                     # CLI
│   │   ├── cli.py               # CLI entry point
│   │   ├── monitor.py           # Trading monitor command
│   │   └── utils.py
│   ├── core/                    # Core engine
│   │   └── trading_engine.py    # Trading engine (event loop and driver)
│   ├── strategy/                # Strategy layer
│   │   ├── strategy.py         # Strategy abstract base class
│   │   ├── simple_strategy.py  # LLM strategy
│   │   └── test_strategy.py    # Test strategy
│   ├── trader/                  # Trade execution layer
│   │   ├── trader.py           # Trader abstract base class
│   │   ├── paper_trader.py     # Paper trading (simulation)
│   │   ├── polymarket_trader.py # Polymarket live trading
│   │   └── types.py            # Trade type definitions
│   ├── data/                    # Data layer
│   │   ├── data_source.py      # Data source abstract base class
│   │   ├── market_data_manager.py # Market data management
│   │   ├── backtest/
│   │   │   └── historical_data_source.py # Historical backtest data source
│   │   └── live/
│   │       └── live_data_source.py # Live data sources (Polymarket/News/RSS)
│   ├── events/                  # Event system
│   │   └── events.py           # OrderBookEvent, NewsEvent, PriceChangeEvent
│   ├── ticker/                  # Instrument identifiers
│   │   └── ticker.py           # Ticker, PolyMarketTicker, CashTicker
│   ├── order/                   # Orders
│   │   └── order_book.py       # Order book management
│   ├── position/                # Positions
│   │   └── position_manager.py # Position and PnL management
│   ├── risk/                    # Risk control
│   │   └── risk_manager.py     # NoRisk/Standard/Conservative/Aggressive
│   ├── analytics/                # Analytics
│   │   └── performance_analyzer.py # Performance analysis
│   ├── backtest/                # Backtesting
│   │   └── backtester.py       # Backtest orchestration
│   └── live/                    # Live trading
│       └── live_trader.py      # Live/paper trading entry point
├── examples/                     # Examples
│   ├── backtest_example.py
│   ├── live_paper_trading_example.py
│   ├── custom_strategy_example.py
│   ├── performance_analysis_example.py
│   ├── monitor_example.py
│   └── demo_monitor.py
├── scripts/                      # Utility scripts
│   ├── get_live_polymarket_data.py
│   └── get_live_news_data.py
├── tests/                        # Unit tests
├── docs/                         # Documentation
├── .github/                      # CI/CD and issue templates
├── pyproject.toml               # Poetry configuration
├── README.md
└── .pre-commit-config.yaml
```

### 3.1 Key Directory Descriptions

| Directory             | Purpose                                                                                         |
| --------------------- | ----------------------------------------------------------------------------------------------- |
| `oracle3/core/`      | Trading engine: event loop, strategy invocation, trade execution scheduling                     |
| `oracle3/strategy/`  | Strategy definitions: implements `process_event` and calls `trader.place_order`                 |
| `oracle3/trader/`    | Trade execution: includes simulation (PaperTrader) and live (PolymarketTrader)                  |
| `oracle3/data/`      | Data source abstraction: historical, Polymarket, news, and RSS implementations                  |
| `oracle3/events/`    | Event types: OrderBookEvent, NewsEvent, PriceChangeEvent                                        |
| `oracle3/risk/`      | Risk control layer: per-trade, per-instrument, total exposure, drawdown, and daily loss limits  |
| `oracle3/position/`  | Position tracking and PnL computation                                                           |
| `oracle3/analytics/` | Sharpe ratio, win rate, maximum drawdown, profit/loss ratio, and other performance metrics      |
| `oracle3/live/`      | Live/paper trading entry points (`run_live_paper_trading`, `run_live_polymarket_trading`, etc.) |

---

## 4. Program Entry Points

### 4.1 CLI Entry Point (Main)

Defined in `pyproject.toml`:

```toml
[tool.poetry.scripts]
oracle3 = "oracle3.cli.cli:cli"
```

The main entry point is: **`oracle3.cli.cli:cli`**.

After installation, the CLI can be invoked directly:

```bash
oracle3 monitor           # Monitor
oracle3 monitor --watch   # Live refresh
```

### 4.2 Entry Point Overview

| Entry Point       | File                                                                       | Description                                  |
| ----------------- | -------------------------------------------------------------------------- | -------------------------------------------- |
| **CLI**           | `oracle3/cli/cli.py`                                                      | `cli()` — registers the `monitor` subcommand |
| **Backtesting**   | `examples/backtest_example.py`                                             | Run this script directly for backtesting     |
| **Paper Trading** | `examples/live_paper_trading_example.py` or `oracle3/live/live_trader.py` | Run via `run_live_paper_trading()`           |
| **Live Trading**  | `oracle3/live/live_trader.py`                                             | Run via `run_live_polymarket_trading()`      |

### 4.3 Execution Flow Overview

```
User command / script
    ↓
CLI (cli.py) or examples / live_trader
    ↓
TradingEngine(data_source, strategy, trader)
    ↓
engine.start(): loops calling data_source.get_next_event()
    ↓
OrderBookEvent → market_data.process_orderbook_event()
PriceChangeEvent → market_data.process_price_change_event()
    ↓
strategy.process_event(event, trader)
    ↓
Strategy internally calls trader.place_order()
    ↓
PaperTrader or PolymarketTrader executes order and updates position_manager
```

---

## 5. Appendix: Typical Run Commands

### Backtesting

```bash
python examples/backtest_example.py
```

### Paper Trading (RSS News)

```bash
python examples/live_paper_trading_example.py
# or
python -c "
import asyncio
from decimal import Decimal
from oracle3.data.live.live_data_source import LiveRSSNewsDataSource
from oracle3.live.live_trader import run_live_paper_trading
from oracle3.strategy.test_strategy import TestStrategy

asyncio.run(run_live_paper_trading(
    data_source=LiveRSSNewsDataSource(polling_interval=60.0),
    strategy=TestStrategy(),
    initial_capital=Decimal('10000'),
    duration=300,
))
"
```

### Monitoring

```bash
oracle3 monitor
oracle3 monitor --watch --refresh 1.0
```

---

_Document version: Based on current project code structure_
