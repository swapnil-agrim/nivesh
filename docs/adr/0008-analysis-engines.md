# ADR-0008: Analysis engines (E6)

Status: accepted. Every indicator, ratio and score here is computed by deterministic, Decimal-only,
read-only code. The conventions below were derived by hand and checked against hand-derived known
answers; there is no TA-Lib or pandas-ta reference series in the repository (D3), and the India
statement tags, the 8-K item layout and the estimate fields are per spec and unverified against live
sources. Nothing here is advice.

## Decisions
1. **Pure engines, one loader, no new dependency.** The engines in `nivesh_engine` are pure
   functions: no network, no clock (`as_of` and `today` are parameters), no `float`, no random
   source, no I/O. The only bridge to the stores is `nivesh_adapters/analysis_data.py`, which reads
   in bulk (a constant number of statements for any number of securities), always returns bars on
   the adjusted basis (splits and bonuses applied, dividends not, Yahoo closes never adjusted
   twice), never writes, and defines no adapter or client. There is no new dependency: no numpy,
   pandas, scipy or TA-Lib, and `pyproject.toml` and `uv.lock` are untouched. All arithmetic is
   `Decimal` under one fixed context (28 digits, round half even, `dmath`); values are quantised
   once, at the boundary. The duplicated `_median`, `_q` and CAGR helpers of the mutual-fund
   engines moved into `dmath` with byte-identical output (the E5 tests pass untouched).
2. **No new MCP server (D1).** The `nivesh-engine` server of PID section 14.4 is deferred: the
   surface is the `nivesh ta | fa | valuation | flags | xray | risk | screen | score` CLI, which
   opens both stores read-only and prints exact decimal strings under `--json`. The registry, the
   MCP configuration and the allow-list are unchanged. Two output field names are renamed at the
   CLI boundary (`key` to `ref`, `portfolio_vol` to `overall_vol`) because a redaction pass keyed on
   field names (ADR-0003) would blank them; a test asserts no output field name triggers it.
3. **Config replaces `methodology.yaml`.** One nested `analysis` block (`AnalysisSettings`, extra
   fields forbidden, Decimal fields, grouped sub-models for `ta, levels, regime, setups, fa,
   valuation, flags, xray, risk, screen, scoring`) satisfies the PID's methodology file without a
   second loader. Every threshold, window and weight is owner-set and versioned; the example block
   in `config/nivesh.yaml` equals the built-in defaults. D13 asks the owner to review them.
4. **Indicator conventions (ST-6.1), TA-Lib style where identical.** SMA is simple. EMA uses alpha
   2/(N+1) seeded with the SMA of the first N closes. RSI and ATR use Wilder smoothing,
   `avg = (avg*(N-1) + x)/N`, seeded with the simple mean of the first N gains and losses (ATR:
   the first N true ranges from bar index 1). MACD(12,26,9): each EMA is seeded with the SMA of its
   own first N closes, the line starts where the slow EMA starts, and the signal is an EMA(9) seeded
   with the SMA of the first nine line values; TA-Lib seeds differently, so MACD can differ from
   TA-Lib in a late decimal on a short history (D3). ROC is close over the close N sessions back
   minus one (63, 126, 252). Realised volatility is the sample deviation of simple daily returns
   times the square root of 252 (same as `mf_returns`). Bollinger(20, 2) uses the population
   deviation; width is (upper - lower)/middle. Relative strength is the ratio to a benchmark or
   sector series over the dates both share. A long window with too few bars is null with
   `need N bars, have M` (SMA200 needs 200 bars, 12-month ROC 253); short windows still compute.
   The known answers (RSI 4500/58, ATR 20/9, Bollinger width 1.632993161855452, volatility
   sqrt(5.04), ramp closed forms) are asserted to 1e-6.
5. **Levels, regime and setups (ST-6.2).** A swing pivot is the first maximum (minimum) of the
   window of `pivot_window` bars either side; the last bars cannot be confirmed. Pivots cluster by
   ascending price within `cluster_tolerance_pct` of the cluster mean; levels below the last close
   are support, above are resistance, at most three each, ranked by touches then proximity then
   price. Regime is `risk_on` when the index is above its 200-day average and breadth is at least
   `risk_on_breadth_pct`, `risk_off` when below and at most `risk_off_breadth_pct`, else `neutral`;
   it is None with a reason, never guessed, when either input is missing or the universe is below
   `min_breadth_universe`. Setups in precedence `breakout, pullback, trend_continuation, base,
   downtrend, none`; entry zone is a half-ATR band around the trigger, stop is the entry low minus
   `stop_atr_mult` ATRs, invalidation is the nearest support strictly below the entry low else the
   stop, and reward/risk uses the nearest resistance above the entry (no resistance, no target, no
   invented figure). **Screener metrics `base_breakout` and `pullback_to_50dma`** are 1/0 flags that
   read "the setup classifier matched breakout" and "matched pullback" (a close above the nearest
   resistance on volume at 1.5x the 20-day average, and an uptrend close within one ATR of the
   50-day or 21-day average with RSI under the trend minimum). They are not a base-then-breakout
   sequence or an exact touch of the 50-day line; ST-9.2 presets may refine them (D10).
6. **Fundamentals (ST-6.3).** Rows carry `filed_at`; `fa_compute(as_of=D)` uses only rows filed on
   or before D (a restatement filed later is ignored and the earlier value used; shareholding
   follows the same rule, applied before the per-quarter ranking), and the loader applies the same
   bound again. ROE uses the average of opening and closing equity; ROIC is operating income times
   (1 - tax) over debt plus equity less cash, with the tax rate from the filing else
   `default_tax_rate_pct` and listed in the inputs. Every metric is `available` or carries a reason
   and the inputs it used; the output states a coverage percent. The US concept vocabulary is
   extended additively (13 EDGAR concepts, a new fixture file, the original fixture byte-identical).
   **India limits:** the India `operating_income` is a profit-before-tax proxy and is never presented
   as EBIT or EBITDA; EBITDA-based ratios, ROCE, interest cover, current ratio and cash-quality
   metrics are unavailable with a reason because the India items do not exist (D2). Banks and
   NBFCs (configured sector names, or bank items present) get GNPA, NNPA, NIM, CASA and CAR instead
   of EBITDA-based metrics.
7. **Valuation (ST-6.4).** P/E is the raw close at the observation date over the trailing four
   filed quarters of EPS, else the last annual EPS (labelled); history is month-end observations
   filed by then (no look-ahead), the percentile is the share of observations at or below the
   current value, with a `min_obs` guard; peers come from `SecurityMaster.peers` (industry, same
   market) or the owner's override, with `min_peers`. The reverse DCF grows the cash flow at a
   constant rate for `horizon_years`, adds a terminal value `CF_N x (1+gT)/(r-gT)` and solves the
   growth that equals the market cap by bracketed bisection; a flat cash flow with zero growth is
   worth CF/r (the invariant test). The base, bull and bear range echoes every assumption. A
   non-positive cash flow or an unreachable market cap is unavailable with the reason.
8. **Red flags (ST-6.5).** `detect_flags` returns one result per flag with status `fired`,
   `clear` or `not_evaluable`, severity, and evidence made of numbers, dates and filing
   identifiers only; filing text is never quoted (it would have to go through
   `nivesh_agents/untrusted.py`). A flag whose data does not exist (US pledge, India auditor
   change, contingent liabilities without an input) is `not_evaluable`, never `clear`; the
   thresholds and the hard/soft severity map are owner-set (D13). The auditor-change flag reads
   stored 8-K filings that carry Item 4.01 (D7 for the India source).
9. **Follow-ups #53 and #54.** XIRR is a **bracketed** bisection with an expanding bracket
   (upper bound 100, then tenfold up to an annual ceiling of 1e9); it is not Newton or Brent,
   and no Newton polish was added (the acceptance text "bracketed Newton/Brent" reads as bracketed
   bisection). `xirr_result` adds the reason (fewer than two flows, one date, no positive or no
   negative flow, ceiling); `xirr` keeps its signature and result, and a gain of 100 percent in 30
   days now solves instead of returning None. Lot coverage is tri-state, `exact`, `partial` or
   `over`, by exact Decimal comparison with no tolerance; `lots_cover_quantity` stays a bool (true
   only for exact) and the `get_lots` payload gains an additive `coverage` string. **Over-covered
   semantics:** a security whose dated lots exceed the held quantity gets no XIRR (never valued at
   the covered units), and the overall figure is withheld naming it, the same handling as a missing
   price; a security with lots and a held quantity of zero is therefore over-covered and now
   withholds XIRR (it was computed before). **X-ray difference:** the X-ray total pools only
   holdings with exact coverage and reports the share of the portfolio value that is, whereas
   `lot_report` overall still pools partial coverage with its existing note.
10. **Portfolio X-ray (ST-6.6).** Allocation by asset class, market, sector, market cap and
    currency, each summing to 100 over the rows that have an INR value (the others are listed with
    the reason); an `unclassified` bucket is reported, never dropped; drift in percentage points
    against the owner's targets over the union of classes; top 5 and top 10, HHI and effective
    positions; positions and sectors strictly over the profile limits, with the look-through
    sector basis separate. Market cap buckets use owner-set thresholds and are `unclassified` where
    no source exists (India holdings, funds; D4). Gold and unmapped fund categories land in
    `unclassified`. Indian direct equity has no dated lots, so its XIRR is unavailable with that
    reason (D9 covers Alpaca lot dates and NPS).
11. **Risk (ST-6.7) and screener (ST-6.8).** Volatility is sqrt(w' S w) x sqrt(252) on returns
    aligned to the date intersection; drawdown is a backtest of today's constant weights; days to
    trade is the position over `participation_pct` of the 20-day average rupee volume. INR/USD
    sensitivity is not computed (D8). The screener reads a YAML rule file (operators `< <= > >= ==
    != between`, extra keys rejected) over a metric registry shared with the scoring that already
    holds the ST-9.2 preset metrics. A rule unavailable for every security is skipped and reported
    with its reason, a security with a skipped rule is a partial match, a `required` rule rejects
    instead of skipping. The universe is an **explicit universe**: the securities named on the
    command line, else all direct securities with stored bars; the output says so, and
    universe-relative metrics such as `rs_percentile` rank within it (D6). Measured on 1,000
    synthetic securities with about 500 bars and 40 statement rows each, on the development machine:
    4.8 s to load and 2.0 s to evaluate (limit 60 s); the 100-security CI budget test and the
    `bench`-marked 1,000-security test are separate, and the 60 s acceptance figure is verified by
    running the bench by hand, not by `make check`.
12. **Scoring (ST-6.9) and the BR-12 guard.** Five factors, 0 to 100, each the mean of the cohort
    percentiles (mid-rank, lower-is-better inputs inverted) of its available sub-inputs; a cohort
    never mixes markets and falls back from sector to market, with the reason, when the sector has
    fewer than `min_peers_for_sector` names; a cohort of one scores 50, never 100. Long-term weights
    are 30/25/25/10/10 and positional 15/10/20/45/10 (quality, value, growth, momentum, risk);
    every score carries `weights_version` (the owner's label) and `weights_digest` (a digest of
    the weights). **BR-12 holds by construction:** `support` is the mean of the quality, value and
    growth factors; the momentum credit is `min(momentum, support)`, less a penalty when the
    valuation percentile is over the limit, and zero when no fundamental factor is available; the
    **price-derived** inputs (`price_vs_sma200_pct`, `rs_benchmark_change_6m_pct`,
    `setup_quality`, `vol_60d`, `max_drawdown_pct`, `atr_pct`) are the only ones held to the
    support; the top band needs the composite at `top_band_min` and a support at `support_floor`.
    A hard red flag sets `cap = "HOLD"` and clamps the band to hold; soft flags lower the risk
    factor without a cap; a flag that is `not_evaluable` neither caps nor reassures. A security
    with fewer than `min_input_pct` of its inputs is `insufficient_data` with no composite. The
    scoring source reads no trailing-return field (a source test), and a seeded sweep over
    thousands of vectors, plus adversarial ones, shows a security with only trailing-price strength
    never reaches the top band. Band labels are data strings; the config field for the top-band
    threshold is `top_band_min`.
13. **Safety and PII.** No function, class or command name carries a write verb
    (asserted by `tests/safety/test_analysis_safety.py` over the new modules and the CLI). The
    engines need no external access; test dummies are built by concatenation; fixtures use invented ISIN-
    shaped identifiers and synthetic series built from integer arithmetic; the CLI output and the
    new fixtures and config are scanned for PII. Coverage: every new engine module is at or above
    90 percent under the 85 percent gate.

## Unverified and owner-set
- Unverified: the recorded TA-Lib/pandas-ta parity (D3), the India XBRL tags beyond the items
  already ingested (D2), and the wire format of any 8-K item layout beyond the stored sections.
- Owner-set and versioned in the `analysis` block: indicator windows, level and setup thresholds,
  flag thresholds and severities, DCF scenarios, risk and screen limits, market-cap bands, target
  mappings, factor weights and band thresholds.

## Deferrals
- D1: the `nivesh-engine` MCP server and its contract/security review (nine tools of PID 14.4).
- D2: India statement items for EBITDA, D&A, interest, cash, current assets and liabilities,
  receivables, total assets, CFO and capex (refs #39, #46).
- D3: recorded TA-Lib/pandas-ta reference fixtures and MACD seeding parity.
- D4: market-cap source for India holdings, ETFs and funds in the X-ray.
- D5: sector-index mapping and a Nifty 500 benchmark for relative strength against a sector.
- D6: the investable universe (ST-9.1) replaces the explicit id list for screen, breadth and RS rank.
- D7: sources for the auditor change (India), contingent liabilities and related-party flags.
- D8: INR/USD sensitivity, time-weighted return against benchmarks and tax-aware lot flags.
- D9: Alpaca lot dates (#52) and NPS XIRR (#33).
- D10: ST-9.2 screener presets as YAML packs.
- D11: the delivery percentage metric (India).
- D12: deeper India filing history for a 10-year valuation median.
- D13: owner sign-off of red-flag severities, thresholds, factor weights and band thresholds, and
  reconciling the PID `methodology.yaml` name with the `analysis` block.
