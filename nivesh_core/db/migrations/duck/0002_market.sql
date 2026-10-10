-- E4 market data. Forward-only and additive: new tables only (cache_entry untouched), so no backfill.
-- Money is DECIMAL, never float. Timestamps are UTC ISO-8601 text. security_id is the SQLite
-- security.id (no cross-store foreign key: SQLite owns identity).
CREATE SEQUENCE news_id_seq;
CREATE SEQUENCE filing_id_seq;

CREATE TABLE price_bar (
  security_id INTEGER NOT NULL,
  date DATE NOT NULL,
  open DECIMAL(18,6),
  high DECIMAL(18,6),
  low DECIMAL(18,6),
  close DECIMAL(18,6) NOT NULL,
  volume BIGINT,
  adj_close DECIMAL(18,6),
  source VARCHAR NOT NULL,
  flag VARCHAR,
  fetched_at VARCHAR,
  PRIMARY KEY (security_id, date, source)
);

CREATE TABLE corp_action (
  security_id INTEGER NOT NULL,
  ex_date DATE NOT NULL,
  kind VARCHAR NOT NULL,
  ratio DECIMAL(18,8),
  amount DECIMAL(18,6),
  source VARCHAR NOT NULL,
  PRIMARY KEY (security_id, ex_date, kind, source)
);

-- Long format (bank fields are sparse). Append-only across filed_at so restatements keep history.
CREATE TABLE fundamental (
  security_id INTEGER NOT NULL,
  period_end DATE NOT NULL,
  period_type VARCHAR NOT NULL,
  item VARCHAR NOT NULL,
  value DECIMAL(28,4) NOT NULL,
  currency VARCHAR,
  source VARCHAR NOT NULL,
  filed_at DATE NOT NULL,
  fetched_at VARCHAR,
  PRIMARY KEY (security_id, period_end, period_type, item, source, filed_at)
);

CREATE TABLE shareholding (
  security_id INTEGER NOT NULL,
  period_end DATE NOT NULL,
  promoter_pct DECIMAL(9,4),
  promoter_pledged_pct DECIMAL(9,4),
  public_pct DECIMAL(9,4),
  source VARCHAR NOT NULL,
  filed_at DATE NOT NULL,
  PRIMARY KEY (security_id, period_end, source, filed_at)
);

CREATE TABLE estimate (
  security_id INTEGER NOT NULL,
  metric VARCHAR NOT NULL,
  period VARCHAR NOT NULL,
  value DECIMAL(28,4) NOT NULL,
  as_of DATE NOT NULL,
  source VARCHAR NOT NULL,
  PRIMARY KEY (security_id, metric, period, as_of, source)
);

CREATE TABLE macro_series (
  series_id VARCHAR NOT NULL,
  date DATE NOT NULL,
  value DECIMAL(24,8) NOT NULL,
  source VARCHAR NOT NULL,
  PRIMARY KEY (series_id, date)
);

CREATE TABLE filing (
  id INTEGER PRIMARY KEY DEFAULT nextval('filing_id_seq'),
  security_id INTEGER NOT NULL,
  form VARCHAR NOT NULL,
  filed_at DATE NOT NULL,
  period_end DATE,
  source VARCHAR NOT NULL,
  url VARCHAR,
  doc_key VARCHAR NOT NULL UNIQUE
);

CREATE TABLE filing_section (
  filing_id INTEGER NOT NULL,
  section VARCHAR NOT NULL,
  text VARCHAR NOT NULL,
  PRIMARY KEY (filing_id, section)
);

CREATE TABLE news_item (
  id INTEGER PRIMARY KEY DEFAULT nextval('news_id_seq'),
  security_id INTEGER,
  kind VARCHAR NOT NULL,
  published_at VARCHAR NOT NULL,
  source VARCHAR NOT NULL,
  title VARCHAR NOT NULL,
  url VARCHAR,
  url_canon VARCHAR,
  summary VARCHAR,
  event_type VARCHAR,
  materiality VARCHAR,
  sentiment VARCHAR,
  classified_by VARCHAR,
  fetched_at VARCHAR
);

CREATE TABLE calendar_event (
  security_id INTEGER NOT NULL,
  event_type VARCHAR NOT NULL,
  event_date DATE NOT NULL,
  source VARCHAR NOT NULL,
  as_of DATE,
  PRIMARY KEY (security_id, event_type, event_date, source)
);
