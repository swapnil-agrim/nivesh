# ADR-0007: Mutual fund intelligence (E5)

Status: accepted. Wire formats of MFapi.in, AMFI NAVAll (beyond the grammar the E4 fixture
already reads), the monthly-holdings source and the scheme-metadata source are per spec and
unverified against live sources; all fixtures are hand-built. Parsers are strict
(`DataQualityError`), so a wrong guess fails loudly instead of storing a wrong number.

## Decisions
1. **Read-only, no model in the numbers.** Every mutual-fund figure comes from deterministic,
   Decimal-only code with no network, no clock, no random and no float in the engine
   (`as_of`/`today` are parameters). There is no new dependency (numpy, pandas and scipy are not
   project dependencies) and no new MCP server (D2): the MF surface is the `nivesh mf` CLI. No
   function, class or command name carries a write verb (asserted by the tool-safety tests).
2. **Storage.** Migration `duck/0003_mf.sql` is forward-only and additive: four new tables
   (`nav_point`, `fund_meta`, `fund_holding`, `nav_gap`), DECIMAL columns, the primary key
   includes `source`, UTC text timestamps. Rows are keyed by the SQLite `security.id` of the
   scheme's master row (no cross-store foreign key). Only held and candidate schemes are stored,
   not the whole scheme universe (D8). Rollback is restoring the pre-upgrade `nivesh backup`.
   `fund_meta` is append-only by `as_of` (the latest wins on read); `fund_holding` holds one
   month at a time per source and a re-ingest replaces the month; `nav_gap` is recomputed and
   replaced on every NAV run, so a backfill clears a flag. AUM is stored in rupees crore
   (`aum_crore`), as the source states it, so stored values stay clear of the long digit runs
   the redaction sweep masks (ADR-0003). `fetched_at` and `detected_at` are store-side clock
   reads, outside the engine's no-clock rule.
3. **Config.** An `mf` block (`MfSettings`, extra fields forbidden) holds every threshold, the
   exit-load and tax tables, the benchmark map (fund benchmark text to an index symbol) and the
   screen weights. The holdings source credential is a `ref:NAME` reference only (field
   `holdings_api_ref`), resolved at request time and sent as a header: never a parameter, never
   cached, never logged. No rule hard-codes a threshold. Field names avoid the redaction key
   parts (`amfi_code`, `scheme_name`, `expense_ratio`, `aum_crore`, `weight_pct`, ...).
4. **Scheme identity.** A scheme has one master row per ISIN. `SecurityMaster.by_amfi_code`
   returns the growth-looking row for a code (else the lowest id), `by_isin` is an exact ISIN
   lookup without the fuzzy fallback, and `mf_schemes` lists one row per code. The NAV and
   metadata stores are keyed by the `by_amfi_code` row, reached from a holding through its AMFI
   code, so a holding on the non-preferred (IDCW) ISIN still maps to the same history.
5. **NAV (ST-5.1).** MFapi.in is the primary and AMFI NAVAll the fallback, both GET only behind
   `Adapter`. Only an unavailable or rate-limited primary falls through; a `DataQualityError` is
   never masked. A source stores only the dates it does not yet hold (a re-run adds nothing, a
   late backfill lands). NAVAll gives one point per run; its "N.A." NAV rows are skipped and
   counted. A cross-source difference beyond `nav_tolerance` is flagged, the primary value wins.
   Gaps are runs of missing weekdays minus configured holidays longer than `nav_gap_days` (5 is
   not flagged, 6 is), checked between stored dates and after the last one; the trading calendar
   is not used because it raises for a year without holiday data. The gap check is skipped, with
   a note, when only AMFI points exist (one per run, so the spans are not real gaps).
6. **Metadata and the direct twin (ST-5.2).** Plan (direct, regular) and option (growth, idcw,
   other) are read from the scheme name, not from a holding's free-text plan; an unknown plan is
   unknown, not regular. A regular scheme's twin is the master scheme with the same AMC (when
   both are known), normalised name and option and a direct plan; two or more matches are
   `ambiguous` and listed, never guessed. Missing TER or AUM is NULL. The TER cost is the gap in
   percent and rupees per year on the current value; no twin, no TER or no value is "unavailable"
   with the reason, and a gap of zero or less is "no saving".
7. **Monthly holdings (ST-5.3).** The configured source (`mfdata`, `amc` or `fixture`) selects a
   parser table; weights outside 0 to 100, a month summing above 101 percent, a bad ISIN or a
   non-month-end date raise. Every line stays: cash, derivative and debt lines are kind `other`
   (key `OTHER:<label>`), and a line that is itself a mutual fund is reclassified `other`
   (fund-of-fund look-through does not recurse, D8). Each equity ISIN maps through the master;
   the report gives mapped, unmapped and other weight per month and a coverage line when fewer
   than 12 months are stored.
8. **Analytics (ST-5.4).** Definitions (see `mf_returns.py`): a window ends on each NAV date and
   starts at the latest point on or before `date - window_days`; the window return is annualised
   on the actual elapsed days, `(end / start) ** (365 / elapsed) - 1` via Decimal `ln` and `exp`
   under a fixed context (28 digits, round half even); fewer than two windows is "insufficient
   history". Relative metrics use the dates present in both series (coverage reported, below
   `min_alignment_pct` they are unavailable). Beat percent is the share of windows with positive
   excess and median excess averages the two middle values for an even count. Standard deviation
   is the sample deviation of daily returns times the square root of 252; max drawdown is peak to
   trough on NAV with both dates; downside capture is the mean fund return over the benchmark's
   down days divided by the mean benchmark return over them; Sortino is
   `(252 * mean return - MAR) / (sqrt(252) * downside deviation)` with MAR from `mar_pct`.
   **The benchmark is a price index, not TRI**, so excess returns are overstated by the dividend
   yield and every output says so (D4). IDCW schemes are "not comparable" (payouts distort NAV)
   and get no numbers (D8). The valuation lens is a weighted harmonic mean of P/E and P/B (the
   inverse of the weighted yield, so loss-makers and missing multiples are excluded and coverage
   is reported) against the median, minimum and maximum of the fund's own latest 36 month ends,
   with at least 24 usable months; per-stock multiples come from stored annual EPS and book value
   per share as of each month end (no look-ahead), so P/B needs shares outstanding and is often
   unavailable. Benchmarks are mapped through `mf.benchmarks`; an unmapped one makes relative
   metrics unavailable with the reason.
9. **Overlap and look-through (ST-5.5).** Pairwise overlap is the sum of the smaller weight over
   common equity ISINs; the matrix is symmetric and sorted by AMFI code. Look-through exposure
   is `value * weight / 100` per fund summed with direct equity of the same ISIN, listing every
   contribution, as rupees and percent of the portfolio total, top 20 stocks (ties by ISIN),
   sectors, an `unmapped_sector` bucket (never guessed) and an `other` bucket. A fund without a
   current value or stored holdings is excluded and named.
10. **Lots, exit load and tax (ST-5.6).** Lots are FIFO from statement transactions (units added
    by purchase, SIP, reinvested dividend, switch-in and gift-in; removed by redemption,
    switch-out and gift-out; cash dividends and tax lines ignored; segregation, reversal, misc
    and unknown rows reported as warnings). A redemption beyond the open units is a reported
    data error, not clamped; lot units must reconcile with the held quantity, and a stale or
    missing NAV makes value and gain unavailable. Exit load and tax rates are owner-set tables
    (D6): an unknown row or unset rate is "unavailable", gain and days are always shown. Nothing
    here is advice.
11. **Fund doctor (ST-5.6).** One of KEEP, SWITCH_TO_DIRECT, REPLACE, CONSOLIDATE or REVIEW,
    decided by the first rule that fires: (1) regular plan with a found twin and a TER gap above
    `ter_excess_pct`; (2) overlap with an owned same-category fund above `overlap_pct`; (3)
    consistency below the minimum or downside capture or drawdown beyond the maximum, REPLACE
    when a screened candidate exists else REVIEW; (4) short manager tenure, stretched valuation,
    ambiguous twin, not comparable, or an unavailable core metric (beat percent, median excess,
    downside capture, drawdown, or the TER gap when a twin was found), REVIEW. Reasons carry the
    code, metric, value and threshold, sorted by rule then code. An unavailable metric is listed
    as such and never treated as a pass; tenure and valuation unavailable are listed but do not
    force a review. Exit-load and tax blocks are computed only for SWITCH_TO_DIRECT and REPLACE
    and are shown before the action. **BR-12:** a trailing one-year return is accepted as
    display-only input and no rule reads it, so it can never be the only reason (three tests,
    one of them a source assertion). The agent narrative and `/review-funds` are D1.
12. **Discovery (ST-5.7).** Hard filters (direct plan, comparable growth option, then the
    requested minimum AUM, maximum TER and minimum tenure; unknown fails a requested limit), then
    a weighted rank-sum over consistency, downside capture, cost and valuation with weights from
    config; ties share a rank, unavailable ranks last and is flagged, the final tie-break is the
    AMFI code; at most five results with overlap against owned funds. The universe is what the
    owner has loaded (D8).
13. **Secrets, tests and PII.** MFapi and AMFI need no key. Test dummies for a credential are
    built by concatenation in `tests/pii_values.py`; fixtures use invented ISIN-shaped codes and
    synthetic names; an end-to-end test scans the SQLite and DuckDB dumps, cache payloads and CLI
    output and asserts the dummy reference value appears nowhere. Coverage: every new engine and
    adapter module is at or above 90 percent under the 85 percent gate. Issue #53 (XIRR bracket)
    is out of scope: no E5 module calls `xirr`, and a test pins that.

## Deferrals
- D1: the MF analyst agent narrative and the `/review-funds` command (depends on ST-7.5, not built).
- D2: MCP exposure of the MF engine (`engine.mf_*`); the CLI ships instead.
- D3: verification of the MFapi, AMFI and holdings-source wire formats against live sources.
- D4: category-relative metrics and TRI benchmark series.
- D5: none; nothing was trimmed for scope (the discovery, valuation and tax-impact stories shipped).
- D6: exit-load scraping and detailed India tax rules; owner-set tables only, gain and days always shown.
- D7: style and market-cap drift metrics and manager-change detection beyond the start date.
- D8: whole-universe NAV storage, IDCW-adjusted series and fund-of-fund look-through recursion.
