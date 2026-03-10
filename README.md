<p align="center">
  <img src="assets/oracle3-banner.svg" alt="Oracle3" width="600">
</p>

<h1 align="center">Oracle3</h1>
<p align="center">
  <strong>Autonomous On-Chain Trading Agent for Prediction Markets</strong>
</p>

<p align="center">
  <a href="https://github.com/YichengYang-Ethan/oracle3/actions"><img src="https://github.com/YichengYang-Ethan/oracle3/actions/workflows/pytest.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/YichengYang-Ethan/oracle3/actions"><img src="https://github.com/YichengYang-Ethan/oracle3/actions/workflows/ruff.yml/badge.svg" alt="Lint"></a>
  <a href="https://github.com/YichengYang-Ethan/oracle3/actions"><img src="https://github.com/YichengYang-Ethan/oracle3/actions/workflows/mypy.yml/badge.svg" alt="Type Check"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/solana-mainnet--beta-9945FF?logo=solana&logoColor=white" alt="Solana">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License">
  <a href="https://github.com/YichengYang-Ethan/oracle3/releases"><img src="https://img.shields.io/github/v/release/YichengYang-Ethan/oracle3?color=orange" alt="Release"></a>
</p>

<p align="center">
  <em>A fully autonomous agent that reads on-chain data, exploits structural mispricings across prediction markets, signs Solana transactions, and manages risk end-to-end — no human in the loop.</em>
</p>

---

<p align="center">
  <img src="assets/dashboard-live.png" alt="Oracle3 Live Dashboard" width="800">
  <br>
  <sub>Live trading dashboard — real-time equity curve, execution pipeline, 8 on-chain agent capabilities</sub>
</p>

## What's New

| Date | Update |
|------|--------|
| **2026-03-09** | v1.0.0 released — 8 arbitrage strategies, market relation graph, SpreadExecutor, engine control server |
| **2026-03-06** | Matching pipeline optimization — resolution filter, volume filter, confidence sizing |
| **2026-03-04** | Live trading dashboard with 8 on-chain feature cards and real-time equity curve |
| **2026-03-01** | Solana integration — native tx signing, Jito MEV protection, on-chain trade logging |
| **2026-02-28** | Initial commit — trading engine, multi-exchange support, AI agent strategies |

## Why On-Chain Agents?

DeFi is shifting from human-operated dashboards to **autonomous agents** that perceive, decide, and execute entirely on-chain. Prediction markets are the ideal proving ground: discrete outcomes, transparent order books, and real-money accountability force an agent to be right — not just convincing.

Oracle3 is built on this thesis. It treats the Solana blockchain as the agent's native runtime: reading on-chain state for signals, simulating transactions before committing capital, writing an immutable audit trail via the Memo program, and composing instructions atomically so complex multi-leg trades either fully succeed or fully revert.

### Why Solana?

| Property | Why it matters for agents |
|----------|--------------------------|
| **Sub-second finality** | Agents can observe → decide → execute within a single block window |
| **Transaction simulation** | `simulateTransaction` lets agents dry-run before committing capital |
| **Atomic composability** | Multiple instructions in one transaction — all-or-nothing execution |
| **On-chain transparency** | Every trade is publicly verifiable; builds agent reputation over time |
| **Low fees** | Enables high-frequency micro-strategies that would be cost-prohibitive on L1 Ethereum |

## Architecture

```
                    ┌───────────────────────────────────────────────┐
                    │              Oracle3 CLI                      │
                    │   paper │ live │ blinks │ monitor │ control   │
                    └───────────────────┬───────────────────────────┘
                                        │
          ┌─────────────────────────────┼─────────────────────────────┐
          │                             │                             │
 ┌────────▼────────┐         ┌──────────▼──────────┐       ┌─────────▼─────────┐
 │  Agent Strategy │         │   Quant Strategy    │       │ Arbitrage Suite   │
 │  (LLM + Tools)  │         │  (Momentum/MR/MM)   │       │ (8 strategies)    │
 └────────┬────────┘         └──────────┬──────────┘       └─────────┬─────────┘
          └─────────────────────────────┼─────────────────────────────┘
                                        │
                    ┌───────────────────▼───────────────────────────┐
                    │             Trading Engine                    │
                    │  Event Loop • Risk Manager • Position Tracker │
                    │  SpreadExecutor • Control Server • Registry   │
                    └───────────────────┬───────────────────────────┘
                                        │
          ┌─────────────────────────────┼─────────────────────────────┐
          │                             │                             │
 ┌────────▼────────┐         ┌──────────▼──────────┐       ┌─────────▼─────────┐
 │  Solana / DFlow │         │    Polymarket       │       │      Kalshi       │
 │  SPL Tokens     │         │    CLOB API         │       │    REST API       │
 └────────┬────────┘         └─────────────────────┘       └───────────────────┘
          │
 ┌────────▼────────┐
 │  Solana Blinks  │  ← Shareable trade URLs
 │  On-Chain Logs  │  ← Memo program audit trail
 │  Jito Bundles   │  ← MEV protection
 └─────────────────┘
```

## Arbitrage Strategy Suite

Oracle3 ships with **8 production-ready arbitrage strategies** that systematically exploit structural mispricings in prediction markets. Each strategy formalizes a specific mathematical invariant that markets must satisfy, then trades the violation when the edge exceeds fees.

### Constraint-Based Arbitrage

These strategies exploit violations of probability axioms — fundamental laws that prediction market prices must obey. When prices drift out of bounds, profit is mathematically guaranteed at settlement.

| Strategy | Invariant | Entry Signal | Legs |
|----------|-----------|--------------|------|
| **Cross-Market Arb** | Same event, same price | Price gap across DFlow/Polymarket/Kalshi exceeds fees | 2 |
| **Exclusivity Arb** | P(A) + P(B) ≤ 1 | Sum of mutually exclusive event prices > 1 | 2 |
| **Implication Arb** | P(A) ≤ P(B) | Implied event priced higher than its parent | 2 |
| **Conditional Arb** | P(A\|B) ∈ [L, U] | Conditional probability bounds violated | 2 |
| **Event Sum Arb** | Σ P(outcome) = 1 | Within-event outcome prices don't sum to 1 | N |
| **Structural Arb** | P(A) = β·P(B) + α | Price deviates from calibrated linear model | 2 |

### Statistical Arbitrage

These strategies use quantitative signals rather than hard constraints — profiting from mean-reversion and temporal patterns.

| Strategy | Method | Edge Source |
|----------|--------|------------|
| **Cointegration Spread** | Self-calibrating z-score bands | Mean-reverting spread between cointegrated markets |
| **Lead-Lag** | Rolling cross-correlation | Follower market lags leader's price movements |

### Strategy Design Principles

Every strategy follows the same battle-tested pattern:

- **Position state machine** — explicit `flat → open → flat` lifecycle prevents phantom positions
- **Fee-aware edge** — conservative 0.5% per-side fee buffer; only trades when `net_edge = gross_edge - 2×fee > threshold`
- **Cooldown windows** — prevents rapid-fire re-entry on the same signal
- **Fill-guarded transitions** — state only changes when orders actually execute (no phantom positions on failed fills)
- **Audit trail** — every decision (trade or hold) recorded via `record_decision()` for post-hoc analysis

## Market Relation Graph

Oracle3 maintains a **persistent knowledge graph** of market relationships at `~/.oracle3/relations.json`. This graph powers the arbitrage strategies by storing discovered, validated, and deployed market pairs.

```
Discovery → Validation → Deployment → Monitoring
   │            │             │            │
   │   ┌────────▼────────┐   │   ┌────────▼────────┐
   │   │ Engle-Granger   │   │   │ Live constraint │
   │   │ Cointegration   │   │   │ checking        │
   │   │ ADF stationarity│   │   │ Drift detection │
   │   │ OLS hedge ratio │   │   │ Auto-invalidate │
   │   │ OU half-life    │   │   └─────────────────┘
   │   │ Pearson correl. │   │
   │   │ Lead-lag detect. │   │
   │   └─────────────────┘   │
   │                         │
   └─── Lifecycle: discovered → validated → deployed → retired
```

**Relation types**: same-event, cross-platform, implication, exclusivity, conditional, structural, cointegration, complement

## Safe Multi-Leg Execution

The **SpreadExecutor** handles multi-leg arbitrage with automatic protection against partial fills:

```
Leg 1: BUY A_YES @ 0.45  →  ✅ Filled
Leg 2: BUY B_NO  @ 0.52  →  ❌ Failed
                               │
                     ┌─────────▼──────────┐
                     │ Auto-Unwind Leg 1  │
                     │ SELL A_YES @ bid   │
                     │ (market price)     │
                     └────────────────────┘
```

No naked positions. No manual intervention. Failed legs are unwound in reverse LIFO order at market prices.

## 8 On-Chain Agent Capabilities

Beyond arbitrage, Oracle3 implements 8 core on-chain agent capabilities for production trading:

| # | Feature | Description |
|---|---------|-------------|
| 1 | **Cross-Market Arbitrage** | Detects same-event price discrepancies across DFlow, Polymarket, and Kalshi |
| 2 | **On-Chain Risk Manager** | Dual-layer: local limits (position, exposure, drawdown) + Solana `simulateTransaction` |
| 3 | **On-Chain Signal Source** | Whale wallet movements, large SPL transfers, DFlow TVL changes |
| 4 | **MEV Protection (Jito)** | Jito Bundle submission with tip; auto-fallback to standard RPC |
| 5 | **Agent Reputation** | 0–100 on-chain score from win rate, Sharpe, consistency via Memo program |
| 6 | **Multi-Agent Pipeline** | SignalAgent → RiskAgent → ExecutionAgent coordination |
| 7 | **Flash Loan Arbitrage** | Atomic borrow → buy → sell → repay in one Solana transaction |
| 8 | **Atomic Multi-Leg** | DFlow + Jupiter + Drift instructions packed into one all-or-nothing tx |

## Engine Control & Portfolio Management

### Runtime Control Server

Oracle3 runs a Unix socket control server alongside each strategy, enabling hot management without process restarts:

```bash
oracle3 engine pause  --id my-strategy    # Pause event ingestion
oracle3 engine resume --id my-strategy    # Resume trading
oracle3 engine stop   --id my-strategy    # Graceful shutdown
oracle3 engine status --id my-strategy    # Runtime stats
oracle3 engine killswitch                 # Emergency stop all + sentinel file
```

### Strategy Registry

The portfolio registry at `~/.oracle3/portfolio.json` tracks every strategy's lifecycle:

```
paper_trading → live_trading → retired
```

```bash
oracle3 engine list                       # All registered strategies
oracle3 engine report --check-health      # Health check (PID, socket, PnL)
oracle3 engine allocate --method kelly    # Capital allocation
```

## AI-Powered Trading

- **LLM Agent Strategies** via OpenAI Agents SDK with 8 built-in tools (place trades, read order books, check positions, fetch news)
- **Adaptive Quant Strategies** — OB imbalance + EMA momentum with self-tuning weights
- **Hybrid approach** — LLM for news analysis, heuristics for order book/price events
- **Multi-provider support** — OpenAI, DeepSeek, or any LiteLLM-compatible model

## Quick Start

### Install

```bash
git clone https://github.com/YichengYang-Ethan/oracle3.git
cd oracle3
poetry install
```

### Browse Markets

```bash
oracle3 market list --exchange solana --limit 10
oracle3 market search --query "bitcoin" --exchange polymarket
```

### Paper Trading (with Live Dashboard)

```bash
oracle3 dashboard --exchange solana \
  --strategy-ref oracle3.strategy.contrib.adaptive_onchain_strategy:AdaptiveOnChainStrategy \
  --initial-capital 10000
```

Open **`http://localhost:3000/live`** for the full live dashboard.

### Run an Arbitrage Strategy

```bash
# Cross-market arbitrage between Polymarket and DFlow
oracle3 dashboard --exchange solana \
  --strategy-ref oracle3.strategy.contrib.cross_market_arbitrage_strategy:CrossMarketArbitrageStrategy

# Cointegration spread trading
oracle3 dashboard --exchange solana \
  --strategy-ref oracle3.strategy.contrib.coint_spread_strategy:CointSpreadStrategy

# Exclusivity constraint arbitrage
oracle3 dashboard --exchange solana \
  --strategy-ref oracle3.strategy.contrib.exclusivity_arb_strategy:ExclusivityArbStrategy
```

### Live Trading (Solana)

```bash
oracle3 live run \
  --exchange solana \
  --strategy-ref oracle3.strategy.contrib.solana_agent_strategy:SolanaAgentStrategy \
  --solana-keypair-path ./keypair.json \
  --use-jito \
  --onchain-signals \
  --monitor
```

### Backtest with DFlow Episodes

```bash
oracle3 dashboard --exchange solana \
  --strategy-ref oracle3.strategy.contrib.cross_market_arbitrage_strategy:CrossMarketArbitrageStrategy \
  --episode-dir data/episodes/dflow_15min
```

## How It Works

```
1. Data Sources fetch live market data + news + on-chain signals
          ↓
2. Market Relation Graph identifies structural mispricings
          ↓
3. Arbitrage / AI / Quant Strategy generates trade signals
          ↓
4. On-Chain Risk Manager validates (local limits + tx simulation)
          ↓
5. SpreadExecutor places multi-leg trades with auto-unwind protection
          ↓
6. Jito Bundle submission for MEV protection (optional)
          ↓
7. On-Chain Logger writes audit trail + updates Reputation score
          ↓
8. Control Server enables live pause/resume/killswitch
```

## Project Structure

```
oracle3/
├── agent/                # Multi-agent coordination pipeline
├── cli/                  # CLI commands (Click)
├── core/                 # Trading engine with event loop + snapshot system
├── engine/               # Engine infrastructure
│   ├── control.py        # Unix socket runtime control (pause/resume/stop)
│   └── registry.py       # Strategy portfolio registry + lifecycle
├── market/               # Market structure & knowledge
│   ├── relations.py      # Persistent market relation graph
│   └── validation.py     # Quantitative validation (cointegration, ADF, hedge ratio)
├── strategy/             # Strategy framework
│   ├── agent_strategy.py # LLM agent with tool calling
│   ├── quant_strategy.py # Quantitative strategy base
│   └── contrib/          # Production strategies
│       ├── cross_market_arbitrage_strategy.py   # Cross-exchange arb
│       ├── exclusivity_arb_strategy.py          # A+B≤1 constraint arb
│       ├── implication_arb_strategy.py          # A≤B constraint arb
│       ├── conditional_arb_strategy.py          # p(A|B) bounds arb
│       ├── event_sum_arb_strategy.py            # Σ(YES)=1 arb
│       ├── structural_arb_strategy.py           # Linear relationship arb
│       ├── coint_spread_strategy.py             # Cointegration spread trading
│       ├── lead_lag_strategy.py                 # Lead-lag temporal trading
│       ├── adaptive_onchain_strategy.py         # Self-tuning OB+EMA
│       ├── solana_agent_strategy.py             # LLM-driven Solana agent
│       └── multi_agent_strategy.py              # Multi-agent pipeline
├── trader/               # Exchange-specific traders
│   ├── solana_trader.py        # Solana/DFlow transaction signing
│   ├── spread_executor.py      # Multi-leg execution + auto-unwind
│   ├── jito_submitter.py       # Jito Bundle MEV protection
│   ├── flash_loan.py           # Flash loan arbitrage
│   ├── atomic_trader.py        # Atomic multi-leg trades
│   ├── polymarket_trader.py    # Polymarket CLOB
│   └── kalshi_trader.py        # Kalshi REST
├── data/                 # Data sources (live + backtest)
│   └── live/
│       ├── dflow_data_source.py         # DFlow REST + WebSocket
│       ├── polymarket_orderflow.py      # Polymarket CLOB
│       ├── kalshi_data_source.py        # Kalshi REST
│       ├── onchain_signal_source.py     # Whale + TVL signals
│       └── google_news_data_source.py   # News feed
├── risk/                 # Dual-layer risk management
├── position/             # Position & P&L tracking
├── onchain/              # On-chain logging + reputation
├── dashboard/            # Web dashboard (FastAPI + WebSocket)
├── blinks/               # Solana Blinks/Actions server
├── analytics/            # Performance analysis (Sharpe, drawdown)
├── backtest/             # Backtesting engine
└── events/               # Event types

tests/                    # 553 tests (pytest + pytest-asyncio)
data/episodes/            # DFlow backtest episodes (parquet)
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ (async/await) |
| Blockchain | Solana (solders + solana-py) |
| Exchanges | DFlow, Polymarket, Kalshi |
| AI | OpenAI Agents SDK + LiteLLM |
| Quantitative | statsmodels, scipy, numpy (optional) |
| Web UI | FastAPI + WebSocket |
| Terminal UI | Textual + Rich |
| CLI | Click |
| Testing | pytest + pytest-asyncio + hypothesis |
| Quality | Ruff + MyPy + Beartype + pre-commit |

## Environment Variables

```bash
# Solana (required for live trading)
export SOLANA_KEYPAIR_PATH="/path/to/keypair.json"

# LLM Provider
export OPENAI_API_KEY="sk-..."           # OpenAI
# or
export DEEPSEEK_API_KEY="..."            # DeepSeek

# Optional: other exchanges
export POLYMARKET_PRIVATE_KEY="..."
export KALSHI_API_KEY_ID="..."
```

## Development

```bash
poetry install --with dev,test
pytest tests/ -v --cov=oracle3
ruff check . && ruff format .
mypy oracle3/
```

## Motivation

On-chain autonomous agents represent the next evolution of crypto infrastructure. Today's DeFi is overwhelmingly human-operated — users manually swap tokens, rebalance portfolios, and chase yield across protocols. The future is **agentic**: software that holds its own keys, perceives market microstructure directly from blockchain state, makes decisions under uncertainty, and executes atomically — all while building a verifiable, on-chain track record.

Oracle3 is my exploration of what that future looks like in practice. Prediction markets are the sharpest testbed because they provide:

- **Binary accountability** — the agent is either right or wrong, no narrative hedging
- **Rich signal diversity** — order books, news, whale flows, cross-market spreads
- **Structural alpha** — probability axioms create mathematically guaranteed arbitrage when prices misbehave
- **Composable execution** — flash loans, atomic multi-leg, MEV protection all compose natively on Solana

The goal is not just a profitable bot, but a reference architecture for how LLM reasoning, quantitative signals, constraint-based arbitrage, and on-chain primitives can be unified into a single autonomous system.

## Why Oracle3?

| Feature | Oracle3 | [Polymarket/agents](https://github.com/Polymarket/agents) | [freqtrade](https://github.com/freqtrade/freqtrade) |
|---------|---------|-------------------|-----------|
| Prediction markets | Solana/DFlow + Polymarket + Kalshi | Polymarket only | Crypto spot/futures only |
| On-chain atomic execution | Solana native | Off-chain | Off-chain |
| Constraint-based arbitrage | 8 strategies with formal invariants | None | None |
| Statistical arbitrage | Cointegration + Lead-lag | None | FreqAI (ML-based) |
| Cross-market arbitrage | 3 exchanges | Single exchange | Single exchange |
| LLM agent with tools | OpenAI Agents SDK + LiteLLM | RAG pipeline | None |
| Multi-leg auto-unwind | SpreadExecutor with LIFO unwind | None | None |
| MEV protection | Jito Bundles | N/A | N/A |
| On-chain audit trail | Solana Memo program | None | None |
| Risk simulation | `simulateTransaction` pre-flight | None | None |
| Live dashboard | Web + Terminal TUI | None | FreqUI |
| Engine hot control | Unix socket (pause/resume/killswitch) | None | Telegram bot |

## Powered By

<p align="center">
  <a href="https://solana.com"><img src="https://img.shields.io/badge/Solana-9945FF?style=for-the-badge&logo=solana&logoColor=white" alt="Solana"></a>
  <a href="https://polymarket.com"><img src="https://img.shields.io/badge/Polymarket-0052FF?style=for-the-badge&logoColor=white" alt="Polymarket"></a>
  <a href="https://kalshi.com"><img src="https://img.shields.io/badge/Kalshi-000000?style=for-the-badge&logoColor=white" alt="Kalshi"></a>
  <a href="https://openai.com"><img src="https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white" alt="OpenAI"></a>
  <a href="https://jito.network"><img src="https://img.shields.io/badge/Jito-00C7B7?style=for-the-badge&logoColor=white" alt="Jito"></a>
</p>

## Star History

<p align="center">
  <a href="https://star-history.com/#YichengYang-Ethan/oracle3&Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=YichengYang-Ethan/oracle3&type=Date&theme=dark" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=YichengYang-Ethan/oracle3&type=Date" />
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=YichengYang-Ethan/oracle3&type=Date" width="600" />
    </picture>
  </a>
</p>

## Community

<p align="center">
  <a href="https://github.com/YichengYang-Ethan/oracle3/discussions"><img src="https://img.shields.io/badge/GitHub_Discussions-181717?style=for-the-badge&logo=github&logoColor=white" alt="Discussions"></a>
  <a href="https://github.com/YichengYang-Ethan/oracle3/issues"><img src="https://img.shields.io/badge/Issues-181717?style=for-the-badge&logo=github&logoColor=white" alt="Issues"></a>
</p>

- **Questions & Ideas** — [GitHub Discussions](https://github.com/YichengYang-Ethan/oracle3/discussions)
- **Bug Reports** — [GitHub Issues](https://github.com/YichengYang-Ethan/oracle3/issues)
- **Security** — see [SECURITY.md](SECURITY.md)
- **Contributing** — see [CONTRIBUTING.md](CONTRIBUTING.md)

## Contributors

<a href="https://github.com/YichengYang-Ethan/oracle3/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=YichengYang-Ethan/oracle3" alt="Contributors" />
</a>

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

<p align="center">
  <sub>This software is for research and educational purposes. Trading on prediction markets involves financial risk.</sub>
</p>
