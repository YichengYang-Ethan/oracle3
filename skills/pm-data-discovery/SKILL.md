---
name: pm-data-discovery
description: Find out what prediction-market data is available, locate the target markets, and save research samples to files. Data discovery and collection only; no strategy hypotheses.
---

# PM Data Discovery

Use this skill when the user wants to look at data, find markets, or collect research samples before any strategy work.

## Goals

Answer three questions:

- Which data sources are available?
- How do I find the target markets?
- How do I save the data as files that later strategy research can use?

## Data entry points

1. Market metadata and search (online)

- `oracle3 market list --exchange polymarket --limit 50 --json`
- `oracle3 market search --exchange polymarket --query "<keyword>" --limit 50 --json`
- `oracle3 market info --exchange polymarket --market-id <market_id> --json`
- `oracle3 market history --market-id <market_id> --interval 1h --limit 500 --json`

2. News and external events (online)

- `oracle3 news fetch --source google --query "<keyword>" --limit 30 --json`
- `oracle3 news fetch --source rss --query "<keyword>" --limit 30 --json`

3. Local history files (offline)

- `oracle3 research markets --history-file <history.jsonl> --sort-by points --limit 50 --json`
- `oracle3 research slice --history-file <history.jsonl> --market-id <M> --event-id <E> --output <slice.jsonl> --json`

4. Raw stream recording (online)

- `oracle3 data record --exchange polymarket --output <events.jsonl> --duration 3600 --json`

5. Through the MCP server (`oracle3-mcp`)

- `search_markets`, `get_market`, `get_orderbook`, `get_quote` for Kalshi and Polymarket.

## Recommended flow

1. Use `market search` or `market list` to define the candidate markets.
2. Pull `market info` and `market history` for the candidates to check liquidity and price behavior.
3. For offline research, record with `data record` or use an existing history file.
4. Produce slices of the key markets for the strategy agent to read.

## Output requirements

- A clear data inventory: markets, time range, file paths.
- Every conclusion comes with a command that reproduces it.
- If the network is unavailable, switch to the local `research` commands instead of stopping.

## Hard rules

- Do not propose strategy logic in this skill.
- Only discover, filter, sample and save data.
- Prefer `--json` output so that later agents can consume it.
