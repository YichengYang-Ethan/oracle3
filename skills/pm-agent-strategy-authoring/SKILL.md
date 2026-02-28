---
name: pm-agent-strategy-authoring
description: 用于把 LLM/工具驱动的想法实现为 AgentStrategy 代码，并通过 paper trading 评估效果。
---

# PM Agent Strategy Authoring

当用户要求"写一个用 LLM / MCP 工具 / 外部 API 做决策的策略"时，使用这个技能。

## 目标

- 产出可运行的策略类（`AgentStrategy` 子类）
- 保证 `strategy validate` 与 `paper run` 可执行
- **不**做参数网格搜索（agent 策略不可 auto-tune）

## 代码入口

- 基类契约：`oracle3/strategy/agent_strategy.py` (`AgentStrategy`)
- LLM 策略参考：`oracle3/strategy/simple_strategy.py`
- 新策略目录：`strategies/`

## 实施流程

1. 创建骨架

- `oracle3 strategy create --output strategies/<name>.py --class-name <ClassName> --type agent`

2. 实现策略

- 在 `process_event` 中调用 LLM / MCP 工具 / 外部 API
- 非确定性 OK：每次调用结果可以不同
- 用 `self.record_decision(reasoning=...)` 把 LLM 输出写入 reasoning 字段，便于监控
- 决策时调用 `trader.place_order(...)`
- 用 `self.is_paused()` 守卫控制平面暂停信号

3. 快速验证

- `oracle3 strategy validate --strategy-ref strategies/<name>.py:<ClassName> --dry-run --events 10 --json`

4. Paper trading 评估（agent 策略的正确评估方式）

- `oracle3 paper run --strategy-ref strategies/<name>.py:<ClassName> --monitor`
- 通过 `oracle3 trade status --json` 观察 decisions / positions
- 用 `oracle3 trade get-state --json` 获取完整快照

5. 干预与调整

- `oracle3 trade pause` / `oracle3 trade resume`
- `oracle3 trade swap-strategy --strategy-ref strategies/<new>.py:<NewClass>` 热换策略

## Hard Rules

- `AgentStrategy` 子类**不可**用于 `research discover-alpha --param-grid-json`（会报错）。
- 外部 API 调用必须做好超时与错误处理，避免阻塞事件循环。
- 必须通过 `self.is_paused()` 检查再决策，尊重控制平面暂停信号。
- 不使用未来信息（禁止 look-ahead）。
- 必须可复现路径：策略文件路径、类名、命令都要明确记录。
