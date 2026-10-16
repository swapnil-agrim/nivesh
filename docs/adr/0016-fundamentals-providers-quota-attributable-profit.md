# ADR-0016: Pluggable India fundamentals providers, a vendor call ledger, attributable profit (#131)

Status: accepted. This extends ADR-0015; it does not replace it. Tapetide stays the primary source
of Indian statements, keyed on the NSE symbol, but three things change: a vendor is now one
module behind a small interface (D1, D2), the vendor's plan limits are owner configuration enforced
by a local call ledger that survives between runs (D3 to D7), and net income is turned into profit
attributable to the parent's shareholders when `fa` reads it (D10, D11). It also adds the
cross-provider read and `nivesh fundamentals compare` (D8), a Upstox slot that is disabled and whose
every wire format is UNVERIFIED (D9), a reusable contract suite (D12) and the #135 and #136
follow-ups of ADR-0015 (D13). It adds **one migration (0010, two SQLite tables)**, two command
groups (`quota`, `fundamentals`) and one option (`fa --cross-check`), no MCP tool and no dependency.
The new secret name is `UPSTOX_ANALYTICS_TOKEN`; a value is never in a file, a fixture, a cache
row, a ledger row, a log or a message. **This goal's verification made no live vendor call:** every
test uses fixtures and `httpx.MockTransport`, and nothing here was observed against Tapetide,
Upstox, NSE, BSE or AMFI (see the end of this record). The numbers are the owner's own information,
not investment advice.

## Context
- ADR-0015 hard-coded the free plan (50 calls a day, 1,000 a month) into the adapter's messages and
  counted calls per run only, so a second process could not know what the first had spent, and the
  vendor sends no remaining-quota header. On 2026-10-15 the owner moved to the Starter plan (monthly,
  400 calls a day, 5,000 a month) and wants the door open for Upstox (a read-only Analytics Token;
  no account yet) and other vendors later.
- The India branch of `ingest_fundamentals` was an `if Tapetide else BSE` written for two vendors.
  The issue asks for the opposite shape: "a statements/shareholding/actions provider is added by
  implementing one interface and registering it" (the issue cites NFR-18). The NFR text itself is
  not in the repo (`rg NFR-18` finds nothing outside this goal's own files) and was not in the
  issue, so this ADR works from the issue's sentence alone.
- Tapetide's Net Profit includes minority interest (ADR-0015 D11): `fa` showed RELIANCE ROE 10.96%
  against about 9.25% on the parent's profit, and HDFCBANK 14.30% against 13.72%. ADR-0015 left a
  heuristic note at ingest for it (#135) and deferred the real fix (its X1).

## Decisions
- **D1 Provider seam and registry.** `nivesh_adapters/providers.py` defines `FundamentalsProvider`
  (an ABC with a class-level `ProviderInfo` and `collect(ctx) -> ProviderResult`), the capability
  matrix `Capability` (statements, shareholding, pledge, actions, estimates, bank_metrics, news) and
  the identifier a vendor keys on (NSE symbol, BSE code or ISIN). The declaration is code, not
  config, and the contract suite (D12) checks that what a provider declares is what its harness
  produces. A provider receives callables (`CollectContext`: the run's cached fetch, the shared
  failure list, the quota budget) and returns rows; it never writes a store and never imports the
  ingest, so there is no cycle. **One registration mechanism:** `BUILTIN` maps a name to
  `module:Class` and the class is imported lazily on first use, so a new provider is one module plus
  one `BUILTIN` line; a plain `REGISTRY` dict of classes added at run time wins over it, which is how
  the contract suite registers `FakeProvider` with no production edit. There is no decorator. Roles
  are `primary` and `fallback`: every primary runs in config order; a fallback runs only when no
  earlier provider produced statements (today's rule for the BSE adapter), and each provider writes
  in its own transaction. An enabled name nobody registered is a `ConfigError` that lists the
  registered names, raised when the provider is resolved (ingest or read), not at import; a disabled
  unregistered name is ignored. `NotConfigured` (a `SourceUnavailable`) says the owner must set
  something up (no token, no ISIN, no plan) and becomes one failed line while the loop goes on. The
  matrix names `actions`, `estimates` and `news` because the issue lists them, but `ProviderResult`
  has a slot only for statements, shareholding (with pledge) and bank metrics; a provider declaring
  the others extends the harness probe.
- **D2 Config block `fundamentals.providers`.** An ordered list in `config/nivesh.yaml`, validated by
  the existing Pydantic settings (`extra="forbid"`): `name`, `enabled`, `role`, optional `plan`
  (`daily`, `monthly`, `min_interval_s`, `month_start_day` 1 to 28, `reserve_pct`) and `credential`,
  which must be a `ref:NAME` reference (the key is treated like the other secret-ish keys, and the
  error never echoes a value). Order is priority. The defaults equal today: `tapetide` (primary,
  daily 400, monthly 5,000, `ref:TAPETIDE_TOKEN`), `bse_xbrl` (fallback, no plan) and `upstox`
  (disabled, `ref:UPSTOX_ANALYTICS_TOKEN`). `fundamentals.compare` holds the two tolerances of D8.
  The adapter reads its secret name and pacing from the spec; a metered provider without a plan is
  a `ConfigError` and is never built.
- **D3 The ledger lives in SQLite, not DuckDB.** ADR-0002 keeps transactional state in SQLite and
  analytical data in DuckDB. DuckDB allows one writer, and an ingest holds it for its whole run, so a
  ledger there could not be read by `nivesh quota status` while an ingest runs, and could not be
  written before each request. Migration `0010_vendor_quota.sql` (forward-only and additive: two
  tables, nothing rebuilt, no backfill) adds `vendor_call` (one row per attempt, UTC ISO `called_at`,
  `day_ist` the IST date, `kind` call or manual, `calls`, `tool`, `symbol`, `priority`, `outcome`)
  and `vendor_limit` (owner overrides of the caps). Triggers make both append-only, with the one
  exception of D4. The downgrade refusal of ADR-0002 applies: a store migrated to 0010 cannot be
  opened by an older `nivesh`. Nothing takes a backup first: `init_stores` migrates on the first
  command that opens the store (including `fa`, `quota status` and `fundamentals compare`), so the
  owner's way back is a backup they take beforehand (`nivesh backup`).
- **D4 Reserve before the call, at the one place a request leaves.** In the Tapetide adapter the
  single POST sits behind `Budget.reserve`: after pacing and before `client.post`, it opens
  `BEGIN IMMEDIATE` (so two processes cannot read the same count), counts today and this month,
  refuses with `QuotaExceeded` (naming the window, used and limit, and the reset) or inserts the
  row and commits before returning. A crash after the commit over-counts by one; nothing can
  under-count. **Every attempt counts and is never refunded**: an error, a garbage reply or a
  vendor refusal keep their row, and no code path deletes or lowers one. The accounting columns of
  `vendor_call` are fixed at insert (an `UPDATE OF` trigger aborts), and `outcome` is **set-once**
  (NULL to ok, error or halted; a second change, a delete and an `INSERT OR REPLACE` all abort),
  so the ledger can answer "were failed calls billed" by comparing outcomes with the owner's
  dashboard, and ledger rows reconcile exactly with requests (`count(outcome='ok')` equals the
  billed-reply count of the run). `vendor_limit` is append-only without exception. The adapter is
  **fail-closed**: it refuses to POST, with zero requests and `NotConfigured`, unless a budget was
  attached, and checks that before it reads the secret; `ingest_fundamentals` always attaches a
  budget built from the SQLite store and the settings. A `QuotaExceeded`, or a `sqlite3.Error` from
  the reserve ("quota ledger unavailable"), halts the adapter for the rest of the run: later calls
  raise without touching the ledger or the network, and a cached copy is used if one exists.
- **D5 Windows, and which Starter facts are the owner's.** The day window is the IST calendar date
  (the vendor resets at `00:00 IST`; times are stored as UTC, `day_ist` as the IST date, so the
  boundary is exact: 18:30 UTC starts the next day). The month window is an IST month that starts
  on `month_start_day` (default 1; before that day it is still the previous month's), so a
  different billing anchor is a config change. The numbers 400 a day and 5,000 a month are the
  owner's own statement of the Starter plan (the issue), not read from the vendor: no endpoint
  reports a plan, and the config comment says so. **UNVERIFIED:** the monthly anchor (calendar month
  or subscription date) and failed-call billing. The first is why `month_start_day` exists; the
  second is why every attempt counts and the outcome is recorded.
- **D6 Caps, warning, halt, priority floors.** The hard cap of either window refuses the call; when
  both would refuse, the one with the later reset is reported. Priority classes order callers
  holdings, then watchlist, then universe: a class may use all but `reserve_pct` percent of each
  window (`reserve_pct`: holdings 0, watchlist 10, universe 30 by default). With 400 and 5,000 the
  universe class is refused at the 281st call of a day, the watchlist at the 361st and holdings at
  the 401st, and the same shares apply to the month; the refusal says how many calls are kept for
  higher-priority callers. The class of a single-symbol run is derived (held in the latest holdings
  snapshot, then watched, else universe); a bulk run does not exist yet (X4), so the floors are
  proved at ledger level. A window at 80% of its cap (`WARN_PCT`) is a warning in `quota status`
  and in the ingest report notes. **Halt, never retry:** there is no retry anywhere. When the vendor
  itself reports a halt (HTTP 429, quota wording or a `Retry-After` header) the adapter persists it
  with `Budget.exhaust`: one `kind=manual` row tops the relevant window up to its cap (the IST day
  for generic or daily wording; for monthly wording a row dated on the first day of the monthly
  window, so it fills the month and not the day), in one `BEGIN IMMEDIATE` so racing processes add
  the gap once, and it never lowers anything. The next process then refuses without spending a
  request to learn the same thing. A token rejection (401, 403) halts but does not exhaust.
  **Disclosed limit:** the top-up row is an ordinary manual row, so `quota status` shows it as
  "manual calls" with no reason, and a halt that was only a per-minute limit, or benign text that
  matched the unobserved monthly pattern, fills the whole window in the ledger until it resets
  (append-only: it cannot be taken back, only outweighed with `quota set --daily` or `--monthly`).
  This is the conservative direction; marking such rows so `status` can say why is a follow-up.
- **D7 `nivesh quota status|set`, and the shared quota.** Other clients of the same plan spend the
  same allowance and the vendor sends no remaining-quota header (ADR-0015), so the ledger can only
  undercount. `quota status [--vendor V] [--json]` prints, per vendor with a plan, used, limit and
  left for today and this month, the source of each limit (config or override), manual calls, the
  reset and any warning; it opens only SQLite, so it works while an ingest holds the market
  database. `quota set VENDOR [--daily N] [--monthly N]` appends a `vendor_limit` override (the
  latest non-null value per field wins; the config file is untouched). `--used-today N [--on DATE]`
  records the calls spent elsewhere: it writes ONE manual row for the difference so that IST day
  totals N, tops up only (when the ledger already shows N or more it writes nothing and says so),
  and refuses a future date. `--on` is an addition to the specified command: a check run from a
  scratch data directory can then be entered in the real ledger afterwards. Every output passes the
  PII scan. `nivesh status` is unchanged (X1).
- **D8 Source-aware reads and `compare`.** Stored rows already carry their `source`; the readers
  `get_statement_rows(_many)` and `get_shareholding(_many)` now take `sources`, the provider
  priority from config (`provider_sources`: enabled providers in order, then disabled registered
  ones so rows an earlier configuration wrote stay readable). Selection is in SQL, never in Python:
  DuckDB `QUALIFY dense_rank()` with a `CASE` rank built from bound parameters (source names are
  never interpolated; an unlisted source ranks last). It runs AFTER the `filed_at <= as_of` filter,
  so the read stays **point in time**: a priority-source row filed after the date never hides the
  lower-priority row that existed on it, and every `filed_at` version of the winning source is
  kept, so restatements still resolve in `latest_as_of`. Statements are chosen **per-field** (per
  period and item): an item only a lower-priority provider has still appears, which means one ratio
  can mix providers (revenue from one, net income from another); `compare` lists such figures under
  "only in". Shareholding is chosen as a **whole row** per quarter (priority, then latest
  `filed_at`), because filling a pledge from another provider's filing would mix filings;
  field-level fill is deferred (X5). `StatementRow` is unchanged, with one provider the result is
  the legacy one, the analysis loaders take `fundamentals` as a required keyword, and an AST test
  requires `sources=` at every reader call in the packages (aliased imports included). Read
  surfaces (`fa`, `valuation`, the MCP tools, `mf_report`) therefore agree on the priority
  provider's value, and reversing the config order flips it. **`nivesh fundamentals compare
  SYMBOL [--as-of D] [--json]`** is read-only over stored rows (no adapter is built, no request, no
  ledger row): it lists each provider with stored rows and its declared `revenue_basis`,
  `profit_basis` and filing-date quality, then compares every other provider against the
  highest-priority provider that holds the figure: percent difference
  `(other - base) / |base| * 100` (a zero base with a non-zero value is flagged with no percent),
  default tolerance 1% for statements and 0.5 percentage points for shareholding, plus the "only
  in" lists. Compare **never averages** and never merges a value; which figure to trust is the
  owner's call. With one provider it says "single provider with stored rows (tapetide): nothing to
  compare" and prints no disagreement list. `nivesh fa SYMBOL --cross-check` appends the same block
  after the normal output (it rejects `--json` and `--note`).
- **D9 The Upstox slot: disabled, ISIN-keyed, every shape UNVERIFIED.** `fundamentals_upstox.py`
  holds a GET-only adapter and `UpstoxProvider`, built from a summarised read of Upstox's public API
  documentation by the research phase; no account was used, nothing was browsed by the
  implementation and no call was ever made to `api.upstox.com` (in code that host appears in the
  adapter module only). The paths (income statement, balance sheet, cash flow, share holdings,
  corporate actions under `/v2/fundamentals/{isin}/`), the query parameters, the error codes, the
  units, the EPS unit, the header scheme and every response field name are **UNVERIFIED**: where the
  summary was silent the shape is a guess, and each of the six hand-written fixtures carries a
  `_comment` saying so. It is keyed on the ISIN and never guesses one (no valid ISIN raises
  `NotConfigured`); the Analytics Token is the secret `UPSTOX_ANALYTICS_TOKEN` (a `ref:` name), sent
  only in the authorization header (the scheme word is assembled in code, and the scheme itself is
  UNVERIFIED), never in the URL, a message, a `repr`, a cache payload or the ledger. It maps revenue
  and net income, total assets, `total_equity` only when both the Equity Capital and Reserves lines
  exist (Tapetide's `total_equity` is their sum), operating cash flow and the promoter percentage;
  it stores no pledge and no public percentage (`retail_and_other` is not Public). The documented
  replies carry no filing date, so every `filed_at` is an estimate from the period end (quarterly 45
  days, annual 60, shareholding 21, clamped to today; approximate constants) and `filing_dates` says
  `estimated`. The corporate-action mapper (Dividend, Bonus, Split; Rights is skipped) exists but
  ingest never requests it (X3). It is `enabled: false` in the defaults; enabling it without a token
  yields one failed line, not a crash; `UpstoxFundamentals` is in the write-verb safety scan. Treat
  the first real response as likely to need mapper edits (X6).
- **D10 Attributable profit, derived when `fa` reads it.** `nivesh_engine/attributable.py` (pure,
  Decimal) gives, for one fiscal period, the first basis that applies: (1) `reported_minority` when
  a provider stored a `minority_interest` item (net income minus it); (2) `eps_x_shares`, the EPS
  times the shares outstanding of the SAME period, judged by the gap
  `(net income - EPS x shares) / (EPS x shares) * 100` on its two-digit figure: within plus or
  minus `GAP_TOLERANCE_PCT` (3 percent, inclusive, a module constant and not config because it is a
  rule of the method) no minority is detected and net income is used
  (`consolidated_no_minority_detected`, gap recorded); above it, EPS x shares is used; below it,
  minority interest cannot explain the difference (it lowers nothing), so the consolidated figure is
  kept as `consolidated_minority_unknown` with a sign-neutral reason that names the gap and says it
  can come from weighted EPS against period-end shares, buybacks or a data mismatch; (3) missing or
  non-positive EPS or shares: the consolidated figure, `consolidated_minority_unknown`, "minority
  interest not available". The issue asks to flag a missing minority interest; the figure stays
  usable and labelled instead of dropping `fa` coverage, and the basis is never switched silently.
  The value is None only when net income itself is missing. **India only** (US `net_income` is
  EDGAR's parent line; US output is unchanged and never labelled) and, for the `eps_x_shares`
  basis, the **latest fiscal year only**, because Tapetide gives a share count for that year only
  (derived from equity capital over face value, so a later split or bonus rewrites it); EPS is
  weighted over the year and shares are the period-end count, so the figure is approximate. Used
  for `net_margin_pct` and `roe_pct` in `fa`, the bank earnings DCF base in `valuation`, and annual
  `net_margin` and `roe` in `get_ratios`; each metric's inputs carry `net_income` (the figure
  used), `net_income_consolidated`, `profit_basis`, `profit_gap_pct` and, when flagged,
  `profit_reason`; `get_ratios` gains the additive key `profit_basis` (quarterly rows have no share
  count and are labelled `consolidated_minority_unknown`). On the recorded fixtures: RELIANCE gap
  +18.55% (attributable 80,772.5 crore against 95,754), HDFCBANK +4.22% (76,011.2 against 79,219),
  ITC +1.60%, TCS +0.44% and INFY +0.30% (no minority detected); ROE RELIANCE 9.25 against 10.96 and
  HDFCBANK 13.72 against 14.30. **Stays consolidated and labelled `profit_basis="consolidated"`:**
  `net_income_cagr`, `cfo_to_pat` and `accruals_ratio_pct` (attributable exists for one year only
  and CFO is consolidated, so the ratio must not mix bases); `eps_cagr` is per share already. The
  issue also lists EPS-based growth: that needs no change. **Disclosed limits:** the ROE denominator
  is Tapetide's `total_equity` (equity capital plus reserves), and whether that includes the
  minority's equity is unverified; if it does, attributable profit over it understates ROE (the
  vendor's own snapshot ROE for RELIANCE is 8.91, the derived figures here are 9.25 and 10.96). For
  RELIANCE the free-cash-flow DCF branch stays consolidated (its inputs say `scope="consolidated"`), so it
  overstates the parent's value by the minority's share, and cross-section scores (`screen`,
  `score`) mix profit bases for the names that fall back to `consolidated_minority_unknown`: each
  metric carries its label, but a rank does not adjust for it. In place of the EPS-times-shares
  ingest note of ADR-0015 (retired, #135), ingest computes no gap and says only, for every
  Tapetide run, the declared `profit basis:` of the provider.
- **D11 Revenue basis is a declared provider label.** `ProviderInfo.revenue_basis` states how the
  vendor defines revenue; for Tapetide "vendor net sales; net of excise and indirect taxes where the
  company reports it so (for example ITC)". The first enabled statements provider's label reaches
  `fa` as `SecurityInputs.revenue_basis` and is stamped into the inputs of `net_margin_pct` and
  `revenue_cagr_*` for India (US untouched), and `compare` prints it. ITC's Tapetide revenue is
  78,868 crore against 89,913 in the filing (ADR-0015: net of excise): the figure is not corrected,
  the basis is recorded. A per-symbol override is deferred (X5).
- **D12 One contract suite for every provider, and a fake.** The suite in
  `tests/adapters/test_provider_contract.py` runs six parametrized checks over every harness in
  `tests/provider_fx.py` (Tapetide, BSE XBRL, the fake provider and the Upstox skeleton): point in
  time (`period_end <= filed_at <= today`), `as_of` and timezone-aware fetch times, unit scaling
  (crore, lakh, per-share, share counts, percentages in 0 to 100), unavailable with a reason and
  never a zero, no secret in any error, note, skipped line, `repr` or cached payload across hostile
  replies (401, an echoed credential, a malformed body, a quota text), and no network without the
  mock. `test_every_registered_provider_has_a_harness` runs over `BUILTIN`, so a new provider
  cannot skip the suite, and declared capabilities must match what the harness produces.
  `FakeProvider` is registered by a fixture and goes through `ingest_fundamentals` end to end (rows
  land with source `fake`) with no production edit. Tests that resolve a credential request
  `fake_keyring`, so the owner's keychain is never read.
- **D13 Hardening (#136), the retired note (#135), plan-aware wording.** One `_real` helper (not a
  bool, a finite number; an int is always real) replaces three copies, and a huge int is a Decimal
  instead of an `OverflowError`. An unparsable promoter pledge percentage is stored as unknown, the
  promoter and public rows are kept, and one skipped line "promoter pledge percentage is not a
  number" is printed by `market fundamentals` (at most five skipped lines, then "and M more");
  a text pledge with status "none" stays 0 (pinned). An **unknown-symbol short-circuit**: a
  "Company not found" or "Symbol not found" reply stops the remaining sections, so a bad symbol
  costs one call, not four, while "Section not found" for a valid company does not. The
  hard-coded free-plan text is gone: every message and the report line come from the configured
  plan (`Budget.describe()`, for example "daily 400, monthly 5,000; daily resets 00:00 IST,
  monthly resets on day 1 (IST)"), and `market fundamentals` prints `Tapetide calls this run: 6 (6
  attempted; today 6/400, month 6/5,000, daily resets 00:00 IST)`. **The quota regex gained a
  defensive monthly alternative (`monthly (limit|quota|cap)`, `per month`); that wording is not
  observed vendor text:** the vendor's monthly-cap text, its shareholding not-found text, an auth
  error returned at HTTP 200 and whether failed calls are billed were never recorded, because no
  live call was made. The local ledger is the real monthly guard; the regex only stops further spend
  when a reply happens to say so, and it may halt a run on benign text (the cost of being
  defensive). Nothing invented stands in for the missing wording.

## Consequences
- With the defaults, behaviour is unchanged except for the ledger: a cold company costs 6 billed
  calls and 6 ledger rows, a cached re-run costs none, and a quota refusal sends nothing. Nothing
  can reach `api.upstox.com` unless the owner enables `upstox` and sets its token.
- Schema is now 10 and forward-only. The ledger counts this machine's attempts only; calls made
  elsewhere on the same plan must be entered by hand (D7), and the owner should compare the ledger
  with the vendor dashboard, which also settles failed-call billing and the monthly anchor.
- Two of the five recorded names change value by design (RELIANCE and HDFCBANK: net margin, ROE
  and, for the bank, the DCF base); ITC, TCS and INFY keep their value and are now labelled.
  Anything quoting an earlier `fa` for an Indian group with a large minority should be re-read.
- A second provider enabled alongside Tapetide runs as a primary and its rows are read by
  priority; because selection is per field, one ratio can combine two providers until X5 narrows it.
- Follow-ups outside this goal and not built: `ingest_estimates` still uses the UTC date (US only);
  a Tapetide `ratios` section might carry a reported minority line (the helper already prefers a
  stored `minority_interest` item).

## Deferred
- X1: a read-only `quota` line on `nivesh status` (the command is pinned "writes nothing"). Tracked in #149.
- X2: the India `get_estimates` reason derived from the capability matrix (the MCP text still says
  no free estimates source). Tracked in #149.
- X3: wiring Upstox corporate actions into price ingest and `corp_action` (the mapper exists, ingest
  never calls it). Waits for X6; not filed separately.
- X4: a bulk fundamentals ingest (priority classes are proved at ledger level; only single-symbol
  runs exist). Tracked in #147.
- X5: per-symbol revenue-basis overrides, and field-level shareholding fill across providers. Tracked in #146.
- X6: verifying every Upstox shape (and the header scheme, units, EPS unit and quarterly
  parameters) once the owner has an account, and capturing, when observed, the Starter monthly-cap
  wording, the shareholding not-found wording, the HTTP-200 auth error shape, failed-call billing
  and the monthly anchor. Tracked in #143 (owner-run, at most 8 calls).

## Notes
- No live vendor call was made while building or verifying this goal, so this record has no
  observed-behaviour section: Tapetide's Starter numbers are the owner's statement, and the wording
  and billing facts named above remain open until someone observes them.
- Adding a provider: `docs/runbooks/add-a-provider.md`.
- The tests that pinned the schema at version 9 were relaxed to "at least 9" (three places), and the
  two security tests that reject a secret-looking key now allow `credential: ref:NAME` (a reference,
  checked case-sensitively), so a literal after `credential` is still caught.
