# ADR-0011: Idea generation (E9)

Status: accepted. E9 adds investable universes, four YAML presets, a shortlist, `nivesh ideas`, a
minimal call ledger and a watchlist (ST-9.1 to ST-9.5, with the ST-12.1 interface). Agents see only
read-only tools and are tested against the scripted fake SDK (no network, no model call, no
personal data); every number the shortlist and the ranking use comes from deterministic Decimal
code with injected dates. There is **no new MCP server**, **no new agent**, no new prompt or
schema, no new allow-list entry and no new dependency. Nothing here is advice: every threshold is
an example the owner tunes.

## Owner decisions (safest option chosen; owner sign-off is X9)
- **OD-1 Constituents are owner-supplied, never fetched.** `nivesh universe load --index NAME
  --file PATH [--as-of DATE]` ingests a CSV (`symbol`, optional `isin`, optional `sector`)
  offline. Each row resolves to a `security_id` through the security master (ISIN first, then
  symbol inside the index's market); unresolved rows are reported with their symbols, never
  dropped silently, and a file where nothing resolves is refused. Index names are config-defined
  (`universe.indices`), so Nifty Smallcap 250 and Russell 1000 work with an owner file; no list is
  bundled (X1, X6).
- **OD-2 An empty universe raises.** An enabled index with no members, or a universe empty after
  the filters, raises ("no members loaded for NIFTY500; run `nivesh universe load`"). It never
  falls back to "all stored bars": that would widen the universe silently. The explicit-id path
  (`screen --security`) is unchanged.
- **OD-3 Staleness is a warning.** A membership snapshot older than `universe.max_age_days` (35)
  prints a warning. A snapshot dated after the screen date is refused: `index_member` is a
  current snapshot, and using it for an earlier date would look ahead.
- **OD-4 Liquidity floor.** The 20-day average daily value traded (window `risk.adv_days`) must be
  at least INR 5 crore (1 crore = 10^7; `universe.liquidity.IN.min_adv_crore`) or USD 20 million
  (`min_adv_usd_m`). The comparison is in the bar currency (USD before any FX) and equal passes.
  Fewer than 20 bars with a volume, a missing volume, or a last bar older than
  `universe.max_bar_age_days` (7 calendar days) is "insufficient liquidity data", never guessed.
  The calculation is the one `risk` uses for days to trade (`adv_value`), extracted, so the two
  agree; risk output is byte-identical.
- **OD-5 Exclusions match sector too.** One helper (`matches_exclusion`) serves the universe filter
  and the risk veto: a profile exclusion matches a symbol, ISIN, name or sector, casefolded and
  whole. A fund's AMFI code is still not a match.
- **OD-6 Market cap is close times `shares_out` only.** A registry metric `market_cap` and a
  bucket through `analysis.xray.market_cap` bands ("unclassified" when unset). A missing
  `shares_out` gives "unavailable: no shares_out filed", never zero. An external source (rest of
  #72) is X3. Presets do not require it.
- **OD-7 Presets are pure YAML** in `config/screens/` on registered metrics. The thresholds are
  example starting points. They use `roe`, not `roce`: India filings carry operating income as
  profit before tax, so `roce` is unavailable for Indian names (a finding, see X10). **BR-12 lint:**
  any rule on a metric named `roc_*`, `rs_*`, `price_vs_*`, `base_breakout`, `pullback_to_50dma` or
  `setup_type` must sit in a preset with at least one `required` rule on a metric of the `fa`,
  `valuation` or `estimates` bundle, read from the registry; a test proves the lint can fail.
  The horizon is the name prefix: `lt-` long term, `pos-` positional.
- **OD-8 Shortlist.** `ideas.shortlist_size` 8, hard `ideas.shortlist_max` 12 (a larger size is a
  validation error and is checked again at the call site), `ideas.sector_cap` 2. Ranked by
  composite descending, then symbol. A name without a sector shares the `unclassified` bucket,
  which is **not capped** (a missing field must not shrink the list to two) and is warned about.
  Held names are labelled "ADD candidate" and kept (`ideas.held = label`; `exclude` drops them).
  `ideas.max_runs` (16) caps the committee runs of one command across markets.
- **OD-9 Minimal ledger (ST-12.1).** `ledger_entry` is `append-only`: BEFORE UPDATE and BEFORE
  DELETE triggers raise, and a correction is a new row whose `corrects_id` names the row it
  replaces. Columns are exactly the ST-12.1 list (run id, security id, verdict, horizon,
  conviction, suggested weight, entry zone low and high with currency, invalidation, review date,
  last close, benchmark level or the reason it is missing, input hash, prompt and model versions)
  plus three additive columns: `reported` (the row is one of the top n), `preset` and `created_at`.
  Decimals are exact text and are read back through Python Decimal only. `run_id` is a plain
  foreign key, so a run row cannot be removed while ledger rows name it. Scoring, the scorecard
  and the memory server stay with a later epic (X4).
- **OD-10 `/ideas` ranking.** A verdict qualifies when it is BUY or ACCUMULATE and not vetoed by
  risk. Ranking is by conviction (high, medium, low), then composite, then symbol. Fewer than n is
  said ("only k of n ideas qualified; no padding"). The command never changes a verdict: the
  committee rules (cap, veto) stay authoritative. Tier is `deep`; the cost gate may refuse it and
  the command then exits 1 before any spend. With `both`, each market has its own shortlist, there
  is one committee run and one combined ranking.
- **OD-11 Watchlist.** The `watch` table has one row per security with an optional zone. Distance
  is the percent the latest close is above the zone's top, 0 inside, negative below the bottom;
  with no zone or no close it gives a reason. The latest verdict is the newest ledger row. No
  alert is sent (X2).
- **OD-12 Output.** Plain text and `--json` (sorted keys, Decimals as exact strings), like E8; no
  report template and no slash-command file (X5).

## Decisions
1. **Schema and migration notes.** Migration `0007_ideas.sql` adds three tables, `index_member`,
   `ledger_entry` (with its two triggers and indexes) and `watch`. It is **forward-only**: new
   tables only, no existing table rebuilt, no backfill, version 6 to 7 in one transaction by the
   existing runner. Rollback: the runner refuses a store newer than the code, so a code rollback
   after the upgrade means `nivesh restore` of the pre-upgrade backup. Backup and restore carry
   the tables and the triggers (tested). A migration test upgrades a populated version 6 store.
2. **Universe pipeline.** `nivesh_engine/universe.py` holds `adv_value`, `liquidity_ok`,
   `matches_exclusion`, `market_cap_bucket` and `build_universe` (pure; each removed name carries
   one reason). `nivesh_adapters/universe_service.py` reads the stores and feeds it. The screener
   takes `screen --universe india|us` beside `--security`; `rs_percentile` and the score
   percentiles rank within the named universe by design.
3. **Shortlist from stores.** The universe is scored once at the preset's horizon
   (`score_report` over the universe ids), not once per candidate, and the cards are passed to
   `prepare_inputs(..., cards=...)`, which then scores nothing. A security without a card gets an
   empty one, so its coverage reads as a data gap. Securities are named by `id:<n>` so a symbol
   that exists in both markets cannot resolve to the wrong one.
4. **Ledger field sources.** From the verdict: verdict, horizon, conviction, weight, zone,
   invalidation, review date. Read before the agents run: last close (the latest stored close, on
   the adjusted basis the engines use) and the configured benchmark's level then (null with the
   reason when none is configured). From the run's saved snapshot: prompt and model versions and
   the input hash (a SHA-256 of that security's engine outputs and the as-of date). Every committee
   verdict of the shortlist is recorded; `reported` marks the top n.
5. **Run lifecycle.** `ideas` reads every fact with the stores opened read-only, closes them,
   prints the estimated run count, then runs `metered_run(..., "ideas", "deep")` and the
   committee, then reopens SQLite only to add ledger rows. Prices and the USD/INR rate come from
   settings, as in `review holdings`. Agents receive identifiers only: no quantity, value or
   holder reference.
6. **Where writers live.** Ledger, watch and membership writers are in `nivesh_core` and
   `nivesh_cli`, outside every write-word scanned module; reads are in `nivesh_adapters`. No agent
   spec, prompt, schema or MCP file changed, and no agent receives a ledger or watch tool.

## Contract changes
- `universe:` and `ideas:` config blocks are additive and optional (the example blocks equal the
  defaults). New commands only: `universe load|show`, `ideas`, `watch add|list`, and
  `screen --universe/--preset`. The ledger row shape is a forward contract for the later
  scoring epic. A new registry metric, `market_cap`, was added; no existing metric's inputs changed.

## Deferred
- X1: live index-constituent fetch and a monthly refresh job (formats unverified, terms and rate limits).
- X2: R2 alert when the price enters a watch zone (ST-9.5, ST-11.4).
- X3: external market-cap source (rest of #72) and Alpaca lot dates and NPS flows.
- X4: ledger scoring (ST-12.2), the scorecard, the memory server and ledger-based annual turnover.
- X5: slash-command prompt files and the report templates (ST-10.2, ST-10.3).
- X6: bundled Smallcap 250 and Russell 1000 lists.
- X7: replay registration of the `universe` and `shortlist` engines (#79).
- X8: a live check of `/ideas` with a real SDK: verdict quality, cost per run, deep-tier behaviour.
- X9: owner sign-off of the defaults (floors, 35-day staleness, shortlist 8 and max 12, sector cap 2, held label, preset thresholds, the qualifying verdicts, record-all-verdicts).
- X10: India operating-income semantics (profit before tax) make `roce` and `roic` unavailable for Indian names; presets use `roe` until the statement items exist.
