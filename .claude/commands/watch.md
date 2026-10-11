---
description: Manage the watchlist and check watched names against their entry zones
argument-hint: "add TICKER | list | check"
---
Work with the watchlist: $ARGUMENTS

The handler is `nivesh watch $ARGUMENTS`; the sub-commands are listed by `nivesh watch --help`.
Interactive sessions cannot run shell commands, so tell the owner which line to run. A ticker that
matches several listings is listed with its candidates; ask the owner for the ISIN or an
`NSE:` or `US:` prefix. The watchlist holds identifiers and entry zones only, never quantities.
