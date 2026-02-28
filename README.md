# Oracle3

**AI-Native Prediction Market Trading Agent on Solana, Polymarket & Kalshi**

Built for HackIllinois 2026

---

## What is Oracle3?

Oracle3 is an autonomous AI trading agent that operates across multiple prediction market exchanges. It uses LLM-driven strategies to analyze markets, place trades, and manage risk — all from a single CLI.

### Supported Exchanges

| Exchange | Type | Settlement |
|----------|------|------------|
| **Solana/DFlow** | On-chain | Solana mainnet-beta |
| **Polymarket** | Off-chain | Polygon USDC |
| **Kalshi** | Regulated | USD |

### Key Features

- **Multi-Exchange**: Trade prediction markets across Solana, Polymarket, and Kalshi from one interface
- **AI Agent Strategies**: LLM-powered trading via OpenAI Agents SDK with tool-using capabilities
- **Solana Blinks**: Share trades as Solana Actions — anyone can execute directly from a URL
- **On-Chain Logging**: Every trade logged to Solana via Memo program for full transparency
- **Paper Trading**: Test strategies with simulated execution before going live
- **TUI Dashboard**: Real-time terminal UI with live P&L, positions, and order flow

## Architecture

```
┌─────────────────────────────────────────────┐
│               Oracle3 CLI                    │
├──────────┬──────────┬──────────┬────────────┤
│  paper   │   live   │  market  │   blinks   │
├──────────┴──────────┴──────────┴────────────┤
│              Strategy Layer                   │
│  AgentStrategy │ QuantStrategy │ Contrib/*   │
├─────────────────────────────────────────────┤
│            Trading Engine                    │
├──────────┬──────────┬───────────────────────┤
│  Solana  │ Polymarket│    Kalshi            │
│  Trader  │  Trader   │    Trader            │
├──────────┼──────────┼───────────────────────┤
│  DFlow   │  CLOB    │    Kalshi             │
│  API     │  API     │    API                │
└──────────┴──────────┴───────────────────────┘
```

## Quick Start

### Install

```bash
cd oracle3
poetry install
```

### Browse Markets

```bash
# List Solana/DFlow markets
oracle3 market list --exchange solana --limit 10

# Search across Polymarket
oracle3 market search --query "bitcoin" --exchange polymarket

# Kalshi market info
oracle3 market info --market-id TICKER --exchange kalshi
```

### Paper Trading

```bash
# Paper trade on Solana with AI agent strategy
oracle3 paper run \
  --exchange solana \
  --strategy-ref oracle3.strategy.contrib.solana_agent_strategy:SolanaAgentStrategy \
  --initial-capital 1000 \
  --monitor \
  --duration 300

# Paper trade on Polymarket
oracle3 paper run --exchange polymarket --monitor
```

### Live Trading (Solana)

```bash
# Requires a Solana keypair
oracle3 live run \
  --exchange solana \
  --strategy-ref oracle3.strategy.contrib.solana_agent_strategy:SolanAgentStrategy \
  --solana-keypair-path ./keypair.json \
  --monitor
```

### Solana Blinks Server

```bash
# Start the Actions server
oracle3 blinks --port 8080

# Test it
curl http://localhost:8080/api/trade/SOME_MARKET
```

### On-Chain Trade Log

```bash
oracle3 trade-log --limit 20 --json
```

## How DFlow Integration Works

[DFlow](https://dflow.net) tokenizes Kalshi prediction markets on Solana mainnet-beta:

1. **Oracle3** fetches market data from DFlow's prediction markets API
2. **AI Agent** analyzes the market using LLM + market data tools
3. **DFlow Trade API** returns a ready-to-sign Solana transaction
4. **Oracle3** signs with the user's keypair and submits to Solana RPC
5. **Memo Program** logs the trade metadata on-chain for transparency

No API key needed for the dev tier.

## Project Structure

```
oracle3/
├── cli/                  # CLI commands (Click)
├── data/live/            # Data sources (Polymarket, Kalshi, DFlow)
├── trader/               # Traders (Paper, Polymarket, Kalshi, Solana)
├── strategy/             # Strategy base classes + contrib strategies
├── blinks/               # Solana Blinks/Actions server
├── onchain/              # On-chain trade logging
├── core/                 # Trading engine
├── risk/                 # Risk management
├── position/             # Position tracking
└── events/               # Event system
```

## Tech Stack

- **Python 3.10+** with async/await throughout
- **Solana**: solders + solana-py for transaction signing
- **DFlow**: REST API for market data + trade execution
- **OpenAI Agents SDK**: LLM-driven strategy execution
- **FastAPI**: Solana Blinks server
- **Textual**: Terminal UI dashboard
- **Click**: CLI framework

## Environment Variables

```bash
# Solana
export SOLANA_KEYPAIR_PATH="/path/to/keypair.json"
# or
export SOLANA_PRIVATE_KEY="base58_encoded_key"

# Polymarket
export POLYMARKET_PRIVATE_KEY="your_private_key"

# Kalshi
export KALSHI_API_KEY_ID="your_kalshi_key_id"
export KALSHI_PRIVATE_KEY_PATH="/path/key.pem"
```

## Development

```bash
poetry install --with dev,test
pytest tests/ -v
ruff check . && ruff format .
```

## License

Apache 2.0

## Disclaimer

This software is for educational and research use. Live trading carries financial risk.
