-- A schema-version-1 DB with seeded rows (shape of migrations/sqlite/0001_core.sql).
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
INSERT INTO schema_version VALUES (1, '0001_core', '2026-01-01T00:00:00+00:00');
CREATE TABLE security (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, exchange TEXT NOT NULL,
  name TEXT, isin TEXT, currency TEXT NOT NULL, asset_class TEXT, UNIQUE (symbol, exchange));
INSERT INTO security (symbol, exchange, name, currency) VALUES ('TESTCO', 'NSE', 'Test Co', 'INR');
INSERT INTO security (symbol, exchange, name, currency) VALUES ('DEMO', 'NYSE', 'Demo Inc', 'USD');
CREATE TABLE account (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT,
  base_currency TEXT NOT NULL DEFAULT 'INR', created_at TEXT NOT NULL);
CREATE TABLE run (id INTEGER PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL, cost_inr REAL NOT NULL DEFAULT 0);
