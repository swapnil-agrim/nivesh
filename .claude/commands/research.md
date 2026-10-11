---
description: Full committee research note on one security, saved as a checked report
argument-hint: "TICKER [quick|deep]"
---
Run the committee on the security named in the arguments and give the owner the research note.

The handler is `nivesh research $ARGUMENTS` (tier defaults to deep). Interactive sessions cannot
run shell commands, so tell the owner to run that line in a terminal. It prints the note and saves
`report.md`, `report.html`, the facts it was checked against and the trace under
`data/runs/<date>/<run id>/`.

Contract for the note: verdict box, thesis, fundamentals, valuation, technical setup, news and
catalysts, bull and bear cases, risks and invalidation, sizing, data gaps and sources. An ambiguous
ticker is listed with its candidates; ask the owner to retry with `NSE:SYMBOL` or `US:SYMBOL`.
Every number in the note is checked against the run; unmatched numbers are listed under a
`DRAFT - UNVERIFIED NUMBERS` banner and the run status becomes `needs_review`. A quick run costs
less than a deep one and the budget gate may downgrade a deep run near the monthly cap.

This is the owner's own research, not investment advice.
