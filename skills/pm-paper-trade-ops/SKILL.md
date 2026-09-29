---
name: pm-paper-trade-ops
description: Run, monitor, intervene in and archive paper trading after a strategy has passed backtesting.
---

# PM Paper Trade Ops

Use this skill when the user asks to run paper trading or to check live behavior without real money.

## Preconditions

- The strategy passes `oracle3 strategy validate ... --json`.
- There is at least one interpretable backtest or auto-tune result.

## Steps

1. Start paper trading

- `oracle3 paper run --exchange <polymarket|kalshi|solana|rss> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --duration <seconds> --json`
- Add `--monitor` for the live terminal view.

2. Control the running engine

- `oracle3 trade status --json`
- `oracle3 trade state --json`
- `oracle3 trade pause --json`
- `oracle3 trade resume --json`
- `oracle3 trade swap --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`
- `oracle3 trade stop --json`

3. Archive the results

- Save the key outputs to `data/research/<run_id>/paper/`.
- Include at least the configuration, a state snapshot and the end-of-run summary.

## Hard rules

- On any anomaly, `pause` first; `resume` or `stop` only after checking.
- Never use live credentials during paper trading.
- Every run must be replayable from its configuration (strategy ref, kwargs, duration, exchange).
