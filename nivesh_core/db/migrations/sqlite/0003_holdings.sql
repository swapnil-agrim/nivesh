-- E2 holdings ingestion (ADR-0004). Forward-only; ALTER ... ADD COLUMN, no table rebuild.
-- Money and quantities are TEXT holding exact Decimal strings; sums happen in Python.
-- PID `asset_type` is the existing `asset_class`; PID account `label` is the existing `name`.
ALTER TABLE security ADD COLUMN amfi_code TEXT;
ALTER TABLE security ADD COLUMN market TEXT NOT NULL DEFAULT 'IN';
-- An unresolved ISIN is stored as symbol=<ISIN>, exchange='ISIN', unresolved=1 (fits UNIQUE).
ALTER TABLE security ADD COLUMN unresolved INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX account_kind_name ON account(kind, name);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- One row per ingest run (an InvestRight sync, a CAS file, a CSV file).
CREATE TABLE ingest (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  source_label TEXT NOT NULL,
  digest TEXT,
  as_of TEXT NOT NULL,
  created_at TEXT NOT NULL,
  report TEXT NOT NULL
);
CREATE UNIQUE INDEX ingest_digest ON ingest(kind, digest) WHERE digest IS NOT NULL;

-- Every demat/folio seen in an ingest, including zero-balance ones (drives the "latest view").
CREATE TABLE ingest_holder (
  ingest_id INTEGER NOT NULL REFERENCES ingest(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  holder_ref TEXT NOT NULL,
  PRIMARY KEY (ingest_id, account_id, holder_ref)
);

-- Append-only per ingest.
CREATE TABLE holding_snapshot (
  id INTEGER PRIMARY KEY,
  ingest_id INTEGER NOT NULL REFERENCES ingest(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  security_id INTEGER NOT NULL REFERENCES security(id),
  holder_ref TEXT NOT NULL DEFAULT '',
  quantity TEXT NOT NULL,
  avg_cost TEXT,
  price TEXT NOT NULL,
  price_basis TEXT NOT NULL,
  value_inr TEXT NOT NULL,
  as_of TEXT NOT NULL,
  source TEXT NOT NULL,
  plan TEXT
);
CREATE INDEX holding_snapshot_ingest ON holding_snapshot(ingest_id);

-- `transaction` is a reserved word. Deduplicated across overlapping statements; `occurrence` is the
-- nth identical row within one statement, so two identical same-day debits are both kept.
CREATE TABLE txn (
  id INTEGER PRIMARY KEY,
  ingest_id INTEGER NOT NULL REFERENCES ingest(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  security_id INTEGER NOT NULL REFERENCES security(id),
  holder_ref TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL,
  type TEXT NOT NULL,
  quantity TEXT NOT NULL DEFAULT '0',
  price TEXT,
  amount TEXT NOT NULL DEFAULT '0',
  occurrence INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX txn_dedup
  ON txn(account_id, security_id, holder_ref, date, type, quantity, amount, occurrence);
CREATE INDEX txn_security_date ON txn(security_id, date);
