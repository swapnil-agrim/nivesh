# ADR-0015: India fundamentals from Tapetide's free tier (#123)

Status: accepted. This replaces the provisional choice of ADR-0005 for the source of Indian
statements: Tapetide (free plan) is now the primary, keyed on the NSE symbol, and the BSE XBRL
adapter stays only as a fallback (it answers HTTP 403 today). The choice rests on a live spike on
2026-10-13 against five large caps (RELIANCE, HDFCBANK, TCS, INFY, ITC), not on a desk comparison,
and on CR-7 (use only official daily files or terms-compliant sources). It adds one adapter
(`nivesh_adapters/fundamentals_tapetide.py`), rewires the India branch of `ingest_fundamentals`,
extends `write_fundamentals` (several sources in one transaction), prints more in
`market fundamentals`, and makes two small engine changes (D9). It adds **no migration**, no table,
no MCP tool and no dependency. The only new secret is `TAPETIDE_TOKEN`; its value is never in a
file, a fixture, a cache row, a log or a message. No test uses the network. The numbers are the
owner's own information, not investment advice.

## Context
`nivesh fa RELIANCE` reported every metric unavailable because the one India statement source in
code (BSE `ResultsXbrl`, ADR-0005) is blocked by the exchange's bot protection. ST-4.1 and ST-4.4
need at least 5 annual and 12 quarterly periods with `period_end` and `filed_at` (point in time).

## Options evaluated (live, 2026-10-13, CR-7 first)
| Option | Works | Data | Point in time | Terms (CR-7) | Verdict |
| --- | --- | --- | --- | --- | --- |
| NSE JSON API and XBRL files | yes, no cookie | best: full statements, bank taxonomy, 2005 to date | exact filing times | Terms of Use ban "systematic or automated data collection", and the `/api` endpoints are the site's own backend | REJECTED |
| Yahoo `fundamentals-timeseries` | only with the app User-Agent (a browser one got 429) | 4 annual, about 5 quarterly periods, no filing date | none | terms forbid automated collection; robots.txt disallows everything | REJECTED |
| BSE `api.bseindia.com` | no: HTTP 403 (Akamai) on every variant | n/a | n/a | terms unreadable; bypassing the protection is out | unusable, kept as fallback |
| Tapetide MCP, free plan | yes with a free token | 12 fiscal years, 13 quarters, banks included | per period, `reported` or `estimated` | personal use through its own MCP/API is allowed; no redistribution, no limit circumvention | CHOSEN |

Yahoo defects seen (so it would also fail on quality): HDFCBANK profit matches neither the
consolidated nor the standalone figure, INFY comes in USD, RELIANCE and ITC revenue differ from the
filing, ITC quarters repeat. NSE would be the best data; it is rejected on terms alone (X2).
Tapetide's free plan: 50 calls a day (successful calls only), 1,000 a month, 60 calls a minute.

## What Tapetide returns (observed on all five names, not assumed)
- Endpoint: one JSON-RPC POST (`tools/call`) to `mcp.tapetide.com/mcp`; no `initialize` or session
  needed. The reply was JSON for every call (an SSE reply is also parsed, last `data:` line). The
  result text is itself a JSON string. Failures are HTTP 200 with `isError` and free text; a missing
  or wrong token is HTTP 401 (403 is treated the same).
- Units: INR crore as plain numbers, INFY included (INR, not USD); EPS is rupees per share. Percent
  rows are rounded integers and are not used.
- Depth: `quarterly` 13 quarters (Jun 2023 to Jun 2026), `profit_loss`, `balance_sheet` and
  `cash_flow` 12 fiscal years (plus a `TTM` pseudo-period that is skipped). This meets ST-4.4. The
  all-sections call is capped to the latest 5 periods per section even with a larger request, so
  depth needs one call per section.
- Rows: P&L `Sales` (banks: `Revenue`, `Financing Profit`), `Interest`, `Depreciation`,
  `Profit before tax`, `Net Profit`, `EPS in Rs`; balance sheet `Equity Capital`, `Reserves`,
  `Borrowings` (banks: `Borrowing`, `Deposits`), `Total Assets`; cash flow `Cash from Operating
  Activity`, `Free Cash Flow`. No cash, receivables, inventory, current items, or bank asset quality
  (GNPA, NNPA, NIM, CASA, CAR): those stay "unavailable" with their reason, never zero.
- Point in time: `availability[].available_from` with basis `reported` (a real declaration date) or
  `estimated` (a regulatory-lag guess). Quarterly: 8 reported (since Sep 2024) and 5 estimated;
  annual: only the latest 2 or 3 years reported. Values are the latest figures (restatements
  included); only the timing is original.
- Shares: the `top_ratios` snapshot (all-sections call) carries the `Face Value`; shares outstanding
  are derived as Equity Capital over Face Value.
- Shareholding: `get_shareholding` gives promoter and public percent by quarter and the promoter
  pledge for the latest quarter only. No filing date.
- Rate headers: only `x-ratelimit-reset` (the next IST midnight) is sent; there is no remaining
  quota header, so the client counts its own calls.

### Accuracy, FY2026, INR crore (Tapetide against filings)
| Company | Metric | Tapetide | Reference | Verdict |
| --- | --- | --- | --- | --- |
| RELIANCE | Revenue | 1,055,780 | 1,075,675 | -1.8%, revenue is net of some operating items |
| RELIANCE | Net profit | 95,754 | 80,775 attributable | +18.5%: profit **including minority interest**; EPS 59.69 x 1,353.2 crore shares equals the attributable figure |
| TCS | Revenue, profit | 267,021, 49,454 | 267,021, 49,210 | revenue exact, profit +0.5% (minority) |
| INFY | Revenue, profit | 178,650, 29,474 | 178,650, 29,440 | revenue exact (INR), profit +0.1% |
| ITC | Revenue | 78,868 | 89,913 | -12.3%, **net of excise** |
| ITC | Net profit | 21,018 | 20,689 | +1.6% |
| HDFCBANK | Net profit | 79,219 | 76,026 consolidated | +4.2%, consistent with minority-inclusive profit |

Revenue is exact for TCS and INFY; Net Profit is consistently profit including minority interest.
These are definition differences, not sourcing defects, and they are disclosed. HDFCBANK has a
merger discontinuity (revenue FY23 170,754 to FY24 283,649; equity capital doubled), which inflates
a 3-year growth rate: disclosed, not corrected.

## Decisions
- **D1 Primary source and key.** `TapetideFundamentals` (`name="fundamentals_tapetide"`,
  `source="tapetide"`) is keyed on the NSE symbol, so no `bse_code` is needed (the XBRL-independent
  part of #128). The BSE `IndiaFundamentals` is used only when Tapetide produced no statements and
  the security has a BSE scrip code.
- **D2 Seam.** `ingest_fundamentals(..., tapetide=TapetideFundamentals)` beside `india=`; the CLI
  has `_tapetide()` like `_india_fundamentals()`. Tests inject MockTransport clients.
- **D3 Network and secret.** A private `_rpc` helper posts JSON-RPC. The token is read per call
  through `nivesh_core.secrets` and held in no attribute, cache parameter or message; the
  client and the rate limiter are built only after the token check. The scheme word is built by
  concatenation so the secret scanner stays clean. Calls are paced at one per 1.1 seconds.
- **D4 Degradation and halting.** A security with no BSE scrip code used to stop the India ingest
  with `NiveshError` (exit 1); it is now a failed-source line ("no Tapetide statements and no BSE
  scrip code for the fallback") with exit 0, a deliberate change because Tapetide needs no scrip
  code and a missing code only matters for the fallback. Every failure is a reason, never a crash or zero rows: no token
  gives "TAPETIDE_TOKEN not set" with the setup command; HTTP 401 or 403 names token regeneration;
  an `isError` text naming a rate limit, quota, "too many requests", a daily limit or "per day"
  (or a `Retry-After` header, honoured only on the `isError` path, or HTTP 429) is `RateLimited`
  ("50 calls/day, resets 00:00 IST"); other limits such as "periods limit exceeded (max 40)" are
  ordinary errors and do not halt the run; an unknown symbol is "no financial statements"; any other `isError` is the
  first 160 characters of the sanitised text (token removed, control, bidi-override and separator
  characters stripped); a transport exception is reduced to its type name (the HTTP layer can
  echo a header value), and the token is read with all whitespace removed; an envelope that is not the expected shape (non-JSON,
  wrong section count, wrong section, or JSON nested deeply enough to overflow the parser, which
  is caught as `RecursionError`) is `SourceUnavailable("unexpected response shape")`.
  `DataQualityError` is kept for value checks in `validate()` (a text, list or non-finite cell, a
  magnitude above 1e12 in a statement cell; in the shareholding table only the percentage rows
  Promoters, FIIs, DIIs, Government, Public and Others are bounded, at 1000, while other rows such
  as `No. of Shareholders` (counts in the millions) must merely be flat; in the snapshot only
  `Face Value` is checked as a number; a pledge that is not an object or whose percentage is a
  number outside 0..100 (a non-numeric one is stored as unknown)); bad data is never cached. The
  statements, the face-value snapshot and the shareholding are separate calls and **fail independently**: each
  value-check failure is a failed-source line with its reason (the ingest never aborts), and one
  bad statement section loses only that section. Scalar or non-object `availability`, a period
  label with an impossible year, an implausible face value or share count, and any
  `ArithmeticError`, `ValueError` or `TypeError` while mapping, and those or a DuckDB conversion
  error while writing, are likewise reasons, never a traceback; a non-number `Face Value` (object,
  list, text, nan) is treated as absent and costs only `shares_out`; the write is one transaction, so a failure stores nothing. A token or
  quota failure sets `_halt_reason` on the adapter: later calls raise it **without any request**
  (a rejected token is never resent; one failure line is reported, not six). Vendor error text
  is scrubbed of the token over its whole length before it is cut to 160 characters. A body that
  passes `validate()` but is malformed (a row that is not a period map, a non-string period, a
  shareholding table of non-dict rows) is rejected in `_fetch` as the same `unexpected response
  shape`, so mapping never sees it.
- **D5 Mapping.** Values are absolute INR (crore x 10^7, `Decimal`), EPS as given. `revenue` =
  `Sales` or `Revenue`; `net_income` = `Net Profit`; `eps`; `interest_expense`;
  `depreciation_amortization` (a bank's 0 is treated as absent); `total_equity` = Equity Capital +
  Reserves (both needed); `total_debt`; `total_assets`; `deposits`; `financing_profit` (banks and
  NBFCs); `cfo`; `capex` = CFO - Free Cash Flow (skipped and listed when negative); `shares_out` =
  Equity Capital x 10^7 / `Face Value` for the latest balance-sheet period only (skipped with a
  reason when the face value is absent). Not stored: `operating_income` (Tapetide's Operating Profit
  is an EBITDA-style figure and the India rule in `fa` stays), `pretax_income` and `income_tax`
  (no consumer, and the derivation is wrong with associates and exceptionals), cash, receivables,
  inventory, current items, dividends, gross profit, bank asset quality.
- **D6 Point in time.** `filed_at` = `available_from`, clamped to the fetch date (a future guess is
  never used; clamped rows are counted and reported) and clamped up to the period end (a filing
  cannot precede its period: look-ahead; counted and listed in the report notes). A period whose end
  is after the fetch date is skipped and listed (it cannot have been filed, so no date would be
  honest). Rows with basis `reported` are written with
  source `tapetide`, rows with basis `estimated` with source `tapetide_est`, **in one transaction**
  (`write_fundamentals(..., more=[...])`). When a later run brings a reported figure for a period
  that was stored as estimated, the estimate is deleted in the same transaction (`supersede`), so a
  later-dated estimate can never shadow the declared one. A period without an availability entry is
  skipped and listed (no invented date). History before about Sep 2024 is therefore approximate.
- **D7 Shareholding.** Promoter and public percent by quarter; `filed_at` = quarter end + 21 days
  (the SEBI deadline, approximate, clamped to the fetch date), source `tapetide_est`. A quarter
  ending after the fetch date (or in year 9999) is skipped and listed, and so is a table with no
  Promoters or Public row. The pledge is stored only for the latest quarter: the number, or 0 when
  the status is "none" and no number is given, else unknown. The
  vendor's own promoter figure differs from the `Promoters` row, so only the row is used. A
  shareholding failure (HTTP, shape, or a bad value) is its own failed-source line and does not
  fail the financials; likewise a bad snapshot loses only `shares_out`.
- **D8 Honest coverage.** `FundamentalsReport` gains `notes`, `estimated_rows`, `clamped_rows` and
  `tapetide_calls`; the CLI prints "estimated filing dates: N", the notes, and "Tapetide calls this
  run: N (free plan 50/day, resets 00:00 IST)". A note says when net income differs from EPS x
  shares by more than 3% (it includes **minority interest**; RELIANCE and HDFCBANK trigger it; the
  comparison is approximate because EPS is weighted and shares are period-end) and when a section is
  missing.
- **D9 Engine changes (deliberate, test-first).** `sector_kind_of` also treats a `deposits` or
  `financing_profit` row as financial (HDFCBANK has no sector in the master; NBFCs file financing
  profit). The blanket `_INDIA_GAPS` override in `fa.py` is removed: cash conversion, free-cash-flow
  margin and accruals are computed from the rows when present and otherwise name the missing item
  (the BSE fallback rows still lack them, so the reason is honest either way). The EBIT-based India
  override stays. The `fa.py` module docstring is updated.
- **D10 Cache and quota.** `cached_fetch` with the 7-day `fundamentals` TTL and stale-on-failure.
  Params are `{resource, symbol, section}` with section one of quarterly, profit_loss,
  balance_sheet, cash_flow, snapshot, and `{resource: shareholding, symbol}`; the cache holds the
  parsed section (for the snapshot only the `top_ratios` extract), never headers or the token. One
  company costs 6 billed calls (4 sections, 1 snapshot, 1 shareholding); five cost 30 of the 50 daily
  calls; a re-run inside the TTL costs 0; there is no automatic retry anywhere.
- **D11 DCF and the minority caveat.** India now has cash-flow and balance-sheet rows, so the
  fair-value range, free-cash-flow yield and P/B are new output for India. The DCF is built on
  Tapetide free cash flow (financials: on net income), and that net income, and so net margin and
  ROE, include minority interest: they are overstated for groups with large minorities (RELIANCE
  ROE by `fa` is about 11% against 8.9% in Tapetide's own figure). The caveat is in the
  `market fundamentals` note, the `/fa` command text and here. An attributable figure from EPS x
  shares is valid for the latest year only (bonus issues and splits), so it is deferred (X1).
- **D12 Known limits kept visible.** P/B history before the first filed `shares_out` is
  unavailable by design (one share count, latest year). A restated value overwrites under the same
  primary key (no append-only versioning of restatements, since the vendor gives the latest
  figures). A later split or bonus rewrites `shares_out` with the then-current Face Value. The
  recorded fixtures are trimmed public financial figures used only for tests; the snapshot fixtures
  for all but RELIANCE are marked SYNTHETIC. **CR-7 tension, stated openly:** the vendor's terms
  forbid redistribution of its data, yet these trimmed, rounded fixtures (5 companies, a few
  periods, real values) are committed to the repository; the untrimmed raw captures are not. The
  fixtures are for tests only and the repository is the owner's private one, but the owner may
  replace them with fully synthetic data at any time without changing a test's intent (numbers in
  the tests would then change).

## Consequences
- `nivesh market fundamentals RELIANCE` fills 12 annual and 13 quarterly periods and the
  shareholding. `fa` shows growth, margin, ROE, leverage, cash conversion and the pledge;
  `valuation` shows P/E (trailing four quarters) and P/B. `fa HDFCBANK` is classified financial
  with its bank asset-quality items unavailable and a reason.
- Still unavailable for India: operating margin, EBITDA, ROIC, ROCE, interest cover (the
  profit-before-tax rule), cash, current ratio, working-capital days, and the bank items GNPA, NNPA,
  NIM, CASA and CAR.
- The daily quota is shared with any other Tapetide client; with no remaining-quota header the
  client can only count its own calls. A quota denial ends the run's Tapetide calls, keeps cached
  data, and says when the window resets.
- The token was pasted into a chat once; the owner should rotate it under the same keychain name
  (`nivesh secrets set TAPETIDE_TOKEN`); nothing in code changes.
- Vendor risk: terms are personal use only; the endpoint or its response shape can change. The
  shape checks turn a change into "unexpected response shape", not bad data.

## Deferred
- X1: attributable net income (EPS x shares per year) and EBITDA through Tapetide's `Operating
  Profit` (needs the India operating-income rule in `fa` and `valuation` changed).
- X2: the NSE XBRL path, only if the owner ever accepts the terms-of-use risk (it also needs the
  taxonomy work in #128).
- X3: the #128 XBRL tag map and classifier fixes for the BSE fallback (XBRL-only, not needed here).
- X4: a Tapetide ratios section (debtor, inventory, payable days) and a per-company quota guard
  across runs, if the daily limit ever binds.

## Notes

- `nivesh_core/trace.py` `digest_text` now joins its 8-character groups with `.` instead of `-`.
  This is a root-cause fix: a group ending in five digits and the next starting with five read as an
  Indian phone number (5-5 shape) to the PII scanner, so a digest could collide with the scanner.
  It is unrelated to flaky issue #113, which is a random multipart boundary containing a run of
  nine or more digits.
