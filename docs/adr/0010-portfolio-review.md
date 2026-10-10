# ADR-0010: Portfolio review and sell discipline (E8)

Status: accepted. E8 adds a stored investment thesis per equity holding, a holding review that
an agent drafts and code floors, a per-lot tax estimate and an asset-class rebalancing proposal
(ST-8.1 to ST-8.4). ST-8.5 to ST-8.7 are deferred (X1 to X3). Agents see only read-only tools and
are tested against the scripted fake SDK (no network, no model call, no personal data); the
engines are deterministic Decimal code with injected dates. There is **no new MCP server**, no
new allow-list entry and no new dependency. Nothing here is advice; every tax figure is an
"estimate from your config; not tax advice".

## Owner decisions (safest option chosen; owner sign-off is X9)
- **OD-1 Tax config and precedence.** A new `tax.india` block (`long_term_days`,
  `short_rate_pct`, `long_rate_pct`, `ltcg_exemption_inr`) is all unset by default: no rate, day
  count, exemption, cess or surcharge lives in code. Precedence is per asset, never merged: Indian
  direct equity and ETF lots use `tax.india`, mutual-fund lots use `mf.tax`, US lots use
  `tax.us_long_term_days` for the long-term flag only and their tax is always unavailable ("US tax
  not modelled"). `Profile.tax_rates` stays in the profile, is never read (a test greps for it)
  and is marked legacy in `config/profile.yaml`. An unset rate gives gain and days only, with a
  reason. Tax per lot is on the positive gain only (losses are not set off, which overstates
  rather than understates); the exemption is applied once per scenario (one holding or one trim)
  assuming no other long-term gains are realised in the year. Every `long_term_days` (`tax.india`,
  `mf.tax`, `tax.us_long_term_days`) is compared with `>=`: a lot is long-term once its days held
  reach the value. India's rule is "more than 12 months", so set 366 for strictly more than 365.
- **OD-2 HoldingReview vocabulary.** `schema_version` is `2` and `action` is upper case `HOLD |
  ADD | TRIM | EXIT | REVIEW`. v1 is replaced, not kept beside it: it had no producer or consumer
  (contract change, see below).
- **OD-3 Indian direct equity has no dated lots.** The tax engine reports "holding period
  unavailable: no dated lots" rather than a guessed date or an average-cost lot (X4).
- **OD-4 Turnover cap per proposal.** `review.turnover_limit_pct` (30) caps one rebalance
  proposal; annual turnover needs the ledger (X5). Output says "per-proposal cap; annual turnover
  not tracked yet".
- **OD-5 Scope.** Thesis and HoldingReview cover equity and ETF holdings (India and US); mutual
  funds stay with the fund doctor (X7) and the CLI says so. Rebalancing covers every holding with
  an INR value; a row without one (USD with no rate) is left out with a note.
- **OD-6 Reviewer data access.** `holding_review` may call `mcp__engine__fa_compute`,
  `ta_compute`, `valuation_range` and `red_flags` only (no holdings-derived tool, no
  `mcp__holdings__*`); `thesis_draft` has no tools and no servers. Weights, lots, tax and trigger
  results reach the agent as code-computed JSON in the prompt.
- **OD-7 Override semantics.** An agent-supplied `override_reason` can never produce HOLD or ADD
  when a kill criterion is met or rule 1 or 4 is triggered: it can only lift TRIM to REVIEW, and
  the change is recorded in `overrides`. Keeping HOLD over a met criterion is an owner decision
  that belongs to `/decide` (X2). A met criterion without an override therefore yields TRIM or
  EXIT, never HOLD.
- **OD-8 Thesis history.** Append-only rows with `status` active, superseded or closed and a
  unique partial index allowing one active thesis per security. This goal writes only `active`
  rows with `source = 'onboarding'`; the supersede path and `source = 'decision'` are X2.

## Decisions
1. **Thesis model and store (D-1, D-9).** `nivesh_core/thesis.py` (strict pydantic): horizon
   `positional_1_6m | long_term_1y_plus`, why-own of at most 60 words, 2 to 4 kill criteria of
   at most 40 words each with unique ids, at least one machine-checkable (`metric`, `comparator`,
   `threshold` all or none), review date = created + `review.default_review_days` (90).
   `KillCriterion` is shared with the `ThesisDraft` schema. Migration `0006_thesis.sql` adds the
   `thesis` table and the index `thesis_one_active`; Decimal thresholds are stored as strings.
2. **Migration notes.** The migration is forward-only: a new table and index, no rebuild of an existing table, no
   data copied, version 5 to 6 in one transaction by the existing runner. Backfill: none (no
   prior thesis data; onboarding creates rows). Rollback: the runner refuses to open a store newer
   than the code, so a code rollback after the upgrade means `nivesh restore` of the pre-upgrade
   backup; the manual alternative (`DROP TABLE thesis; DELETE FROM schema_version WHERE version =
   6`) is for the owner, not automated. Backup and restore carry the table (tested).
3. **Schemas (D-2).** `ThesisDraft` (new, `schema_version` 1, evidence must point into the
   analyst views it was given) and `HoldingReview` v2 with `criteria` (each `met`, `not_met` or
   `unknown`; an agent's `met` or `not_met` needs evidence, a code-judged entry is marked
   `judged_by: code`), code-filled `triggers`, `tax_note` and `overrides`, `override_reason`,
   `thesis_status`, `confidence` and at least one reason. JSON files are generated from
   `nivesh_agents/schemas.py` and drift-tested.
4. **Discipline rules in code (D-4).** `nivesh_engine/review_rules.py` evaluates the six PID 15.6
   triggers: `kill_criterion`, `fundamental_deterioration` (each of the last
   `deterioration_quarters` quarters vs prior quarter for revenue growth and operating margin; no
   plan figures are stored), `valuation_stretch` (own-history percentile of the configured
   multiple over `valuation_percentile_min` with revisions not rising), `concentration` (position
   over `max_position_pct` or sector over `max_sector_pct`), `below_sma200_weak_rs` (positional
   horizon only: `below_sma200_sessions` sessions under the 200-day average with falling relative
   strength) and `better_use_of_capital` (always `not_evaluable` until idea runs exist, X3).
   Missing input is `not_evaluable` with a reason, never clear. A criterion with a machine metric
   is judged by code when the value exists (code wins and a disagreement is logged); without a
   value an agent `met` stands and an agent `not_met` becomes `unknown`.
5. **Action floor.** `action_floor` uses the ladder `ADD > HOLD > REVIEW > TRIM > EXIT` and can
   only lower the agent's proposal: a met criterion or triggered rule 1 or 4 caps at TRIM (REVIEW
   with an `override_reason`, OD-7); triggered rules 2, 3 or 5, or a passed review date, cap at
   REVIEW. An exhaustive table test covers every action, criterion status, trigger set and
   override. A failed reviewer gives REVIEW, low confidence and the reason
   `reviewer_unavailable`, and the floor still applies.
6. **Tax lots (D-3).** `nivesh_engine/tax_lots.py`: per lot holding days, long-term status, days
   to long-term, gain, tax now and tax at long-term eligibility at the same price, and the saving.
   `cheapest_lots` ranks loss lots first, then lowest estimated tax per unit, then oldest; for
   mutual funds FIFO is assumed (oldest units first) and the note states the tax difference
   against the unconstrained ranking. When the final review action is TRIM, the review's tax note
   adds the lowest-tax lots for the excess over `max_position_pct`.
7. **Rebalancing (D-5).** `nivesh_engine/rebalance.py` works at asset-class level with the X-ray
   class mapping. A class is out of band when it differs from target by more than `band_pp` (5;
   exactly the band is inside). Sequence: new cash to under-weight classes in proportion to their
   shortfall; then each over-weight class is reduced only to its **band edge**, first from
   holdings a review run flagged EXIT, then TRIM, then the rest by lowest estimated tax per rupee
   (unknown tax last, noted); proceeds go to under-weight classes. Turnover = reductions / the
   portfolio value before the proposal, stopped at the cap with a partial last move and a note of
   what stays out of band. Deviation from the plan: an EXIT-flagged holding is also reduced only
   up to the band edge (minimum turnover); the rest of the exit is the review's call, not the
   rebalance's. Output is a proposal only (`Move` kinds `add` and `reduce`).
8. **Agents (D-6).** `thesis_draft` (tier top, no tools, one turn) drafts from the fundamental,
   technical and news analyst views run first; `holding_review` (tier top, OD-6 tools, eight
   turns). Both use `run_agent` (one repair, `GUARD` system prompt, versioned prompts under
   `prompts/<agent>/v1.md`). Outputs go to the run directory as `thesis_draft` and
   `holding_review` kinds, never through `redact_json`. Agent modules import no review glue: facts
   are built in the CLI and passed in.
9. **CLI and run lifecycle (D-7, D-8).** `nivesh thesis onboard [--only SYMBOL]`, `thesis list`,
   `thesis show SYMBOL`, `nivesh review holdings [--only SYMBOL] [--json]`, `review tax SYMBOL
   [--trim QTY] [--json] [--as-of]`, `review rebalance [--cash INR] [--review-run RUN_ID]
   [--json]`. `thesis onboard` and `review holdings` are metered runs at tier quick through the
   extracted `metered_run` (the same gate, run row and trace sequence as `nivesh run`, whose
   behaviour is unchanged); the budget gate refuses only `deep`, so past the cost cap quick shows
   a warning and is not refused. The other commands spend nothing and open the stores read-only;
   only `thesis onboard` writes theses, after the owner accepts or edits each draft.
   `--review-run` reads `data/runs/<id>/outputs/*_holding_review_*.json` (path built from the
   integer only) and skips files that do not validate, with a note.

## Contract changes
- `HoldingReview` v1 to v2 is breaking (actions renamed, fields added and removed) with no
  producer or consumer; `ThesisDraft` is new.
- `review:` and `tax.india` config blocks are additive and optional.
- The `agents.tiers` map grows by `thesis_draft` and `holding_review`: an owner config that lists
  `agents.tiers` explicitly must add both (the validator requires the exact set).

## Deferred (each gets a follow-up issue)
- X1: ST-8.5 `/review-portfolio` end to end with the ST-10.2 report template.
- X2: ST-8.6 `/decide`, thesis from an acted decision (`source = 'decision'`, supersede path) and
  the owner route to keep HOLD over a met kill criterion; needs the ledger (ST-12.1).
- X3: ST-8.7 better use of capital and rule 6 (needs idea runs, ST-9.4).
- X4: dated lots for Indian direct equity (broker trade history or a lot CSV).
- X5: annual (year-to-date) turnover from the ledger (OBJ-9).
- X6: live checks of drafting and review with a real SDK (harness only here).
- X7: mutual-fund holdings review stays with the fund doctor (#59, #65).
- X8: replay registration of `tax_lots`, `review_rules` and `rebalance` (#79).
- X9: owner sign-off of the defaults (95th percentile on pe over 5 years, 60 sessions, 2 quarters
  vs prior quarter, 90-day review, +-5pp band to the band edge, 30% per-proposal turnover, OD-7,
  `tax.india` all unset, exemption once, no loss set-off).
