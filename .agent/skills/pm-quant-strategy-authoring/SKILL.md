---
name: pm-quant-strategy-authoring
description: Turn an agent's quantitative idea into Strategy code with tunable parameters and verifiable behavior.
---

# PM Quant Strategy Authoring

Use this skill when the user asks the agent to come up with a strategy and write the code.

## Goals

- A runnable strategy class (a `QuantStrategy` subclass).
- Exposed numeric parameters, so the strategy can be auto-tuned.
- `strategy validate` and `backtest run` both succeed.

## Code entry points

- Base class contract: `oracle3/strategy/quant_strategy.py` (`QuantStrategy`).
- For LLM-driven strategies use `AgentStrategy` instead (see `pm-agent-strategy-authoring`).
- Example strategies: `examples/strategies/*.py`, `strategies/*.py`.
- New strategies go in `strategies/`.

## Steps

1. Create the skeleton

- `oracle3 strategy create --output strategies/<name>.py --class-name <ClassName> --type quant`

2. Implement the strategy

- In `process_event`, handle only the event types you need (usually `PriceChangeEvent`).
- Put parameters in the constructor; numeric parameters are what tuning searches over.
- Call `trader.place_order(...)` to act.
- Record actions and signals with `self.record_decision(...)`.

3. Quick validation

- `oracle3 strategy validate --strategy-ref strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --dry-run --events 10 --json`

4. Single-market backtest

- `oracle3 backtest run --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --json`

5. Prepare for tuning

- Every parameter has a clear meaning, bounds and a default.
- Parameter types stay JSON-serializable.
- Tune with `oracle3 research auto-tune --param-grid-json '<json>' ...`.

## Hard rules

- Do not hard-code strategy logic in a command-line script; it belongs in its own strategy file.
- Never use future information (no look-ahead).
- Keep runs reproducible: record the strategy file path, class name, kwargs and command.
