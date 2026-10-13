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

For an Indian company, say that net income, net margin and ROE come from Tapetide's Net Profit,
which includes minority interest (overstated for groups with large minorities, for example
RELIANCE and HDFCBANK), that filing dates before about Sep 2024 are estimates, and that
operating margin, EBITDA, ROIC and cash items are unavailable for India (ADR-0015).

For a saved, checked note the handler is `nivesh fa $ARGUMENTS --note` (add `--analyst` for one
quick analyst view). Interactive sessions cannot run shell commands, so offer that line to the
owner instead of running it.
