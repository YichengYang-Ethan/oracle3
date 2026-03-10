# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-03-09

### Added

- **8 constraint-based & statistical arbitrage strategies**: cross-market, exclusivity, implication, conditional, event-sum, structural, cointegration spread, and lead-lag — each with formal invariant, fee-aware edge, cooldown windows, and audit trail
- **Market relation graph**: persistent knowledge graph (`~/.oracle3/relations.json`) with lifecycle management (discovered → validated → deployed → retired) and quantitative validation (Engle-Granger cointegration, ADF stationarity, OLS hedge ratio, OU half-life, Pearson correlation, lead-lag detection)
- **SpreadExecutor**: safe multi-leg execution with automatic LIFO unwind on partial fills — no naked positions
- **Engine control server**: Unix socket runtime control (pause/resume/stop/killswitch) without process restart
- **Strategy portfolio registry**: lifecycle tracking (paper → live → retired), health checks, Kelly capital allocation
- **8 on-chain agent capabilities**: cross-market arbitrage, on-chain risk manager, on-chain signal source, MEV protection (Jito), agent reputation, multi-agent pipeline, flash loan arbitrage, atomic multi-leg trader
- **AI-powered trading** with OpenAI Agents SDK, LiteLLM multi-provider support, and 8 built-in agent tools
- **Solana integration**: native transaction signing, on-chain trade logging via Memo program, Jito bundle submission, Solana Blinks
- **Multi-exchange support**: Solana/DFlow (SPL tokens), Polymarket (CLOB API), Kalshi (REST API)
- **Live trading dashboard** at `/live` with 8 feature cards, equity chart, execution pipeline animation, and pause/resume/e-stop controls
- **Classic terminal dashboard** at `/` for headless environments
- **Risk management**: dual-layer validation (local limits + Solana `simulateTransaction`), max drawdown monitoring, daily loss limits, kill switch
- **Backtesting engine** with DFlow episode replay (parquet format)
- **Coinjure matching pipeline**: cross-platform market relation discovery (implication, exclusivity, complementary) with resolution filter, volume filter, keyphrase pre-filter, confidence sizing, and tag coverage
- **CLI** (`oracle3`) with commands for market browsing, paper/live trading, engine control, reputation, blinks, trade logs
- **CI/CD**: pytest (553 tests), ruff, mypy, codespell, MkDocs documentation site
- **Interactive demo script** (`demo.sh`)

[1.0.0]: https://github.com/YichengYang-Ethan/oracle3/releases/tag/v1.0.0
