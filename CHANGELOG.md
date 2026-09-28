# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Documentation accuracy pass** — corrected claims to match the code exactly:
  - "peer-reviewed pricing model" → the Wang Transform (Wang 2000) calibrated in the author's working paper (Yang 2026, SSRN). The calibration is an unrefereed working paper; only the transform itself is from the peer-reviewed Wang (2000).
  - Strategy count restated as **6 constraint-based + 2 statistical-arbitrage + 2 model-driven** (10 total), replacing the ambiguous "8 constraint-based arbitrage strategies" phrasing in the README, paper, `CITATION.cff`, and `.zenodo.json`.
  - Removed an inaccurate "vectorized NumPy/PyTorch module" description of the pricing engine from the preprint — the deployed path is dependency-light pure Python; NumPy/SciPy are used only by the optional batch MLE estimator.
  - Softened `SpreadExecutor` ("being wired into the multi-leg strategies") and MEV-protection ("best-effort, with public-RPC fallback") descriptions to match their implemented status.
  - Corrected the hierarchical `D` covariate description from "days remaining to expiry" to total contract duration (hours), matching the estimated model.
- Added a **Status & Limitations** section to the README (paper-traded status, fee economics, roadmap to live).

### Fixed

- **Removed fabricated profit accounting** from the flash-loan prototype, which previously booked a placeholder `amount * 0.005` profit on a Memo-only transaction.

### Moved

- Relocated the flash-loan arbitrage prototype to `oracle3/experimental/` and labeled the multi-agent pipeline and on-chain reputation modules as non-production prototypes.

## [1.1.0] - 2026-03-28

### Added

- **Wang Transform MLE** (`oracle3.pricing.wang_mle`): full maximum likelihood estimator implementing the core model from Yang (2026), "Pricing Prediction Markets: Risk Premiums, Incomplete Markets, and a Decomposition Framework"
  - Pooled and hierarchical estimation with analytic gradients
  - Three SE estimators: Fisher, sandwich robust, Liang-Zeger clustered sandwich
  - Numerical Hessian with eigenvalue regularization and BLAS chunk-size safety
  - Model comparison (LR tests, AIC/BIC, pseudo-R²)
  - Empirical priors: Polymarket λ=0.166, Kalshi=0.187, Metaculus=0.287, Manifold=-0.218

- **Probabilistic fair value engine** (`oracle3.pricing`): full pricing module grounded in Yang (2026)
  - Three distortion families: probit (Wang 2000), dual power (Denneberg 1994), proportional hazard
  - **Exact empirical coefficients**: λ_i = 0.259 - 0.072·ln(1+V) + 0.143·ln(1+D) - 0.477·|p-0.5|
  - **Time-varying model**: γ₁=-0.156·τ + γ₂=0.074·τ², half-life 33-77% of contract lifetime
  - **Volume-stratified alpha targeting**: >$10K volume → λ≈0 (premium competed away); $500-$10K = sweet spot
  - Hybrid calibrator: batch MLE (historical data) + streaming EWMA (live trading) with hierarchical shrinkage
  - Premium lifecycle tracker with polynomial decay fitting and optimal entry timing
  - Contract microstructure scorer (volume, spread, duration, extremity, book depth)

- **Model sensitivities (Greeks)** (`oracle3.pricing.greeks`): analytic derivatives of the Wang model
  - dp/dλ, dp/dp*, premium ratio, Kelly fraction, edge decay rate
  - Favorite-longshot bias proven as theorem: overpricing ratio monotonically decreasing in p*
  - Batch computation for portfolio-level risk decomposition

- **Model-informed Kelly sizing** (`oracle3.trading.sizing`): position sizing with Wang-derived edge
  - Kelly criterion with model edge, confidence scaling, and volume-tier gating
  - Automatic skip of very-high-volume markets where premium is already competed away
  - Inspired by three-tier sizing approaches in agent-native prediction market systems

- **Edge-weighted capital allocator** (`oracle3.trading.allocator`): multi-strategy budget allocation
  - Risk-adjusted scoring (PnL / |drawdown|) with 30-day exponential time decay
  - Premium-alpha strategy bonus, reserve capital, per-strategy caps
  - Graceful degradation: performance-weighted → equal → minimum budgets

- **Correlation-aware risk manager** (`oracle3.risk.correlation_risk_manager`): correlated exposure limits
  - EWMA rolling correlation estimation on price returns
  - Effective exposure via correlation matrix quadratic form
  - Concentration ratio gating, stale correlation decay

- **Fair value divergence strategy v2** (`FairValueStrategy`): model-driven alpha with exact coefficients
  - Uses Yang (2026) hierarchical model for per-contract λ estimation
  - Kelly-optimal sizing from model Greeks
  - Volume-tier targeting: focuses on medium-liquidity alpha sweet spot

- **Premium decay strategy** (`PremiumDecayStrategy`): timing-based premium lifecycle alpha

- **Distortion-based validation tools**: `estimate_risk_premium`, `cross_platform_premium_test`, `favorite_longshot_test` in `oracle3.market.validation`

- **79 new tests** (total: 633) covering MLE recovery, Greeks, sizing, allocation, strategies

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
