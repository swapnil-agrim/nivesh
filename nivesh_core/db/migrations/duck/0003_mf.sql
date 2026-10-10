-- E5 mutual-fund intelligence. Forward-only and additive: new tables only (no existing table is
-- touched), so no backfill. Money and ratios are DECIMAL, never float. Timestamps are UTC text.
-- security_id is the SQLite security.id of the MF row (no cross-store foreign key).
CREATE TABLE nav_point (
  security_id INTEGER NOT NULL,
  date DATE NOT NULL,
  nav DECIMAL(18,4) NOT NULL,
  source VARCHAR NOT NULL,
  fetched_at VARCHAR,
  PRIMARY KEY (security_id, date, source)
);

-- Append-only by as_of: the latest as_of wins on read, history stays. TER and AUM may be NULL
-- (unknown), never zero.
CREATE TABLE fund_meta (
  security_id INTEGER NOT NULL,
  as_of DATE NOT NULL,
  amfi_code VARCHAR NOT NULL,
  scheme_name VARCHAR NOT NULL,
  amc VARCHAR,
  category VARCHAR,
  plan VARCHAR,
  option VARCHAR,
  expense_ratio DECIMAL(7,4),
  aum_crore DECIMAL(24,2),  -- rupees crore: keeps stored values clear of long digit runs
  benchmark VARCHAR,
  manager VARCHAR,
  manager_since DATE,
  source VARCHAR NOT NULL,
  PRIMARY KEY (security_id, as_of, source)
);

-- kind is 'equity' or 'other' (cash, derivative and debt lines stay as 'other', never dropped).
CREATE TABLE fund_holding (
  security_id INTEGER NOT NULL,
  month_end DATE NOT NULL,
  isin VARCHAR NOT NULL,
  weight_pct DECIMAL(9,4) NOT NULL,
  holding_security_id INTEGER,
  kind VARCHAR NOT NULL,
  source VARCHAR NOT NULL,
  PRIMARY KEY (security_id, month_end, isin, source)
);

CREATE TABLE nav_gap (
  security_id INTEGER NOT NULL,
  gap_start DATE NOT NULL,
  gap_end DATE NOT NULL,
  missing_days INTEGER NOT NULL,
  detected_at VARCHAR,
  PRIMARY KEY (security_id, gap_start)
);
