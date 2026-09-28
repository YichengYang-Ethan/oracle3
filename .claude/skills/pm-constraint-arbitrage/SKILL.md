---
name: pm-constraint-arbitrage
description: Check whether prices of related prediction-market contracts violate a probability bound after venue fees, using the oracle3 MCP server or Python API. Paper trading only.
---

# PM Constraint Arbitrage

Use this skill when the user asks whether related markets are mispriced relative to each other, for example "does P(A) exceed P(B) even though A implies B?", or whether a cross-venue price gap survives fees.

## Tools

MCP server (`pip install oracle3`, then run `oracle3-mcp`):

- `search_markets`, `get_quote`, `get_orderbook` to find markets and read executable prices.
- `check_constraint_live` to fetch quotes and fee schedules and evaluate a relation.
- `check_constraint` to evaluate a relation on quotes you supply.
- `trading_fee` to price a single fill under a venue's schedule.
- `paper_order`, `paper_portfolio` to paper-trade the legs.

Python API: `oracle3.arbitrage.check_constraint` and `oracle3.fees`.

## Relations

| Relation | Bound | Quotes, in order |
|---|---|---|
| implication | P(A) <= P(B) when A implies B | A, B |
| exclusivity | probabilities sum to at most 1 | every outcome |
| complement | P(A) + P(B) = 1 | A, B |
| same_event | P(A) = P(B), one event on two venues | A, B |
| event_sum | probabilities sum to exactly 1 | every outcome |

## Steps

1. Find the markets with `search_markets` and read both markets' resolution rules with `get_market`.
2. Decide which relation holds. This is a judgment about the contract wording, not something the tool can verify.
3. Run `check_constraint_live` with the relation and the markets in the order above.
4. Read `best.gross_edge_per_contract` (edge before fees), `best.fees_per_contract` and `best.net_edge_per_contract`.
5. Only if the net edge is positive and the book is deep enough, paper-trade each leg with `paper_order` at a limit no worse than the quoted ask.

## Hard rules

- Never place real orders from this skill; the MCP server cannot, and the CLI `live` mode is out of scope.
- State the relation you assumed and why; a wrong relation turns an "arbitrage" into a directional bet.
- Report fees per leg. Kalshi taker fees are 0.07·M·C·P·(1−P); Polymarket taker fees are rate·C·p·(1−p) with a per-market rate; Polymarket makers pay nothing.
- The check assumes every leg fills at the quoted price. Say so when the displayed size is smaller than the order.
