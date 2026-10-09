-- Analytical store: adapter response cache (ST-1.5). Timestamps are UTC ISO-8601 text.
CREATE TABLE cache_entry (
  adapter VARCHAR NOT NULL,
  params_hash VARCHAR NOT NULL,
  payload VARCHAR NOT NULL,
  source VARCHAR NOT NULL,
  as_of VARCHAR NOT NULL,
  fetched_at VARCHAR NOT NULL,
  PRIMARY KEY (adapter, params_hash)
);
