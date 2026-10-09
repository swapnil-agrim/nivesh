-- ST-13.3/13.4: per-run trace location, model, tokens, tier and paid-data cost.
-- ALTER ... ADD COLUMN only (no table rebuild), so v1 rows survive with defaults.
ALTER TABLE run ADD COLUMN run_dir TEXT;
ALTER TABLE run ADD COLUMN model TEXT;
ALTER TABLE run ADD COLUMN prompt_version TEXT;
ALTER TABLE run ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run ADD COLUMN paid_data_inr REAL NOT NULL DEFAULT 0;
ALTER TABLE run ADD COLUMN tier TEXT NOT NULL DEFAULT 'quick';
CREATE INDEX run_started ON run(started_at);
