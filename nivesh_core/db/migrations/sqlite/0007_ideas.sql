-- E9 idea generation (ADR-0011). Forward-only: three new tables only; no existing table is
-- rebuilt and nothing is backfilled. Decimals are stored as exact text.

-- Current constituent snapshot per index, loaded from an owner-supplied file. A reload replaces
-- the whole snapshot of that index in one transaction, so there is one `as_of` per index.
CREATE TABLE index_member (
  index_id TEXT NOT NULL,
  security_id INTEGER NOT NULL REFERENCES security(id),
  as_of TEXT NOT NULL,
  PRIMARY KEY (index_id, security_id)
);

-- Append-only record of every committee call (the ST-12.1 field list plus `reported`, `preset`
-- and `created_at`). A correction is a new row whose corrects_id names the row it replaces.
CREATE TABLE ledger_entry (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES run(id),
  security_id INTEGER NOT NULL REFERENCES security(id),
  verdict TEXT NOT NULL CHECK (verdict IN
    ('BUY', 'ACCUMULATE', 'HOLD', 'TRIM', 'SELL', 'AVOID', 'INSUFFICIENT_DATA')),
  horizon TEXT NOT NULL CHECK (horizon IN ('positional_1_6m', 'long_term_1y_plus')),
  conviction TEXT NOT NULL CHECK (conviction IN ('low', 'medium', 'high')),
  suggested_weight_pct TEXT,
  entry_low TEXT,
  entry_high TEXT,
  entry_currency TEXT CHECK (entry_currency IN ('INR', 'USD')),
  invalidation TEXT NOT NULL,
  review_date TEXT NOT NULL,
  last_close TEXT,
  benchmark_level TEXT,
  benchmark_reason TEXT,
  input_hash TEXT NOT NULL,
  prompt_versions TEXT NOT NULL,
  model_versions TEXT NOT NULL,
  reported INTEGER NOT NULL CHECK (reported IN (0, 1)),
  preset TEXT,
  corrects_id INTEGER REFERENCES ledger_entry(id),
  created_at TEXT NOT NULL
);
CREATE INDEX ledger_entry_by_run ON ledger_entry(run_id);
CREATE INDEX ledger_entry_by_security ON ledger_entry(security_id, id);
CREATE TRIGGER ledger_entry_no_update BEFORE UPDATE ON ledger_entry
BEGIN
  SELECT RAISE(ABORT, 'ledger_entry is append-only: add a correcting row instead');
END;
CREATE TRIGGER ledger_entry_no_delete BEFORE DELETE ON ledger_entry
BEGIN
  SELECT RAISE(ABORT, 'ledger_entry is append-only: add a correcting row instead');
END;

-- Securities the owner tracks, with an optional entry zone.
CREATE TABLE watch (
  security_id INTEGER PRIMARY KEY REFERENCES security(id),
  entry_low TEXT,
  entry_high TEXT,
  added_on TEXT NOT NULL,
  CHECK ((entry_low IS NULL) = (entry_high IS NULL))
);
