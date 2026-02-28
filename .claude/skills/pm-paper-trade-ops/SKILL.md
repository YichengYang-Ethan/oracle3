---
name: pm-paper-trade-ops
description: 用于在策略通过回测后执行 paper trading、监控、干预和归档。
---

# PM Paper Trade Ops

当用户要求跑 paper trading 或验证线上行为时，使用这个技能。

## 前置条件

- 策略已能通过：
- `oracle3 strategy validate ... --json`
- 至少一次可解释 backtest/auto-tune 结果

## 启动流程

1. 启动 paper

- `oracle3 paper run --exchange <polymarket|kalshi|rss> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --duration <seconds> --json`
- 需要可视化时加 `--monitor`

2. 运行中控制

- `oracle3 trade status --json`
- `oracle3 trade state --json`
- `oracle3 trade pause --json`
- `oracle3 trade resume --json`
- `oracle3 trade swap --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`
- `oracle3 trade stop --json`

3. 结果归档

- 保存关键输出到 `data/research/<run_id>/paper/`
- 至少包含：配置、状态快照、结束摘要

## Hard Rules

- 发现异常先 `pause`，确认后再 `resume` 或 `stop`。
- paper 阶段不使用 live 凭证。
- 每次运行都要能回放配置（策略 ref + kwargs + duration + exchange）。
