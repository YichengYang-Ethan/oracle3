---
name: pm-quant-strategy-authoring
description: 用于把 agent 的量化想法实现为 Strategy 代码，并形成可调参数接口与可验证行为。
---

# PM Quant Strategy Authoring

当用户要求“让 agent 自己想策略并写代码”时，使用这个技能。

## 目标

- 产出可运行的策略类（`Strategy` 子类）
- 暴露参数（用于 `auto-tune`）
- 保证 `strategy validate` 与 `backtest` 可执行

## 代码入口

- 基类契约：`oracle3/strategy/quant_strategy.py` (`QuantStrategy`)
- 只适用于 `QuantStrategy` 子类，LLM 策略请使用 `AgentStrategy`（见 `pm-agent-strategy-authoring`）
- 示例策略：`examples/strategies/*.py`, `strategies/*.py`
- 新策略目录：`strategies/`

## 实施流程

1. 创建骨架

- `oracle3 strategy create --output strategies/<name>.py --class-name <ClassName> --type quant`

2. 实现策略

- 在 `process_event` 中只处理你需要的事件类型（常见是 `PriceChangeEvent`）
- 参数放在构造函数（数值参数用于调优）
- 决策时调用 `trader.place_order(...)`
- 用 `self.record_decision(...)` 记录动作和信号

3. 快速验证

- `oracle3 strategy validate --strategy-ref strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --dry-run --events 10 --json`

4. 单点回测验证

- `oracle3 backtest run --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --json`

5. 参数调优准备

- 参数要有明确意义、边界、默认值
- 参数类型保持可 JSON 序列化

## Hard Rules

- 不把策略逻辑写死在命令行脚本里，必须落到独立策略文件。
- 不使用未来信息（禁止 look-ahead）。
- 必须可复现：策略文件路径、类名、kwargs、命令都要明确。
