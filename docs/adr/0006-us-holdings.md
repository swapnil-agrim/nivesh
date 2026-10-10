# ADR-0006: US holdings ingestion (E3)

Status: accepted. Wire formats (Alpaca fields and header names, Alpaca and Robinhood export
headers, SEC exchange labels for ARCA and AMEX) are per spec and unverified against live sources.
All fixtures are hand-built from documentation. **AC3 (ST-3.3) is met on the FRED leg only: the
RBI reference-rate source is deferred (D1).**

## Decisions
1. **CSV first behind an interface.** The US CSV importer (generic template plus Alpaca and
   Robinhood-export presets) ships first; the Alpaca connector is a read-only interface
   (`BrokerReader`: `positions`, `account`) beside it. Robinhood has a CSV-export preset only.
2. **Currency in the schema.** Migration `0005_us_holdings.sql` (forward-only) rebuilds
   `holding_snapshot` with `currency` (default `INR`, so every existing row reads as INR) and a
   **nullable `value_inr`**. Non-INR rows store `value_inr = NULL` and the native quantity and
   price; the store ignores any INR value passed with a non-INR row. INR is derived at read time
   from the stored `usdinr` series, so there is one source of truth and a missing rate is
   representable. Rollback is restoring the pre-upgrade `nivesh backup`.
3. **Lots.** A new `lot` table (same migration) keeps dated purchase lots. Lots belong to an
   ingest and follow the same newest-ingest-per-(account, holder) rule as snapshots, so a
   re-import of an edited file replaces its lots. The placeholder merge in the security master
   also repoints lots.
4. **Accounts.** US accounts use their own kinds (`manual_us`, `alpaca`), so a US CSV with the
   same label as an India CSV can never supersede it. Source precedence is
   `investright, cas_demat, cas_rta, alpaca, us_csv, csv`.
5. **US security resolution.** `UsSymbolResolver` reads `security` rows of market US (placeholders
   and indices excluded; `.` and `-` fold, so `BRK.B` matches `BRK-B`). The master's exchange wins
   over the CSV's. A symbol on several exchanges resolves only with a matching hint. A master row
   outside NYSE, NASDAQ and ARCA (CBOE, OTC, the SEC parser's fallback `US`) is rejected, never
   replaced by the CSV hint. Without a master row, the CSV exchange must be one of those three.
6. **FX source honesty.** `nivesh_engine/fx.py` is pure and Decimal-only. The rate is the latest
   positive `usdinr` observation on or before the valuation date (IST date of the clock). The
   series is FRED DEXINUS (H.10 cadence, so gaps of several days are normal): older than 10 days
   is flagged `stale`, older than 30 days is unavailable. Every payload labels the source
   `fred:DEXINUS (RBI reference rate not wired)`. The cost-meter `usd_inr` setting is never read.
   A missing rate is **unavailable**: never zero, never one.
7. **Consolidation and exposure.** USD rows are converted at the valuation-date rate; without a
   rate they are excluded from INR totals and weights (with a note), and USD exposure is
   unavailable. A non-INR row without an ISIN is keyed `symbol:USD`, so one ticker from Alpaca and
   the CSV dedupes. Merging keeps the native price. INR P&L converts USD cost at the valuation-date
   rate, so it excludes FX gain on cost.
8. **XIRR scope.** `nivesh_engine/returns.py`: Decimal bisection, actual/365, returns None (never
   zero, never a raise) when undefined. USD XIRR uses each lot at its date and the lot quantity
   at the latest stored US close (at most 10 days old). INR-terms XIRR converts each lot cost at
   the rate on or before its lot date (at most 7 days old) and the terminal value at the
   valuation-date rate; any missing rate makes INR unavailable while USD is still shown. The
   overall figure is never partial. Holdings with fewer dated lots than shares are flagged
   ("XIRR covers only dated lots"). Alpaca positions carry no lot dates.
9. **Holding period and tax parameters.** `holding_days` comes from lot dates. `tax.us_long_term_days`
   in config drives an informational `long_term` flag (None when unset). Nivesh computes no tax
   amount and gives no tax advice.
10. **MCP surface.** The existing `get_holdings` and `combined_portfolio` gain `currency`,
    `value_usd`, nullable `value_inr`, an `fx` block and `exposure`; one new tool `get_lots`
    (eight tools). Field names avoid the redaction key parts (`overall_xirr_usd`, never
    `portfolio`). The market store is opened read-only; a busy store means "unavailable".
11. **Broker connector and proxy.** `AlpacaClient` issues only `GET /v2/positions` and
    `GET /v2/account`; responses are cut to a whitelist at the boundary (account numbers and asset
    ids never leave). Credentials are `ref:` names (`ALPACA_KEY`, `ALPACA_SECRET`). `ReadOnlyProxy`
    wraps a broker MCP that bundles trade tools: an **allow-list** (not a block-list) decides what
    is exposed, construction rejects an empty list and any write-named entry, and a denied call
    never echoes arguments. The tool-safety discovery now includes the `Proxy` suffix.
12. **Dependencies.** None added.

## Ceilings and risks
- The USD "value" of a CSV row is its cost (book value), not a market price; Alpaca rows carry
  the current price.
- Raw closes versus lot quantities can disagree across a split (D6).
- INR XIRR needs a rate within 7 days of every lot date, so macro history must reach the oldest lot.
- The allow-list cannot know a tool is dangerous; allowing an innocuously named tool that acts is
  an owner error the word check cannot catch.

## Deferred
- D1: an RBI reference-rate source for USDINR; only FRED is wired, so AC3 is met on the FRED leg only.
- D2: a runnable stdio allow-list proxy server wrapping a real broker MCP, and any Robinhood connector.
- D3: live verification of Alpaca fields, Alpaca and Robinhood export headers and SEC exchange labels.
- D4: lot dates for Alpaca positions (needs account activities).
- D5: the portfolio X-ray report and richer exposure (sector, geography).
- D6: historical-rate INR P&L, other foreign currencies, split-aware lot returns, any tax computation.
- D7: only if scope is trimmed: none (INR XIRR and `nivesh sync-us` are delivered).
