-- Core tables only; other PID section 13 entities arrive as later-epic migrations (ADR-0002).
-- All timestamps are UTC ISO-8601 text (NFR-19). No account numbers are stored.
CREATE TABLE security (
  id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL,
  exchange TEXT NOT NULL,
  name TEXT,
  isin TEXT,
  currency TEXT NOT NULL,
  asset_class TEXT,
  UNIQUE (symbol, exchange)
);
CREATE TABLE account (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT,
  base_currency TEXT NOT NULL DEFAULT 'INR',
  created_at TEXT NOT NULL
);
CREATE TABLE run (
  id INTEGER PRIMARY KEY,
  command TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  cost_inr REAL NOT NULL DEFAULT 0
);
