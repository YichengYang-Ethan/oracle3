# Oracle3

[![Tests](https://github.com/YichengYang-Ethan/oracle3/actions/workflows/pytest.yml/badge.svg)](https://github.com/YichengYang-Ethan/oracle3/actions)
[![Lint](https://github.com/YichengYang-Ethan/oracle3/actions/workflows/ruff.yml/badge.svg)](https://github.com/YichengYang-Ethan/oracle3/actions)
[![Type Check](https://github.com/YichengYang-Ethan/oracle3/actions/workflows/mypy.yml/badge.svg)](https://github.com/YichengYang-Ethan/oracle3/actions)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![Solana](https://img.shields.io/badge/solana-mainnet--beta-9945FF?logo=solana&logoColor=white)
![License](https://img.shields.io/badge/license-Apache%202.0-green)

**Oracle3** is an autonomous on-chain trading agent for prediction markets on Solana, Polymarket, and Kalshi. It combines LLM reasoning, quantitative signals, and atomic on-chain execution into a single autonomous system — no human in the loop.

## Highlights

- **8 on-chain agent capabilities** — arbitrage, risk management, MEV protection, reputation, flash loans, and more
- **AI + Quant hybrid** — LLM agent strategies via OpenAI Agents SDK alongside adaptive quantitative strategies
- **Multi-exchange** — Solana/DFlow, Polymarket (CLOB API), Kalshi (REST API)
- **Cross-platform arbitrage** — detect and trade price discrepancies across exchanges
- **Live trading dashboard** — real-time web UI with equity curve, feature cards, and execution pipeline
- **Dual-layer risk** — local limits + Solana `simulateTransaction` pre-flight validation
- **On-chain audit trail** — every trade logged to Solana via Memo program

## Quick Start

```bash
git clone https://github.com/YichengYang-Ethan/oracle3.git
cd oracle3
poetry install
```

```bash
# Browse markets
oracle3 market list --exchange solana --limit 10

# Paper trading with live dashboard
oracle3 dashboard --exchange solana \
  --strategy-ref oracle3.strategy.contrib.adaptive_onchain_strategy:AdaptiveOnChainStrategy \
  --initial-capital 10000
```

Open `http://localhost:3000/live` for the live dashboard.

## Next Steps

- [Quick Start Guide](CLI_QUICK_START.md) — installation and first commands
- [CLI Monitoring](CLI_MONITORING.md) — monitor your trading sessions
- [Architecture](PROJECT_SPECIFICATION.md) — system design and module reference
