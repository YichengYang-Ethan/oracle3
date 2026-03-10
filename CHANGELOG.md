# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-03-09

### Added

- **8 on-chain agent capabilities**: cross-market arbitrage, on-chain risk manager, on-chain signal source, MEV protection (Jito), agent reputation, multi-agent pipeline, flash loan arbitrage, atomic multi-leg trader
- **AI-powered trading** with OpenAI Agents SDK, LiteLLM multi-provider support, and 8 built-in agent tools
- **Solana integration**: native transaction signing, on-chain trade logging via Memo program, Jito bundle submission, Solana Blinks
- **Multi-exchange support**: Solana/DFlow (SPL tokens), Polymarket (CLOB API), Kalshi (REST API)
- **Cross-platform arbitrage**: detect and trade price discrepancies across exchanges
- **Live trading dashboard** at `/live` with 8 feature cards, equity chart, execution pipeline animation, and pause/resume/e-stop controls
- **Classic terminal dashboard** at `/` for headless environments
- **Quantitative strategies**: adaptive OB imbalance + EMA momentum with self-tuning weights
- **Contributed strategies**: cross-market arbitrage, multi-agent pipeline, Solana agent, debate strategy
- **Risk management**: dual-layer validation (local limits + RPC simulation), max drawdown monitoring, daily loss limits, kill switch
- **Backtesting engine** with DFlow episode replay (parquet format)
- **Coinjure matching pipeline**: cross-platform market relation discovery (implication, exclusivity, complementary)
- **CLI** (`oracle3`) with commands for market browsing, paper/live trading, reputation, blinks, trade logs
- **CI/CD**: pytest, ruff, mypy, codespell, MkDocs documentation site
- **Interactive demo script** (`demo.sh`)

[1.0.0]: https://github.com/YichengYang-Ethan/oracle3/releases/tag/v1.0.0
