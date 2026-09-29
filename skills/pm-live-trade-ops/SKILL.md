---
name: pm-live-trade-ops
description: Run live trading only with explicit user authorization, under strict risk and emergency controls.
---

# PM Live Trade Ops

Use this skill only when the user explicitly asks for live trading and paper validation is complete.

## Gates

- `strategy validate` passes.
- The latest backtest or auto-tune results are acceptable.
- Recent paper runs behaved stably.
- The user has explicitly approved starting live trading.

## Start commands

1. Polymarket

- `oracle3 live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

2. Kalshi

- `oracle3 live run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

## Run control

- `oracle3 trade status --json`
- `oracle3 trade state --json`
- `oracle3 trade pause --json`
- `oracle3 trade resume --json`
- `oracle3 trade killswitch --on --json`
- `oracle3 trade stop --json`

## Emergency order

1. `pause` first.
2. Assess positions and open orders.
3. If needed, `killswitch --on`.
4. Finally, `stop`.

## Hard rules

- Never start live trading without explicit user approval.
- Never skip paper trading and go straight to live.
- Every live run keeps an auditable record: time, parameters, state snapshots and every intervention.
- The MCP server (`oracle3-mcp`) has no live-trading tools; live trading is only available through this CLI.
