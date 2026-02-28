---
name: pm-data-discovery
description: 用于先搞清楚“有哪些数据可看、怎么搜索目标市场、怎么落地数据样本”的技能。只做数据发现与取数，不做策略假设。
---

# PM Data Discovery

当用户要先看数据、找市场、收集研究样本时，使用这个技能。

## 目标

- 回答 3 个问题：
- 有哪些可用数据源
- 如何搜索到目标市场
- 如何把数据保存成后续策略研究可用的文件

## 数据入口

1. 市场元数据与搜索（在线）

- `oracle3 market list --exchange polymarket --limit 50 --json`
- `oracle3 market search --exchange polymarket --query "<关键词>" --limit 50 --json`
- `oracle3 market info --exchange polymarket --market-id <market_id> --json`
- `oracle3 market history --market-id <market_id> --interval 1h --limit 500 --json`

2. 新闻与外部事件（在线）

- `oracle3 news fetch --source google --query "<关键词>" --limit 30 --json`
- `oracle3 news fetch --source rss --query "<关键词>" --limit 30 --json`

3. 本地历史文件研究（离线）

- `oracle3 research markets --history-file <history.jsonl> --sort-by points --limit 50 --json`
- `oracle3 research slice --history-file <history.jsonl> --market-id <M> --event-id <E> --output <slice.jsonl> --json`
- `oracle3 research features --history-file <history.jsonl> --market-id <M> --event-id <E> --output <features.jsonl> --json`
- `oracle3 research labels --history-file <history.jsonl> --market-id <M> --event-id <E> --horizon-steps 5 --threshold 0.01 --output <labels.jsonl> --json`

4. 原始流数据录制（在线）

- `oracle3 data record --exchange polymarket --output <events.jsonl> --duration 3600 --json`

## 推荐流程

1. 先 `market search/list` 定义候选市场。
2. 对候选市场拉 `market info/history` 看流动性和价格行为。
3. 需要离线研究时，用 `data record` 或已有 `history_file`。
4. 对重点市场生成 `slice/features/labels` 供策略 agent 阅读。

## 输出要求

- 给出明确的数据清单：市场、时间范围、文件路径。
- 每个结论都附可复现命令。
- 如果网络不可用，明确切换到本地 `research` 命令，不阻塞流程。

## Hard Rules

- 不在此技能里提出策略逻辑假设。
- 只做数据发现、过滤、取样、落盘。
- 命令优先 `--json` 输出，便于后续 agent 消费。
