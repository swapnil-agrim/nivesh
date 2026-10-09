# ADR-0002: Local stores and migrations

Status: accepted

- SQLite (transactional: security, account, run, later holdings/ledger) and DuckDB (analytical: cache, market data), under an owner-only (0700) data dir; files 0600 from creation (umask/`O_EXCL`), not chmod-after.
- Forward-only `NNNN_name.sql` migrations, one transaction per file, `schema_version` table, downgrade refused.
- Timestamps stored as UTC ISO-8601 text (NFR-19).
- Waiver (ST-1.3 AC1): migration 0001 creates only the core tables (`security`, `account`, `run`) and DuckDB `cache_entry`. The remaining PID section 13 entities are added by the epic that first needs them, as new migrations. Recorded in README "Deferred" and the PR body.
