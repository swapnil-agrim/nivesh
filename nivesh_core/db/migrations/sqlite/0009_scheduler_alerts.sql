-- E11 scheduling and alerts (ADR-0014). Forward-only and additive: three new tables, no existing
-- table is rebuilt and nothing is backfilled. Decimals are stored as exact text, times as UTC
-- ISO text, dates as ISO text.

-- One row per (job, trading date): the idempotency key of the scheduler. This table is mutable
-- on purpose: a row is claimed as running, then finished (status, attempts, reason).
CREATE TABLE job_run (
  job TEXT NOT NULL,
  trading_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'skipped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  run_id INTEGER REFERENCES run(id),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  reason TEXT,
  late INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0, 1)),
  notified INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0, 1)),
  PRIMARY KEY (job, trading_date)
);

-- Fired alerts. delivered: 0 not sent (held back, blocked or no channel), 1 sent alone, 2 sent
-- inside a digest. The delivery outcome is known before the insert, so a row is never updated.
CREATE TABLE alert (
  alert_id INTEGER PRIMARY KEY,
  rule TEXT NOT NULL,
  security_id INTEGER REFERENCES security(id),
  fired_at TEXT NOT NULL,
  trading_date TEXT NOT NULL,
  payload TEXT NOT NULL,
  delivered INTEGER NOT NULL CHECK (delivered IN (0, 1, 2)),
  digest_id INTEGER
);
CREATE INDEX alert_by_rule ON alert(rule, security_id, alert_id);
CREATE INDEX alert_by_date ON alert(trading_date);

-- Owner-supplied numeric invalidation level per security; the latest row wins.
CREATE TABLE invalidation_level (
  id INTEGER PRIMARY KEY,
  security_id INTEGER NOT NULL REFERENCES security(id),
  level TEXT NOT NULL,
  set_on TEXT NOT NULL,
  note TEXT
);
CREATE INDEX invalidation_level_by_security ON invalidation_level(security_id, id);

CREATE TRIGGER alert_no_update BEFORE UPDATE ON alert
BEGIN
  SELECT RAISE(ABORT, 'alert is append-only: add a new row instead');
END;
CREATE TRIGGER alert_no_delete BEFORE DELETE ON alert
BEGIN
  SELECT RAISE(ABORT, 'alert is append-only: add a new row instead');
END;
-- A replace removes the old row without firing the delete trigger, so refuse an insert whose
-- key already exists (this also covers INSERT OR REPLACE and REPLACE INTO).
CREATE TRIGGER alert_no_replace BEFORE INSERT ON alert
WHEN NEW.alert_id IS NOT NULL AND EXISTS (SELECT 1 FROM alert WHERE alert_id = NEW.alert_id)
BEGIN
  SELECT RAISE(ABORT, 'alert is append-only: add a new row instead');
END;

CREATE TRIGGER invalidation_level_no_update BEFORE UPDATE ON invalidation_level
BEGIN
  SELECT RAISE(ABORT, 'invalidation_level is append-only: add a new row instead');
END;
CREATE TRIGGER invalidation_level_no_delete BEFORE DELETE ON invalidation_level
BEGIN
  SELECT RAISE(ABORT, 'invalidation_level is append-only: add a new row instead');
END;
CREATE TRIGGER invalidation_level_no_replace BEFORE INSERT ON invalidation_level
WHEN NEW.id IS NOT NULL AND EXISTS (SELECT 1 FROM invalidation_level WHERE id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'invalidation_level is append-only: add a new row instead');
END;
