---
description: Cost against the cap, HDFC session, last ingests, cache freshness and recent runs
argument-hint: "(no arguments)"
allowed-tools: mcp__holdings__session_status
---
Report the system status. $ARGUMENTS

Use `session_status` for the HDFC session. The handler is `nivesh status`, which also prints
month-to-date cost against the cap and the budget gate, the newest ingest per source, how fresh the
cache is, and the last runs with their status and cost. Interactive sessions cannot run shell
commands, so offer that line to the owner. Never print a session value.
