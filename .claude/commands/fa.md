---
description: Fundamental read of one security from stored filings
argument-hint: "TICKER"
allowed-tools: mcp__engine__fa_compute mcp__market__resolve_security
---
Show the fundamental read for the security named in the arguments: $ARGUMENTS

Resolve the ticker with `resolve_security`. If it matches more than one listing, list the
candidates and ask the owner whether they mean the `NSE:` or the `US:` one; do not guess. Then
call `fa_compute` and report growth, profitability, balance sheet and cash quality, the coverage
of the measures and the filing date they come from, and every measure the stored data could not
support. Quote numbers exactly as the tool returned them.

For an Indian company, say which profit each figure uses. Net margin, ROE and the bank earnings
DCF use the profit attributable to the parent where the data allows it (a reported minority
interest, else EPS times shares of the same year) and each metric's inputs name the
`profit_basis`, the consolidated figure and the gap (a group with a large minority, such as
RELIANCE, shows a gap well above 3 percent; one without shows none). When attributable profit
cannot be derived the metric keeps the consolidated figure labelled
`consolidated_minority_unknown` and gives the reason: quote the label and the reason, never
present that number as attributable. Net income growth,
cash conversion (CFO to profit) and accruals use consolidated profit and are labelled
`consolidated`; the free-cash-flow DCF is consolidated as well. Quote the `revenue_basis` the
revenue-based metrics carry (for example ITC revenue is net of excise and indirect taxes where
reported). Say that filing dates before about Sep 2024 are estimates, and that operating margin,
EBITDA, ROIC, ROCE, interest cover, gross margin, working-capital days and the current ratio are
unavailable for India (cash conversion, FCF margin and accruals are computed from the cash flow
statement), each with its reason (ADR-0015). US figures are reported as filed, with no profit
label.

To compare what two stored providers say about the same company, the handler is
`nivesh fa $ARGUMENTS --cross-check` (or `nivesh fundamentals compare $ARGUMENTS`); the compare
never averages and never picks a winner by itself. Offer that line to the owner; do not run it.

For a saved, checked note the handler is `nivesh fa $ARGUMENTS --note` (add `--analyst` for one
quick analyst view). Interactive sessions cannot run shell commands, so offer that line to the
owner instead of running it.
