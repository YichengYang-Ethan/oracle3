# Oracle3 Architecture

## Overview

Oracle3 is an autonomous on-chain trading agent for prediction markets. It reads on-chain data, generates trade signals via AI and quantitative strategies, signs Solana transactions, and manages risk end-to-end.

## System Architecture

```
                          ┌──────────────────────┐
                          │    Oracle3 CLI        │
                          │  paper│live│blinks    │
                          └──────────┬───────────┘
                                     │
                ┌────────────────────┬┴──────────────────────┐
                │                    │                        │
       ┌────────▼────────┐  ┌────────▼────────┐  ┌──────────▼─────────┐
       │  Agent Strategy │  │ Quant Strategy   │  │  Contrib Strategies│
       │  (LLM + Tools)  │  │ (Momentum/MR/MM) │  │  (Debate/News/Arb) │
       └────────┬────────┘  └────────┬────────┘  └──────────┬─────────┘
                └────────────────────┼──────────────────────┘
                                     │
                          ┌──────────▼───────────┐
                          │    Trading Engine     │
                          │  Event Loop + Risk    │
                          │  + Position Manager   │
                          └──────────┬───────────┘
                                     │
                ┌────────────────────┬┴──────────────────────┐
                │                    │                        │
       ┌────────▼────────┐  ┌────────▼────────┐  ┌──────────▼─────────┐
       │  Solana/DFlow   │  │   Polymarket     │  │      Kalshi        │
       │  SPL Tokens     │  │   CLOB API       │  │    REST API        │
       └─────────────────┘  └─────────────────┘  └────────────────────┘
                │
       ┌────────▼────────┐
       │  Solana Blinks  │  ← Shareable trade URLs
       │  On-Chain Logs  │  ← Memo program audit trail
       └─────────────────┘
```

## Execution Flow

```
1. Data Sources fetch live market data + news + on-chain signals
          ↓
2. AI Agent / Quant Strategy analyzes events (multi-agent pipeline)
          ↓
3. Strategy generates trade signals with confidence scores
          ↓
4. On-Chain Risk Manager validates against portfolio limits + simulates tx
          ↓
5. Trader signs tx, optionally via Jito Bundle for MEV protection
          ↓  May use Flash Loan or Atomic Multi-Leg execution
6. On-Chain Logger writes trade to Solana Memo + updates Reputation
          ↓
7. Live Dashboard shows real-time P&L, equity curve, feature cards
```

## Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ (async/await) |
| Solana | solders + solana-py |
| DFlow | REST API (Metadata + Trade) |
| LLM | OpenAI Agents SDK + LiteLLM |
| Web UI | FastAPI + WebSocket |
| Terminal UI | Textual + Rich |
| CLI | Click |
| Testing | pytest + pytest-asyncio |
| Linting | Ruff + MyPy + pre-commit |
| Types | Pydantic + Beartype |

## Module Reference

### `oracle3/agent/`

Multi-agent coordination pipeline: SignalAgent → RiskAgent → ExecutionAgent. Agents communicate through a shared context and produce structured decisions.

### `oracle3/cli/`

CLI entry point built with Click. Provides commands for market browsing, paper/live trading, backtesting, monitoring, reputation queries, blinks server, and trade log inspection.

### `oracle3/core/`

Trading engine with async event loop, snapshot system for state persistence, and strategy orchestration.

### `oracle3/strategy/`

Strategy framework with a unified interface.

- `agent_strategy.py` — LLM agent with tool calling (place trades, read order books, check positions, fetch news)
- `quant_strategy.py` — Quantitative strategies (OB imbalance, EMA momentum)
- `contrib/` — Contributed strategies: adaptive on-chain, cross-market arbitrage, multi-agent pipeline, Solana agent, debate

### `oracle3/trader/`

Exchange-specific trade execution:

- `solana_trader.py` — Solana/DFlow transaction building, signing, and submission
- `jito_submitter.py` — Jito Bundle MEV protection with configurable tips
- `flash_loan.py` — Flash loan arbitrage (MarginFi/Solend)
- `atomic_trader.py` — Atomic multi-leg trades (DFlow + Jupiter + Drift)
- `polymarket_trader.py` — Polymarket CLOB API trading
- `kalshi_trader.py` — Kalshi REST API trading

### `oracle3/data/`

Data source abstraction layer:

- `live/dflow_data_source.py` — DFlow REST polling
- `live/dflow_ws_data_source.py` — DFlow WebSocket streaming
- `live/coingecko_x402_data_source.py` — CoinGecko SOL/crypto prices
- `live/onchain_signal_source.py` — Whale wallet movements, large SPL transfers, TVL changes

### `oracle3/risk/`

Dual-layer risk management:

- `risk_manager.py` — Local limits (position size, exposure, drawdown, daily loss)
- `onchain_risk_manager.py` — Solana `simulateTransaction` pre-flight validation

### `oracle3/blinks/`

Solana Blinks/Actions server. Generates shareable trade URLs that anyone can execute.

### `oracle3/onchain/`

On-chain trade logging via Solana Memo program and agent reputation scoring (0–100 based on win rate, Sharpe, consistency).

### `oracle3/dashboard/`

Web dashboard built with FastAPI + WebSocket:

- `/` — Classic terminal-style monitoring
- `/live` — Live trading dashboard with 8 feature cards, equity chart, execution pipeline animation, pause/resume/e-stop controls

### `oracle3/position/`

Position tracking and P&L computation across multiple exchanges and collateral types.

### `oracle3/analytics/`

Performance analysis: Sharpe ratio, max drawdown, win rate, profit factor, equity curve.

### `oracle3/backtest/`

Backtesting engine with DFlow episode replay from parquet files.

### `oracle3/events/`

Event types: OrderBookEvent, PriceChangeEvent, NewsEvent, OnChainSignalEvent.

### `coinjure/`

Cross-platform market matching pipeline. Discovers relations (implication, exclusivity, complementary) across Polymarket and Kalshi markets for arbitrage opportunity detection.

## 8 On-Chain Agent Capabilities

| # | Capability | Module |
|---|-----------|--------|
| 1 | Cross-Market Arbitrage | `strategy/contrib/cross_market_arbitrage_strategy.py` |
| 2 | On-Chain Risk Manager | `risk/onchain_risk_manager.py` |
| 3 | On-Chain Signal Source | `data/live/onchain_signal_source.py` |
| 4 | MEV Protection (Jito) | `trader/jito_submitter.py` |
| 5 | Agent Reputation | `onchain/` |
| 6 | Multi-Agent Pipeline | `agent/`, `strategy/contrib/multi_agent_strategy.py` |
| 7 | Flash Loan Arbitrage | `trader/flash_loan.py` |
| 8 | Atomic Multi-Leg Trader | `trader/atomic_trader.py` |
