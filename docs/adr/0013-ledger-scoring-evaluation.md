# ADR-0013: Ledger, scoring and evaluation (E12)

Status: accepted. E12 finishes the call ledger (ST-12.1), scores every call at five horizons
(ST-12.2), builds the scorecard and `/scorecard` (ST-12.3, #101), reports calibration of
conviction (ST-12.4), adds a golden-set harness with a release gate (ST-12.6) and a look-ahead
guard (ST-12.7), and derives year-to-date turnover from the ledger (#87). It adds migration 0008
(forward-only, additive), four commands (`decide`, `rescore`, `scorecard`, `calibration`), two
config blocks (`scoring:` and `evaluation:`), **no new MCP server and no MCP change** (the
`nivesh-memory` surface is not built, so it stays read-only by construction), no new agent,
prompt, schema or dependency. No test uses the network, a model, personal data or a stored value.
Every figure is the owner's own record and not investment advice.

## Owner decisions (safest option chosen; owner sign-off is X6)
- **OD-1 `last_close` basis.** `ledger_entry.last_close` is the **split-adjusted** close as of the
  issue date, **display only**. Scoring never divides by it: it reads the issue-date bar and the
  horizon-end bar from the same current series (`load_bars`, split-adjusted), so a split after
  issue cannot corrupt a return. The benchmark is read the same way; the benchmark level stored at
  issue is informational. Dividends are not applied (price return, as in ADR-0008), so every score
  row carries `basis = split_adjusted_price` and every output says **price index, not TRI**. A
  total-return series is X2.
- **OD-2 Issue date.** New columns `as_of` (the run day) and `price_date` (the date of the bar
  `last_close` came from) and `source` (`verdict` or `holding_review`). The entry bar is the bar
  dated `price_date`, not the `as_of` snap, so a bar that did not exist at run time is never the
  entry. Rows written before 0008 have no `as_of`: for those only, the issue day is the IST date
  of `created_at` and the entry is the last bar on or before it within 5 days. A new row with
  `as_of` set and `price_date` empty (no bars at issue) is not legacy: it never borrows a later
  bar, stays pending, then becomes insufficient after the grace window. There is no backfill by
  update (the tables refuse updates). `decided_at` and `created_at` are record time; the trade
  date is `ledger_decision.on_date` (`nivesh decide --on DATE`, default the IST record day).
- **OD-3 Horizons.** The five horizons 1/4/13/26/52 weeks; target date is `as_of` plus 7 days per
  week. Sessions are the stored bar dates of each series (no holiday calendar). The end bar is the
  last bar on or before the target and never after it. A call is **pending** (no row at all)
  until both the security and the benchmark have a bar on or after the target; nothing is carried
  forward.
- **OD-4 Rules.** Assumed round-trip costs (config, percent): India 0.25, US 0.10. Entry verdicts
  (the top entry verdict and ACCUMULATE): `call_excess = ret - benchmark - cost`, hit when above zero. Exit verdicts
  (TRIM, AVOID and the exit verdict): the do-nothing comparison, `call_excess = benchmark - ret`,
  no cost, hit when above zero. The raw `excess = ret - benchmark` is stored for display; all
  mean, median, worst and calibration figures use `call_excess`, one sign for every verdict. HOLD
  is scored (return and excess stored) but has no hit and no `call_excess`, so it counts in calls
  and scored only. INSUFFICIENT_DATA calls get one `insufficient_data` row per horizon: counted,
  not scored. **Staleness:** if the last bar on or before the target of a leg is older than
  `scoring.stale_days` (default 7), or a leg has none, the row is `insufficient_data`; it is
  written only once `today` is at least target plus `stale_days` (grace for late data). A halted
  or delisted name whose leg never reaches the target becomes insufficient the same way.
  `insufficient_data` is not permanent: the unique index is partial (scored rows only), so a later
  `scored` row for the same key is appended after a backfill, readers prefer `scored`, and a
  scored row is never superseded by an insufficient one. A rule change bumps `SCORE_VERSION`
  (currently 1) so new rows sit beside the old ones.
- **OD-5 Benchmark.** `scoring.benchmarks` (market to index symbol) wins over
  `analysis.ta.benchmarks`. The benchmark entry bar is the last benchmark bar on or before the
  security's entry bar, within 5 days. With no benchmark the rows turn insufficient after the
  grace window and the reason is printed. Every output carries the note **price index, not TRI**
  until the owner loads and configures a TRI symbol (X2).
- **OD-6 Scorecard.** A bucket with fewer than 20 scored calls (`scoring.min_bucket`) reads **not
  enough data**. The unit of count is the call (a distinct surviving ledger row), never a score
  row. Every dimension except `horizon` uses one primary horizon (`scoring.primary_horizon_weeks`,
  default 13); the `horizon` dimension shows each horizon as its own bucket. Dimensions: verdict,
  horizon, market, conviction, command (from `run.command`), and system calls against calls acted
  on. **One decision rule everywhere:** only effective rows count (a row a later `corrects_id` row
  names is ignored); `acted` and `partial` rows add up; a later `skipped` does not undo an
  earlier `acted` (only `--corrects ID` undoes it). **Dedupe:** a row that a later row corrects
  folds into its correction; rows with the same security, issue day and verdict keep the latest
  id; then repeats of one verdict on a security inside 7 days times the primary horizon fold into
  the earliest call, so daily or weekly reviews do not stack. Decisions on a folded row mark the
  call it folded into. Folded counts are printed.
- **OD-7 Calibration.** At least 40 scored, hit-eligible calls at the primary horizon
  (`scoring.calibration_min`; calls, not rows), else **not enough data (N of 40)**. Per
  conviction bucket: hit rate and mean `call_excess` with a seeded bootstrap 90 percent interval
  (`resamples` 1000, `resample_seed` 20260101, nonzero). The generator is an integer xorshift on
  an `rng_state`; the stdlib generator is not used, so a report replays exactly. Monotonic is
  **non-strict**: hit rate does not fall from high to medium to low. The verdict is `monotonic`
  (non-increasing and no adjacent intervals overlap), `not monotonic` (a reversal whose intervals
  are apart) or `inconclusive` (intervals that would decide it overlap, or fewer than two buckets
  have calls). The index draw is `state mod n`; the small modulo bias is accepted at these sizes.
- **OD-8 Look-ahead guard.** `evaluation.model_cutoffs` (model id to training-cutoff date).
  `assert_out_of_sample(eval_start, model_id, cutoffs)` refuses unless `eval_start` is strictly
  after the cutoff; an unknown model or an empty map refuses. The harness runs it first for any
  case marked `historical` and records a refusal as a failure, not a skip.
- **OD-9 Golden gate.** `tests/eval/golden_cases.yaml` holds at least 40 model-free cases, each
  pass or fail with a reason, and `tests/eval/baseline.json` holds the committed pass rate
  (100). A run **blocks the release** when the pass rate falls more than 5 pp below the baseline,
  or when any case marked `safety` fails (every safety case is a PID safety property: ceilings and
  vetoes, momentum alone never reaching the top band, no write-named tools, citation misses,
  append-only tables, look-ahead refusals). With no baseline file the run only reports.
- **OD-10 Turnover (#87).** Year-to-date means the **calendar year** (the Indian financial year is
  X6): the sum, over effective `ledger_decision` rows with decision `acted` or `partial`, bucketed
  by `on_date`, on calls whose verdict is an exit verdict, of quantity times price (USD at
  `settings.usd_inr`, Decimal). The cap used by `propose_moves` becomes `max(cap% of total minus
  year-to-date sold, 0)`. Documented approximation: divided by today's portfolio total (no
  year-start value is stored). ADR-0010 is not edited; this ADR supersedes its per-proposal cap
  wording.
- **OD-11 Holding reviews in the ledger.** `review holdings` writes one row per call:
  `ADD` becomes ACCUMULATE, `EXIT` becomes the exit verdict, `TRIM` and `HOLD` as is, `REVIEW`
  writes no row. Horizon is the thesis horizon, conviction the review confidence, review date the
  thesis review date, `source = holding_review`, `as_of` the review day and `price_date` the date
  of the last bar on or before it (empty when there is none), input hash the sha256 of the
  review JSON, no weight or zone. A ledger failure prints a warning and never hides the reviews.

## Decisions
1. **Append-only everywhere.** `ledger_decision` and `ledger_score` have the same refusing triggers
   as `ledger_entry`; owner decisions, corrections and rescoring are new rows. No statement in the
   code updates or removes rows of the three tables. The triggers survive backup and restore
   (tested).
2. **Atomic writes (#94).** `record_calls` and `record_scores` run in one `BEGIN IMMEDIATE`
   transaction and roll back on any failure; `ideas` writes all its rows or none.
3. **Where logic lives.** Pure Decimal logic is in `nivesh_engine` (`ledger_score`, `scorecard`,
   `calibration`, `lookahead`, in the no-float, no-clock, no-random scan); services in
   `nivesh_adapters` (`score_service`, `eval_harness`); `nivesh_core/ledger.py`,
   `scoring_config.py` and `nivesh_cli/scorecard.py` stay thin. The command is `rescore` because
   `score` is the existing composite factor score.
4. **Command surface.** `decide` and `rescore` open their own read-write connection (the shared
   reader is query-only); `scorecard` and `calibration` read only. `.claude/commands/scorecard.md`
   names the handler. The `nivesh-memory` MCP server is not built, so nothing here can write
   through MCP.

## Known limits
- Scores are split-adjusted price returns against a price index. Both ignore dividends, so excess
  is roughly consistent but not a total-return result.
- Bar dates differ by a holiday between a security and its index; both use the last bar on or
  before the date.
- A bucket under the floor still carries its numbers in the data but the report shows only
  "not enough data".
- Dedupe can fold two genuinely separate calls that repeat inside the window; the folded count is
  shown so it is visible.
- The release gate is only as good as its cases: any flaky case blocks release by design.

## Contract changes
- Config: additive `scoring:` and `evaluation:` blocks (the examples equal the defaults).
  `review.turnover_limit_pct` now means a calendar-year cap.
- Commands: new `decide`, `rescore`, `scorecard`, `calibration`. `ScorecardInput` and `ScoreRow`
  gain defaulted fields, so existing report fixtures are unchanged. `propose_moves` gains a
  defaulted `ytd_sold_inr` and its last note changes.
- Schema: migration 0008 only (three columns on `ledger_entry`, two new tables); rollback is the
  restore of the pre-migration backup.

## Deferred
- X1: paper model portfolio (next-day-open fills, costs, risk limits, NAV against benchmark, turnover, drawdown, Sharpe, monthly report) (ST-12.5).
- X2: total-return benchmarks (Nifty 500 TRI, S&P 500 TR) and dividend-inclusive scoring.
- X3: record `/research` committee verdicts in the ledger and create a thesis from an acted entry call (ST-8.6).
- X4: the `nivesh-memory` read-only MCP server (get_profile, get_thesis, list_recommendations, get_scorecard).
- X5: LLM-judge qualitative evals, a nightly golden runner and CI wiring, and a point-in-time engine backtest with survivorship-aware constituents (remainder of ST-12.6 and ST-12.7).
- X6: owner sign-off of the scoring defaults (costs, HOLD and AVOID rules, calendar year against Indian financial year for turnover, model cutoffs, bucket floors) and carry-over of #98 (universe floors, replay registration of the universe and shortlist engines, see #79).
