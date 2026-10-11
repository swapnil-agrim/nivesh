-- E12 ledger and evaluation (ADR-0013). Forward-only and additive: three columns on the ledger,
-- two new append-only tables. Nothing is rebuilt or backfilled (old rows keep as_of NULL).

ALTER TABLE ledger_entry ADD COLUMN as_of TEXT;
ALTER TABLE ledger_entry ADD COLUMN price_date TEXT;
ALTER TABLE ledger_entry ADD COLUMN source TEXT NOT NULL DEFAULT 'verdict'
  CHECK (source IN ('verdict', 'holding_review'));

-- Owner decisions on a call. A correction is another row whose corrects_id names the one it
-- replaces. on_date is the trade date; decided_at is the record time.
CREATE TABLE ledger_decision (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES ledger_entry(id),
  decision TEXT NOT NULL CHECK (decision IN ('acted', 'skipped', 'partial')),
  qty TEXT,
  price TEXT,
  currency TEXT CHECK (currency IN ('INR', 'USD')),
  note TEXT,
  on_date TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  corrects_id INTEGER REFERENCES ledger_decision(id)
);
CREATE INDEX ledger_decision_by_entry ON ledger_decision(entry_id, id);
CREATE TRIGGER ledger_decision_no_update BEFORE UPDATE ON ledger_decision
BEGIN
  SELECT RAISE(ABORT, 'ledger_decision is append-only: add a correcting row instead');
END;
CREATE TRIGGER ledger_decision_no_delete BEFORE DELETE ON ledger_decision
BEGIN
  SELECT RAISE(ABORT, 'ledger_decision is append-only: add a correcting row instead');
END;

-- One row per call, horizon and scoring version. Many insufficient_data rows may exist for a
-- key, but only one scored row (partial unique index), so a later data backfill can append a
-- scored row without any edit.
CREATE TABLE ledger_score (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES ledger_entry(id),
  horizon_weeks INTEGER NOT NULL CHECK (horizon_weeks IN (1, 4, 13, 26, 52)),
  status TEXT NOT NULL CHECK (status IN ('scored', 'insufficient_data')),
  ret TEXT,
  benchmark_ret TEXT,
  cost_pct TEXT,
  excess TEXT,
  call_excess TEXT,
  hit INTEGER CHECK (hit IN (0, 1)),
  end_date TEXT,
  benchmark_symbol TEXT,
  basis TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  scored_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ledger_score_scored
  ON ledger_score(entry_id, horizon_weeks, version) WHERE status = 'scored';
CREATE INDEX ledger_score_by_key ON ledger_score(entry_id, horizon_weeks, version, id);
CREATE TRIGGER ledger_score_no_update BEFORE UPDATE ON ledger_score
BEGIN
  SELECT RAISE(ABORT, 'ledger_score is append-only: add a new row instead');
END;
CREATE TRIGGER ledger_score_no_delete BEFORE DELETE ON ledger_score
BEGIN
  SELECT RAISE(ABORT, 'ledger_score is append-only: add a new row instead');
END;
