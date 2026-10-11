# Runbook: scoring the call ledger by hand (E12)

Nothing runs this on a timer. Run the three commands in order whenever you want a fresh view.
None of them calls a model, and none spends money.

1. `nivesh rescore` scores every call in the ledger that has become measurable at 1, 4, 13, 26
   and 52 weeks and appends rows; it prints "scored N, insufficient M, pending K". A second run
   right after prints scored 0. A call is pending (no row) until both its security and its
   benchmark index have a bar on or after the target date; load fresh prices first
   (`nivesh market prices`, `nivesh ingest`).
2. `nivesh scorecard [--since YYYY-MM-DD]` saves and prints the scorecard: hit rate, count, mean
   and median excess and the worst call by verdict, horizon, market, conviction and command, and
   system calls against calls you acted on. A group with fewer than 20 scored calls says "not
   enough data". Add `--deliver` to send it to the channels you configured.
3. `nivesh calibration [--json]` asks whether higher conviction hits more often. With fewer than
   40 scored calls it prints "not enough data (N of 40 scored calls)".

Record what you did about a call so the scorecard can tell system calls from acted ones:
`nivesh decide ENTRY_ID acted --qty 10 --price 123.45 --on 2026-10-01`, or `skipped`, or
`partial`. A mistake is fixed with `--corrects DECISION_ID`, which adds a row; nothing is edited.
Sales you record this way count against the yearly turnover cap in `nivesh review rebalance`.

Read every figure with its note: scores are split-adjusted price returns against a price index,
not a total-return index, with assumed round-trip costs (India 0.25 percent, US 0.10 percent).
They are your own record, not advice. Settings are in the `scoring:` block of `config/nivesh.yaml`;
the decisions are in `docs/adr/0013-ledger-scoring-evaluation.md`.

Release check: `uv run pytest tests/eval` runs the golden set against `tests/eval/baseline.json`.
