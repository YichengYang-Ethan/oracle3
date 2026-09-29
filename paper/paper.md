---
title: 'Oracle3: An open-source autonomous trading agent for prediction markets with a Wang Transform pricing engine'
tags:
  - Python
  - prediction markets
  - quantitative finance
  - Wang Transform
  - Kelly criterion
  - arbitrage
  - Solana
  - Polymarket
  - Kalshi
authors:
  - name: Yicheng Yang
    orcid: 0009-0000-7973-6931
    corresponding: true
    affiliation: 1
affiliations:
  - name: University of Illinois Urbana-Champaign, USA
    index: 1
date: 7 May 2026
bibliography: paper.bib
---

# Summary

Prediction markets price binary contracts at systematically biased levels: a contract whose objective probability is 50% trades on average around 57 cents, an empirical regularity known as the favorite-longshot bias [@thaler:1988]. Despite a long literature documenting this distortion, most open-source trading bots ignore it, treating market prices as unbiased estimates of probability. `Oracle3` is a Python framework that operationalizes a risk-neutral pricing model for binary outcome contracts and uses it to drive automated trading across multiple venues (Kalshi, Polymarket, and Solana-based DFlow). At its core is a Wang Transform [@wang:2000; @wang:2002] calibrated by maximum-likelihood estimation on 291,309 resolved contracts spanning six platforms, with hierarchical covariates for volume, contract duration, and contract moneyness. The library exposes the model as a fair-value engine and pairs it with six constraint-based arbitrage strategies, two statistical-arbitrage strategies, two model-driven strategies, model-Greek-driven sizing, and a risk manager — all wired into an event-driven async trading core with snapshot persistence, killswitch support, and on-chain audit trails.

# Statement of need

Quantitative research on prediction-market pricing has accelerated, but the gap between empirical pricing literature and reproducible, deployable software remains wide. Existing open-source bots (e.g., `polymarket-py`, `kalshi-python`) are thin client wrappers; they do not estimate fair value, do not operationalize known pricing distortions, and offer no shared abstraction across venues. Conversely, academic replication packages typically stop at calibration and rarely produce a runnable trading agent. `Oracle3` closes this gap by shipping the maximum-likelihood estimates from @yang:2026 directly as a real-time pricing engine, exposing the same hierarchical covariate model used in the paper, and connecting it to live order-book data, execution, and risk management.

The intended audience is twofold. For researchers studying prediction-market efficiency, the favorite-longshot bias [@thaler:1988; @snowberg:2010], and risk-neutral pricing [@harrison:1979; @wang:2000], `Oracle3` provides a reproducible, well-tested implementation of a multi-venue pricing-and-trading pipeline that can be used as a baseline for new strategies or as a calibration testbed. For practitioners, it provides a transparent, auditable agent whose every signal is grounded in an explicit pricing model rather than opaque heuristics.

# State of the field

Several Python tools touch the prediction-market space, but each is partial. `py-clob-client` and `kalshi-python` are vendor SDKs without modeling. The `crypto-trading-bot` family targets centralized exchanges and currency markets, not binary outcome markets. Research code accompanying empirical-finance papers is typically Jupyter-driven, focused on calibration rather than deployment. Outside Python, decentralized-finance projects on Solana such as `dflow-program` provide on-chain primitives but no off-chain modeling layer. `Oracle3` is, to our knowledge, the first open-source library that combines (i) calibrated probabilistic pricing for binary contracts, (ii) constraint-based arbitrage detection across heterogeneous venues, and (iii) production-grade execution with on-chain settlement.

# Software design

`Oracle3` is organized as four layers:

1. **Pricing engine** (`oracle3/pricing/`). Implements the Wang Transform $p^{\mathrm{mkt}} = \Phi(\Phi^{-1}(p^*) + \lambda)$ with $\hat\lambda = 0.183$ at the global level and a hierarchical covariate model $\lambda_i = 0.259 - 0.072 \ln(1+V) + 0.143 \ln(1+D) - 0.477 |p - 0.5|$ where $V$ is daily volume, $D$ is total contract duration (hours), and $|p-0.5|$ is moneyness. An online recalibrator combines batch MLE with a streaming EWMA estimator and category shrinkage, so the model adapts to regime change without forgetting prior data.
2. **Strategy layer** (`oracle3/strategy/`). Six constraint-based strategies each enforce a probability-axiom invariant (cross-market identity, exclusivity bound $P(A)+P(B)\le 1$, implication monotonicity, conditional bounds, event-sum unity, structural relationships from the calibrated model) and emit signals when violations exceed thresholds. Two model-driven strategies trade fair-value divergence and a "premium decay" lifecycle effect. Two statistical-arbitrage strategies (cointegration spread, lead-lag) provide complementary signals.
3. **Trading core** (`oracle3/core/`, `oracle3/trader/`). An async event loop dispatches strategy signals to a `SpreadExecutor` component that places multi-leg orders sequentially and unwinds via LIFO on partial fills to avoid naked legs. A dual-layer risk manager enforces position, drawdown, and exposure limits locally, and additionally calls Solana `simulateTransaction` for pre-flight on-chain checks.
4. **Infrastructure** (`oracle3/cli/`, `oracle3/data/`). A Click-based CLI exposes market discovery, live dashboards, backtests, and paper-run modes. Snapshot persistence and a Unix-socket control plane enable pause/resume/killswitch operations on long-running agents. Solana submissions go through Jito bundles, with a Memo-program audit trail for every fill.

The codebase is type-checked with `mypy`, linted with `ruff`, and validated by 633 unit tests and integration tests on every push, enforced by GitHub Actions. The design has been used to run paper-trading sessions on Polymarket and Kalshi, and the pricing engine is shared with the replication package of @yang:2026.

# Acknowledgements

The author thanks the maintainers of the Polymarket, Kalshi, and DFlow APIs for documentation; the prediction-market research community whose empirical work motivates this software; and contributors who reported issues and suggested strategies. Any errors are the author's own.

# References
