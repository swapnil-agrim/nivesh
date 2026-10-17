-- Vendor call ledger and quota limits (ADR-0016). Forward-only and additive: two new tables, no
-- existing table is rebuilt and nothing is backfilled. Times are UTC ISO text, `day_ist` is the
-- IST calendar date as ISO text (the vendor's day), counts are integers.

-- One row per request that left (or was about to leave) the machine, written BEFORE the request
-- so a crash can only over-count. A row is never refunded or removed; its accounting columns are
-- fixed at insert and only `outcome` may be set, exactly once (NULL to ok, error or halted), so the
-- owner can compare outcomes with the vendor's dashboard. kind 'manual' rows record calls spent
-- by other clients of the same plan (`calls` can then be more than 1).
CREATE TABLE vendor_call (
  id INTEGER PRIMARY KEY,
  vendor TEXT NOT NULL,
  called_at TEXT NOT NULL,
  day_ist TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'call' CHECK (kind IN ('call', 'manual')),
  calls INTEGER NOT NULL DEFAULT 1 CHECK (calls >= 1),
  tool TEXT,
  symbol TEXT,
  priority TEXT CHECK (priority IS NULL OR priority IN ('holdings', 'watchlist', 'universe')),
  outcome TEXT CHECK (outcome IS NULL OR outcome IN ('ok', 'error', 'halted'))
);
CREATE INDEX vendor_call_by_day ON vendor_call(vendor, day_ist);

-- Owner overrides of the configured plan limits; the latest non-null value per field wins.
CREATE TABLE vendor_limit (
  id INTEGER PRIMARY KEY,
  vendor TEXT NOT NULL,
  daily INTEGER CHECK (daily IS NULL OR daily >= 1),
  monthly INTEGER CHECK (monthly IS NULL OR monthly >= 1),
  set_at TEXT NOT NULL
);

-- SQLite fires an UPDATE OF trigger only when a listed column is a SET target, so setting
-- `outcome` alone is allowed by the first trigger and guarded by the second.
CREATE TRIGGER vendor_call_accounting_is_fixed
BEFORE UPDATE OF id, vendor, called_at, day_ist, kind, calls, tool, symbol, priority ON vendor_call
BEGIN
  SELECT RAISE(ABORT, 'vendor_call is append-only: add a new row instead');
END;
CREATE TRIGGER vendor_call_outcome_is_set_once
BEFORE UPDATE OF outcome ON vendor_call
WHEN OLD.outcome IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'vendor_call outcome can be set once; the ledger is append-only');
END;
CREATE TRIGGER vendor_call_no_delete BEFORE DELETE ON vendor_call
BEGIN
  SELECT RAISE(ABORT, 'vendor_call is append-only: add a new row instead');
END;
-- A replace removes the old row without firing the delete trigger, so refuse an insert whose
-- key already exists (this also covers INSERT OR REPLACE and REPLACE INTO).
CREATE TRIGGER vendor_call_no_replace BEFORE INSERT ON vendor_call
WHEN NEW.id IS NOT NULL AND EXISTS (SELECT 1 FROM vendor_call WHERE id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'vendor_call is append-only: add a new row instead');
END;

CREATE TRIGGER vendor_limit_no_update BEFORE UPDATE ON vendor_limit
BEGIN
  SELECT RAISE(ABORT, 'vendor_limit is append-only: add a new row instead');
END;
CREATE TRIGGER vendor_limit_no_delete BEFORE DELETE ON vendor_limit
BEGIN
  SELECT RAISE(ABORT, 'vendor_limit is append-only: add a new row instead');
END;
CREATE TRIGGER vendor_limit_no_replace BEFORE INSERT ON vendor_limit
WHEN NEW.id IS NOT NULL AND EXISTS (SELECT 1 FROM vendor_limit WHERE id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'vendor_limit is append-only: add a new row instead');
END;
