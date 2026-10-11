---
description: One-page market brief, at most 400 words plus one table
argument-hint: "[india|us|both]"
allowed-tools: mcp__macro__get_rates_snapshot mcp__macro__get_flows_india mcp__market__get_index
---
Give the market brief for: $ARGUMENTS (default both).

The handler is `nivesh brief $ARGUMENTS`. It reads the stored data only, makes no model call and
costs nothing; it saves the report under `data/runs/<date>/<run id>/`. Interactive sessions cannot
run shell commands, so offer that line to the owner. If asked to summarise here instead, use the
read tools and keep to the contract: index levels and moves, the market regime, rates, currency
and crude, institutional flows for India, the overnight US session, events for holdings and the
watchlist, and material news for holdings. Anything missing is listed as unavailable with the
reason, never guessed. At most 400 words and one table.
