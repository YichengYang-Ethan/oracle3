---
name: pm-agent-strategy-authoring
description: Turn an LLM- or tool-driven idea into AgentStrategy code and evaluate it with paper trading.
---

# PM Agent Strategy Authoring

Use this skill when the user asks for a strategy that makes decisions with an LLM, MCP tools or external APIs.

## Goals

- A runnable strategy class (an `AgentStrategy` subclass).
- `strategy validate` and `paper run` both succeed.
- No parameter grid search: agent strategies cannot be auto-tuned.

## Code entry points

- Base class contract: `oracle3/strategy/agent_strategy.py` (`AgentStrategy`).
- LLM strategy reference: `oracle3/strategy/simple_strategy.py`.
- New strategies go in `strategies/`.

## Steps

1. Create the skeleton

- `oracle3 strategy create --output strategies/<name>.py --class-name <ClassName> --type agent`

2. Implement the strategy

- Call the LLM, MCP tools or external APIs inside `process_event`.
- Non-determinism is fine: results may differ between calls.
- Write the LLM output into the reasoning field with `self.record_decision(reasoning=...)` so it can be monitored.
- Call `trader.place_order(...)` to act.
- Guard every decision with `self.is_paused()` to respect the control plane.

3. Quick validation

- `oracle3 strategy validate --strategy-ref strategies/<name>.py:<ClassName> --dry-run --events 10 --json`

4. Evaluate with paper trading (the right way to evaluate agent strategies)

- `oracle3 paper run --strategy-ref strategies/<name>.py:<ClassName> --monitor`
- Watch decisions and positions with `oracle3 trade status --json`.
- Take a full snapshot with `oracle3 trade state --json`.

5. Intervene and adjust

- `oracle3 trade pause` / `oracle3 trade resume`
- Hot-swap the strategy with `oracle3 trade swap --strategy-ref strategies/<new>.py:<NewClass>`.

## Hard rules

- `AgentStrategy` subclasses cannot be used with `research auto-tune --param-grid-json`; it raises an error.
- External API calls need timeouts and error handling so they never block the event loop.
- Check `self.is_paused()` before every decision.
- Never use future information (no look-ahead).
- Keep runs reproducible: record the strategy file path, class name and command.
