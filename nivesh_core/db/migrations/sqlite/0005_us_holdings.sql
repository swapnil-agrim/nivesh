-- E3 US holdings (ADR-0006). Forward-only. Rebuilds holding_snapshot so value_inr can be NULL (a
-- non-INR row stores native quantity/price and derives INR at read time; "rate unavailable" must
-- be representable, never zero) and adds currency (existing rows read as INR). Adds the lot table.
-- Nothing references holding_snapshot by foreign key; ids are copied so nothing repoints.
CREATE TABLE holding_snapshot_new (
  id INTEGER PRIMARY KEY,
  ingest_id INTEGER NOT NULL REFERENCES ingest(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  security_id INTEGER NOT NULL REFERENCES security(id),
  holder_ref TEXT NOT NULL DEFAULT '',
  quantity TEXT NOT NULL,
  avg_cost TEXT,
  price TEXT NOT NULL,
  price_basis TEXT NOT NULL,
  value_inr TEXT,
  as_of TEXT NOT NULL,
  source TEXT NOT NULL,
  plan TEXT,
  currency TEXT NOT NULL DEFAULT 'INR'
);
INSERT INTO holding_snapshot_new
  (id, ingest_id, account_id, security_id, holder_ref, quantity, avg_cost, price, price_basis,
   value_inr, as_of, source, plan)
  SELECT id, ingest_id, account_id, security_id, holder_ref, quantity, avg_cost, price,
         price_basis, value_inr, as_of, source, plan FROM holding_snapshot;
DROP TABLE holding_snapshot;
ALTER TABLE holding_snapshot_new RENAME TO holding_snapshot;
CREATE INDEX holding_snapshot_ingest ON holding_snapshot(ingest_id);

-- A dated purchase lot (US CSV). Belongs to an ingest and follows the same newest-ingest rule.
CREATE TABLE lot (
  id INTEGER PRIMARY KEY,
  ingest_id INTEGER NOT NULL REFERENCES ingest(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  security_id INTEGER NOT NULL REFERENCES security(id),
  holder_ref TEXT NOT NULL DEFAULT '',
  acquired_on TEXT NOT NULL,
  quantity TEXT NOT NULL,
  cost_per_unit TEXT NOT NULL,
  currency TEXT NOT NULL
);
CREATE INDEX lot_ingest ON lot(ingest_id);
