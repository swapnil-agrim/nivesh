-- E8 thesis store (ADR-0010). Forward-only: a new table and index only; no existing table is
-- rebuilt and nothing is backfilled. Rows are append-only history; at most one active row per
-- security. kill_criteria is JSON with Decimal thresholds as strings.
CREATE TABLE thesis (
  thesis_id INTEGER PRIMARY KEY,
  security_id INTEGER NOT NULL REFERENCES security(id),
  created_at TEXT NOT NULL,
  horizon TEXT NOT NULL CHECK (horizon IN ('positional_1_6m', 'long_term_1y_plus')),
  why TEXT NOT NULL,
  kill_criteria TEXT NOT NULL,
  target_review_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'closed')),
  source TEXT NOT NULL CHECK (source IN ('onboarding', 'decision'))
);
CREATE UNIQUE INDEX thesis_one_active ON thesis(security_id) WHERE status = 'active';
