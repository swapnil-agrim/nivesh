---
description: Hit rate, excess return and worst call of past calls, by verdict, horizon, market, conviction and command
argument-hint: "[--since YYYY-MM-DD]"
---
Show the scorecard of past calls: $ARGUMENTS

The handler is `nivesh scorecard $ARGUMENTS`. It reads the append-only call ledger and the stored
scores only, makes no model call and costs nothing; it saves the report under
`data/runs/<date>/<run id>/`. Run `nivesh rescore` first to score calls that have become
measurable. Interactive sessions cannot run shell commands, so offer those lines to the owner.
Keep to the contract: hit rate and counts per verdict, horizon, market, conviction and command,
mean and median excess and the worst call, and system calls against calls the owner acted on.
A group of fewer than 20 scored calls says "not enough data". Scores are split-adjusted price
returns against a price index, not a total-return index; always say so. Never guess a missing
number.
