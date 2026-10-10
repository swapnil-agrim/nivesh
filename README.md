# Nivesh

Agentic investment research agent (India + US), read-only. (Repository formerly named bull-pulse.)

## Architecture
- `nivesh_core`: typed config and profile, secrets, SQLite and DuckDB stores, migrations.
- `nivesh_engine`: deterministic analytics: holdings consolidation (`consolidate.py`), trading calendars, corporate-action adjustment, statements and ratios, macro maths, news de-duplication and classification (E4). E6 adds the analysis engines (`dmath.py`, `ta.py`, `levels.py`, `regime.py`, `setups.py`, `fa.py`, `valuation.py`, `redflags.py`, `xray.py`, `risk.py`, `metrics.py`, `screen.py`, `scoring.py`), fed by the read-only loader `nivesh_adapters/analysis_data.py`. E5 adds the mutual-fund engines (`mf_returns.py`, `mf_valuation.py`, `mf_overlap.py`, `mf_cost.py`, `mf_lots.py`, `fund_doctor.py`, `fund_screen.py`).
- `nivesh_adapters`: data-source adapters (read-only InvestRight client and daily login, CAS parser boundary on `casparser`, CSV import; E3: US CSV import, read-only Alpaca client and allow-list broker proxy; E4: security-master sources, NSE/BSE/Yahoo/Stooq prices, SEC EDGAR, India XBRL fundamentals, FRED and NSE flows, FMP estimates, news feeds), quality validators, record/replay fixtures, TTL cache, and `market_ingest.py` (the only module that writes market tables).
- `nivesh_mcp`: read-only FastMCP server framework and server registry (`demo`, `holdings`, and the E4 data servers `market`, `fundamentals`, `filings`, `news`, `macro`; 20 tools, shared helpers in `common.py`).
- `nivesh_agents`: headless Agent SDK runtime (same agents as the Claude Code `.claude/` config).
- `nivesh_cli`: the `nivesh` command (`init`, `mcp list`, `run`, `status`, `replay`, `secrets`, `pii-scan`, `egress-check`, `backup`, `restore`, `login`, `sync`, `sync-us`, `ingest`, `import-csv`, `master build`, `market prices|fundamentals|macro|estimates|news`, `mf nav|meta|holdings|returns|overlap|doctor|discover`, and the E6 analysis commands `ta|fa|valuation|flags|xray|risk|screen|score`).

Supporting dirs: `config/`, `schemas/`, `prompts/`, `templates/` (CSV template), `docs/adr/`, `docs/runbooks/`, `tests/fixtures/`, `.claude/`.

## Setup
```
make setup   # uv sync (creates .venv with Python 3.12)
make check   # ruff, mypy, pytest with coverage gate
```
`make check` is also exactly what CI runs. The coverage gate (85% branch) covers `nivesh_engine` and
`nivesh_adapters`.

Market data (E4) needs three secrets, set with `nivesh secrets set NAME` (or env vars): `EDGAR_CONTACT`
(your contact for the SEC User-Agent; personal data, never put it in config), `FRED_API_KEY` and
`FMP_API_KEY` (free-tier keys; without them the macro and estimates features report the gap instead
of failing). Then `nivesh master build`, and for example `nivesh market prices RELIANCE --start
2026-01-01 --end 2026-01-31`. Complete `market.nse_holidays` in `config/nivesh.yaml` from the NSE
circular: a year with no entry fails loudly instead of guessing.

## CI and merge blocking
CI (`.github/workflows/ci.yml`) must pass before merge. Branch protection requiring the `ci`
check is a repository setting and must be enabled manually. macOS runners cost more; that is also
a manual decision.

## Security & ops
- Secrets: `nivesh secrets set NAME` (hidden prompt, OS keychain) and `secrets check`; in Docker/VM inject env vars (see `docs/deploy/vm.md`). Config only holds `ref:NAME`. Any session-token file must be written with `nivesh_core.paths.write_private` (0600 from birth).
- Redaction: every MCP tool output, recorder fixture, cache payload, trace and error is redacted; `nivesh pii-scan PATH` and the nightly `tests/security` job check for leaks. Contract note: tool envelopes now contain `[REDACTED]` where PII was. The blanket 9-18 digit sweep is kept, so an unlabelled long number in free text is masked (recall over fidelity, ADR-0003).
- Tracing/cost: `nivesh run [--tier brief|quick|deep] [--force]` records a run row and `runs/<id>/trace.jsonl`; `nivesh status` shows month-to-date vs the cap; `nivesh replay <id>` re-checks recorded engine calls. Budget rule: >=80% downgrades `deep` (override with `--force`), >=100% refuses `deep` even with `--force`.
- Egress: `nivesh egress-check` compares the public IP with `registered_ip`; `run` warns but proceeds.
- Backups: `nivesh backup` / `nivesh restore` (age-encrypted); host cron line in `docs/deploy/vm.md`.
- Injection: adapters and agents must pass all external text through `nivesh_agents.untrusted.wrap_untrusted`.
- Holdings (E2): `nivesh login` (daily InvestRight login, `--paste` for a pasted URL or token), `nivesh sync [--ltp]`, `nivesh ingest` (CAS PDFs from `<data_dir>/inbox`, password and folio salt from the keychain), `nivesh import-csv PATH [--preset zerodha|groww|upstox --label NAME]`. The `holdings` MCP server exposes seven read-only tools: `session_status`, `get_holdings`, `get_positions`, `get_funds`, `combined_portfolio`, `get_transactions`, `read_cas_statement`. Runbook: `docs/runbooks/investright.md`. PII (names, PAN, e-mail, mobile, address, DP/client IDs, folios) never leaves `nivesh_adapters/cas.py`; folio and demat identifiers are stored only as a salted `holder_ref`.
- Market data (E4): the five data servers are read-only; DuckDB allows one writer or many readers, so an ingest run blocks the MCP data tools for its duration (they answer "ingest in progress, retry"), and a running reader makes `nivesh backup` ask for a retry. External text (news, filing sections, announcements) is returned only inside untrusted-data blocks. EDGAR identifiers (CIK, accession) are never emitted by tools: filings are cited by local `filing_id`. Sources are free; Yahoo's chart endpoint is unofficial and may break or throttle.
- US holdings (E3): `nivesh import-csv PATH --market us [--preset alpaca|robinhood --label NAME]` (template `templates/holdings_us.csv`: symbol, exchange NYSE/NASDAQ/ARCA, quantity, average cost in USD, optional lot date) and `nivesh sync-us` (read-only Alpaca positions and account; secrets `ALPACA_KEY` and `ALPACA_SECRET` via `nivesh secrets set`, config block `us_broker` holds `ref:` names only). USD holdings flow into the same holding schema and consolidation as India, with the source labelled (`alpaca`, `us_csv`). INR is derived at read time as `value_usd x USDINR` for the valuation date; the rate, its date and its source are reported, INR vs USD exposure is reported, and a missing rate is "unavailable", never zero. The `holdings` server now has eight read-only tools; the new `get_lots` shows lots, holding days and XIRR in USD and INR terms. The optional config block `tax.us_long_term_days` only drives an informational flag; Nivesh gives no tax advice. `nivesh_adapters/broker_proxy.py` is a library allow-list proxy for a broker MCP that bundles trade tools. USDINR needs `nivesh market macro` to have run (FRED series `DEXINUS`); the RBI reference rate is not wired. Decisions: `docs/adr/0006-us-holdings.md`.
- Mutual funds (E5): `nivesh mf nav CODE... [--refresh --cross-check]` (MFapi.in primary, AMFI NAVAll fallback, incremental, flags a gap of more than `mf.nav_gap_days` missing weekdays), `mf meta CODE...` (category, plan, option, TER, AUM in crore, benchmark, manager; for a regular plan the direct twin and the TER cost in percent and rupees per year), `mf holdings CODE...` (monthly ISIN weights, mapped, unmapped and other weight per month, shortfall below 12 months), `mf returns CODE [--benchmark SYMBOL]` (rolling returns, beat percent, median excess, standard deviation, max drawdown, downside capture, Sortino, portfolio P/E and P/B against the fund's own history), `mf overlap` (pairwise overlap and look-through exposure by stock and sector, top 20), `mf doctor [CODE]` (KEEP, SWITCH_TO_DIRECT, REPLACE, CONSOLIDATE or REVIEW with reasons citing metrics, exit-load and tax-impact blocks before any switch or replace) and `mf discover --category C [--max-ter --min-aum --min-tenure]` (up to five ranked direct-plan candidates from the schemes you have loaded). All numbers come from deterministic Decimal code, never a model; a missing TER, twin, benchmark, history, NAV, exit-load row or tax rate is "unavailable" with a reason, never zero. Benchmarks are price indices, not total-return indices (excess is overstated by the dividend yield), mapped from the fund's benchmark text through `mf.benchmarks`. Thresholds, the exit-load table, tax parameters and screen weights are owner-set in the `mf` config block (example values only; Nivesh gives no tax or investment advice). The holdings source key, if any, is `MFDATA_API_KEY` via `nivesh secrets set`; config holds `ref:` only. A trailing one-year return is shown but is never a reason for an action (BR-12). There is no MF MCP server: the surface is the CLI (E5 D2). Issue #53 (XIRR bracket) is untouched; no MF module calls `xirr`. Decisions: `docs/adr/0007-mutual-fund-intelligence.md`.
- Analysis engines (E6): `nivesh ta SYMBOL` (SMA, EMA, RSI, MACD, ROC, ATR, volatility, Bollinger width, volume, relative strength, support and resistance, setup), `fa SYMBOL [--as-of DATE]` (growth, profitability, balance sheet and cash quality from filings made on or before the date), `valuation SYMBOL` (multiples with 5y and 10y median, percentile, peer median, reverse DCF and a base/bull/bear range), `flags SYMBOL` (red flags with status, severity and evidence), `xray` (allocation, drift, concentration, limits, look-through and XIRR with its coverage), `risk [--candidate SYMBOL --weight PCT]` (volatility, drawdown, beta, days to trade, pro-forma weights), `screen --rules FILE.yaml [--security SYMBOL]` and `score [SYMBOL...] [--horizon long_term|positional]`. Every command takes `--as-of` and `--json` (exact decimal strings), opens both stores read-only and fetches nothing. All numbers come from deterministic Decimal code with no new dependency; a metric that cannot be computed is "unavailable" with a reason, never zero, and a red flag without data is `not_evaluable`, never clear. The `analysis` block of `config/nivesh.yaml` holds every owner-set window, threshold, scenario and factor weight (example values equal the defaults; the PID's `methodology.yaml` is this block); each score carries `weights_version` and `weights_digest`. The screen and the score use an explicit universe (the securities you name, else every direct security with stored bars) until ST-9.1 exists, and say so. BR-12: a security whose strength is trailing price alone cannot reach the top band (momentum credit is capped by the quality, value and growth support) and a hard red flag caps the band at hold. There is no `nivesh-engine` MCP server (E6 D1): the surface is the CLI. Also fixed: #53 (XIRR bracket, so a +100% in 30 days gain solves) and #54 (lot coverage is `exact`, `partial` or `over`; an over-covered security gets no XIRR). Related: #46, #39, #52, #33. Decisions: `docs/adr/0008-analysis-engines.md`.
- Decisions: `docs/adr/0003-trace-redaction-cost.md`, `docs/adr/0004-holdings-ingestion.md`, `docs/adr/0005-india-fundamentals-source.md`, `docs/adr/0006-us-holdings.md`, `docs/adr/0007-mutual-fund-intelligence.md`, `docs/adr/0008-analysis-engines.md`.

## Deferred
- E13 F1: numeric replay over real engines (`REPLAYABLE` is empty until E6).
- E13 F2: schema-constrained verdict output and the red-team injection eval (needs ST-7.1, ST-12.6).
- E13 F3: scheduler service in compose and the nightly backup job (E11); only the command and a host cron line exist.
- E13 F4 (remainder): registration steps and the SSH tunnel are documented (`docs/runbooks/investright.md`, `docs/deploy/vm.md`) but unverified against the live HDFC portal; see E2 D2 and D5 below.
- E13 F5: restore reproducing "the last report" (reports are E10); run dirs and stores are restored.
- E13 F6: paid-data cost recording and delivery of cost warnings (stderr and `status` only).
- E13 F7: resolved by E2; `InvestRightClient` is covered by the introspection test and by a discovery lock-in test.
- E13 review notes: `replay` redacted-data comparison is dormant while `REPLAYABLE` is empty and must be fixed before E6 engines land; a crash before any result message records 0 cost; the budget check is not concurrency-safe; backup archive extraction has no size limit; `replay` compares redacted data (a difference hidden by redaction is not detected); the monthly budget is checked only at run start (a long run can overshoot); field-name redaction is blunt (any key containing `key`, `client`, etc. is masked).
- E13 F8: live verification of `docker compose up`, a VM, a Linux keychain, and real `age` in CI (the real-age test skips when the binary is absent; `docker build` reached `uv sync` locally but the network timed out).
- ST-1.1 AC3, move of the seed `investright-mcp`: the seed is absent from this repo, so E2 wrote
  `nivesh_adapters/investright*.py` and `nivesh_mcp/holdings.py` from the spec (wire details unverified, D2).
- E2 deferred (each gets a follow-up issue):
  - D1: resolved by E4 (`nivesh master build`, `SecurityMaster`); nightly scheduling of it is E4 D7.
  - D2: live HDFC/InvestRight verification (login URL, request-token parameter, response and row field names, 401/403 versus code 60014 wording, User-Agent need, static-IP behaviour, LTP request body).
  - D3: round trip of real CDSL/NSDL/CAMS/KFintech CAS PDFs through casparser (fixtures are models built in code; one `live` test accepts an owner-supplied PDF).
  - D4: Zerodha/Groww/Upstox preset layouts verified against real exports.
  - D5: daily login on the VM over an SSH tunnel and a real-socket test of the loopback callback listener.
  - D6: non-INR CSV rows, US holdings and FX (E3).
  - D7: a holdings TTL in cache config (live holdings bypass the cache).
  - D8: NPS and other non-listed asset types in NSDL CAS; XIRR and tax lots from stored transactions.
  - D9: overlap mapping for several InvestRight-linked demats (`investright.demat_ref` handles one).
- ST-1.3 AC1, full PID section 13 schema: core, holdings and E4 market tables (prices, corporate
  actions, fundamentals, shareholding, estimates, macro, filings, news, calendar events, security
  aliases) exist; later epics add the rest as migrations (see ADR-0002).
- E4 deferred (each gets a follow-up issue):
  - D1: ST-4.1 live sampling of 30 companies (5 banks/NBFCs) per candidate, the 10-company manual annual-report check, the paid trial, and confirming or revising the provisional ADR-0005 choice.
  - D2: live verification of every wire format (NSE/BSE bhavcopy and index files, corporate-action JSON, Yahoo chart, Stooq, EDGAR, FRED, NSE FII/DII, BSE/NSE announcements, FMP) and `live` record tests; all fixtures are hand-built from documentation and unverified.
  - D3: ST-4.4 depth targets verified only on synthetic fixtures; Ind-AS XBRL tag mapping for bank fields (GNPA, NNPA, NIM, CASA, CAR) and shareholding/pledge unverified.
  - D4: model-backed news classifier (the `Classifier` Protocol and rule-based default exist) and the web-search news source.
  - D5: keyed estimate vendors beyond one FMP-shaped adapter (Intrinio), and an India estimates source (India is reported `available: false`).
  - D6: optional OpenBB price provider (Yahoo plus Stooq satisfy a configurable second provider).
  - D7: nightly scheduling of master build and ingest (E11); InvestRight master CSV and OpenFIGI inputs; PDF text of announcement attachments.
  - D8: complete and verify the NSE holiday list (lunar holidays, Muhurat sessions); RBI DBIE adapter (policy repo; India CPI and 10y use FRED mirror ids).
  - D9: EDGAR accession numbers and long numeric news-URL ids cannot pass MCP redaction as strings (tools cite `filing_id`); needs a redaction allow-list design.
  - D10: peer ranking by market cap and a ratio set beyond margins, ROE, leverage and growth (`get_peers` returns same-industry securities only, possibly none).
- E3 deferred (each gets a follow-up issue). **AC3 (ST-3.3) is met on the FRED leg only; the RBI reference-rate source is deferred (D1), so ST-3.3 is not fully done.**
  - D1: an RBI reference-rate source for USDINR (FRED DEXINUS stays the fallback); shipped output labels the source "RBI reference rate not wired".
  - D2: a runnable stdio allow-list proxy MCP server wrapping a real broker MCP (registry, `.mcp.json`, settings), and any Robinhood connector; the proxy class and its tool-safety coverage ship.
  - D3: live verification of Alpaca response fields, Robinhood and Alpaca export headers and SEC exchange labels for ARCA and AMEX (all fixtures are hand-built and unverified).
  - D4: Alpaca lot dates (needs account activities); Alpaca rows report "no lot dates" and no XIRR.
  - D5: portfolio X-ray report and richer exposure (sector, geography); an agent-exposed live Alpaca positions tool.
  - D6: historical-rate INR P&L (FX gain on cost), non-USD currencies, split-adjusted lot quantities, any tax computation.
  - D7: none (INR-terms XIRR and `nivesh sync-us` are delivered; this slot is reserved for a scope trim).
- E5 deferred (each gets a follow-up issue); wire formats of MFapi.in, AMFI NAVAll, the holdings source and the metadata source are per spec and hand-built fixtures are unverified:
  - D1: ST-5.6 agent narrative and the `/review-funds` command (depends on ST-7.5); the deterministic doctor with reason codes ships.
  - D2: MCP exposure of the MF engine (`engine.mf_*`, a `nivesh-mf` server); the CLI ships.
  - D3: live verification of the NAV, holdings and metadata sources (`live` record-mode tests).
  - D4: category-relative metrics (category median returns and TER) and TRI benchmark series.
  - D5: none (nothing trimmed for scope).
  - D6: exit-load scraping and detailed India MF tax-slab rules; owner-set tables and gain and days always shown.
  - D7: style and market-cap drift metrics and manager-change detection beyond `manager_since`.
  - D8: whole-universe NAV storage, IDCW-adjusted return series and fund-of-fund look-through recursion; only held and candidate schemes are stored.
- E6 deferred (each gets a follow-up issue); the India statement tags and the 8-K item layout are per spec and unverified, and no TA-Lib reference series exists:
  - D1: the `nivesh-engine` MCP server (nine tools of PID 14.4), its registry, allow-list and contract/security review; the CLI ships.
  - D2: India statement items for EBITDA, D&A, interest, cash, current assets and liabilities, receivables, total assets, CFO and capex (refs #39, #46); until then those metrics are unavailable for India.
  - D3: recorded TA-Lib/pandas-ta reference fixtures and MACD seeding parity (needs a network-enabled session).
  - D4: a market-cap source for India holdings, ETFs and funds in the X-ray.
  - D5: sector-index mapping and a Nifty 500 benchmark for relative strength against a sector.
  - D6: the investable universe (ST-9.1) replaces the explicit id list for screen, breadth and RS rank.
  - D7: data sources for the not_evaluable flags (India auditor change, contingent liabilities, related-party, receivables).
  - D8: INR/USD sensitivity, time-weighted return against benchmarks, tax-aware lot flags.
  - D9: Alpaca lot dates (#52) and NPS XIRR (#33).
  - D10: ST-9.2 screener presets as YAML packs on the metric registry.
  - D11: the delivery percentage metric (India).
  - D12: deeper India filing history for a 10-year valuation median.
  - D13: owner sign-off of the red-flag severities, thresholds, factor weights and band thresholds; reconcile the PID `methodology.yaml` name with the `analysis` block.
- SBOM generation: deferred; weekly `pip-audit` rides the nightly workflow.
