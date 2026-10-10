-- E4 security master (ST-4.8). Forward-only and additive: nullable columns, new tables, one index.
-- No backfill needed; name_norm is filled by `nivesh master build` (and lazily by lookups).
ALTER TABLE security ADD COLUMN sector TEXT;
ALTER TABLE security ADD COLUMN industry TEXT;
ALTER TABLE security ADD COLUMN name_norm TEXT;
CREATE INDEX security_isin ON security(isin);

-- Old/other identifiers that still resolve to the current security.
CREATE TABLE security_alias (
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  security_id INTEGER NOT NULL REFERENCES security(id),
  note TEXT,
  PRIMARY KEY (kind, value, security_id)
);

-- In-database backup of rows a placeholder merge dropped as duplicates (same transaction).
CREATE TABLE master_merge_log (
  id INTEGER PRIMARY KEY,
  placeholder_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  row_json TEXT NOT NULL,
  merged_at TEXT NOT NULL
);
