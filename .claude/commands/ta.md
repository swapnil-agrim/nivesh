---
description: Technical read of one security from stored prices
argument-hint: "TICKER"
allowed-tools: mcp__engine__ta_compute mcp__market__resolve_security
---
Show the technical read for the security named in the arguments: $ARGUMENTS

Resolve the ticker with `resolve_security`. If it matches more than one listing, list the
candidates and ask the owner whether they mean the `NSE:` or the `US:` one; do not guess. Then
call `ta_compute` and report the trend and momentum values, the support and resistance levels, the
setup with its entry, stop and target, and every measure the stored data could not support. Quote
numbers exactly as the tool returned them and say which date they are as of.

For a saved, checked note the handler is `nivesh ta $ARGUMENTS --note` (add `--analyst` for one
quick analyst view). Interactive sessions cannot run shell commands, so offer that line to the
owner instead of running it.
