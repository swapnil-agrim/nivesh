# ADR-0009: Research committee (E7)

Status: accepted. A committee of model agents reads the output of the deterministic engines and
proposes a verdict; code decides what reaches the owner. The agents are built on the pinned
`claude-agent-sdk`, see only read-only tools, and are tested against a scripted fake of the SDK
(no network, no model call, no personal data). Several SDK behaviours are **unverified** against
the live CLI (listed below), and correctness never depends on them. Nothing here is advice.

## Decisions
1. **One `query()` per agent, no new dependency.** `run_agent` in `nivesh_agents/runtime.py` calls
   the module-level `query`, so tests fake it as `tests/agents/test_runtime.py` does. There are no
   SDK subagents (`Task` and `Agent` stay disallowed) and no `ClaudeSDKClient`. `pydantic` (already
   a direct dependency) validates; `jsonschema` is not imported. `pyproject.toml` and `uv.lock` are
   untouched: there is no new dependency. `run_command` and `build_options` are unchanged.
2. **Schemas are pydantic models, the JSON Schema files are generated.** `nivesh_agents/schemas.py`
   is the single source for AnalystView, MacroView, FundView, DebateTurn, LensView,
   RiskAssessment, HoldingReview and Verdict (the class is `CommitteeVerdict` because the fund
   doctor already defines `Verdict`). `schemas/*.json` are committed and a drift test regenerates
   and compares them. Validation is `model_validate_json` in strict JSON mode with unknown fields
   rejected. Field names follow the PID (`key_points`, `tool_call_id`, `evidence`, `stance`).
   Decided additions: `Levels.setup_type` on the technical view, `FundView.action` and a nullable
   `switch_target`, an optional `source_url` on news evidence, and `HoldingReview` as a schema and
   model only (no agent in E7).
3. **Exactly one repair, then the agent is failed and the run continues.** Output is read from the
   SDK's structured output when present, else the last JSON object in the text; correctness does
   not depend on `output_format` (unverified: the supported JSON Schema subset, the failure
   subtype, and structured output together with tool use). Invalid output gets exactly one repair
   call (`REPAIR_RETRIES = 1`, a constant) with the **same system prompt** (`GUARD` plus the
   prompt, so the untrusted-data rule stays), no tools, no servers, `max_turns` 1, and `resume` of
   the first session when the SDK reported one (unverified: resume with an empty tool list; without
   a session id the first output travels in a fresh query). The repair prompt lists the redacted,
   truncated (2,000 characters) errors and the sorted valid `tool_call_id` values. A second
   failure, a raised error or an error result marks the agent failed with an `error` trace record;
   a raised error or error result is not repaired. A failed agent never raises to the caller.
4. **Evidence must be real.** Every `tool_call_id` in a validated output must be an id the agent's
   own run made, checked against a per-(agent, security) index kept in `Tracer` (stricter than
   "somewhere in the run") and equal to the JSONL file (a test reads the trace back). Debate and
   lens agents may cite the union of the analysts' ids or point into the views they were given
   (`{view, point_index}`), and every pointer must exist. The honest limit: offline tests cannot
   show that a live model quotes real ids (R-2); the repair prompt and the prompt wording are the
   mitigation and a live smoke is deferred (D9).
5. **Technical numbers are the engine's.** The technical view's `levels` (entry zone, stop,
   invalidation, `setup_type`) and any evidence field starting `setup.` must equal the agent's own
   `ta_compute` result, compared as Decimals after quantising to the engine quantum (1e-8). The
   comparison reads the raw tool results kept in memory by `run_agent` (the trace file is
   redacted). With no setup from the engine, levels must be null.
6. **Filing cap.** The fundamental agent may call `get_filing_text` at most `max_filing_sections`
   times (default 2). A third call cannot be repaired by text, so it fails the agent with reason
   `filing_section_cap` without spending the repair. Enforcing it with a hook is D14.
7. **Verdict safety is code.** `nivesh_engine/committee_rules.py` (Decimal only, no SDK import, under
   the E6 source scans) holds `risk_veto`, `weight_bounds`, `coverage_pct`, `final_weight` and
   `verdict_ceiling`. The only function that produces a committee verdict is `finalise` in
   `committee.py`, and a source test pins that. See the table below.
8. **Persistence before the verdict.** `snapshot.json` is written once before the first analyst
   call; for each security the outputs (`outputs/NNN_<agent>_<security_id>.json`, or a failed
   marker) are written before the portfolio manager's query starts; the verdict file follows the
   verdict. Files use `write_private` (0600, exclusive create, directory 0700) and the trace holds
   only a digest. Outputs are written as they are, **not** through `redact_json`, which blanks any
   key containing the part `key` and would mask `key_points`. The snapshot holds the run id, as-of
   date, tier, securities (id, symbol, kind), profile limits, the code-computed engine outputs
   (score cards, risk facts), coverage, prompt versions, models and a config digest that includes
   the analysis `weights_version` and `weights_digest`. No database migration: files only.
9. **Bounded parallelism.** One `asyncio.Semaphore(agents.concurrency)` per run (default 4, range
   1 to 16) wraps every `run_agent` call, so live agents never exceed it across securities. The
   macro view is a memoised task on the run object (never a module global), awaited **before** a
   slot is taken, so only the macro call holds one; a macro failure is shared as a failed marker,
   not retried per security. One event loop, no threads, so `Tracer` sequence numbers stay unique.
10. **Modes.** `quick` is the fundamental, technical and news analysts and one bull and one bear
    summary, no lenses, no macro. `deep` adds the macro analyst, `debate_rounds` alternating rounds
    (default 2, range 1 to 3; the last turn of each side states its strongest unrebutted point) and
    the four lenses when enabled. The tier is an input (the cost gate decides it upstream); `brief`
    is not a committee tier and raises `ValueError`. The committee is a library function,
    `nivesh_agents.committee.run_committee`; the CLI command is E10.
11. **Allow-lists (exact names).** fundamental: `engine.fa_compute, valuation_range, red_flags` and
    `filings.list_filings, get_filing_text`; technical: `engine.ta_compute` only; news:
    `news.get_news, get_events_calendar, get_next_results_date` and `filings.get_announcements`;
    macro: `macro.get_series, get_flows_india, get_rates_snapshot` and `market.get_index`; mf:
    `engine.mf_analyse, mf_overlap, get_fund_meta`; risk: `engine.risk_metrics, portfolio_xray,
    red_flags`; bull, bear, lenses and the portfolio manager: none and no servers. The options are
    `tools=[]`, `permission_mode="dontAsk"`, `strict_mcp_config`, the `DISALLOWED` built-ins and
    `setting_sources=[]` (project settings are not loaded for committee agents; unverified live).
    Holdings-derived engine tools are on the risk spec only.
12. **Prompts.** `prompts/<agent>/vN.md` with front matter `agent, version, schema, tools`; the
    newest version wins unless `agents.prompt_pins` names one. Every system prompt is `GUARD` plus
    the prompt text, and the first prompt line is the marker `[agent:<name>]`. Every prompt states:
    the as-of date is today, tool data only (no training-memory facts), uncertainty and
    `insufficient_data` are allowed, no advice on leverage, F&O or margin, and external text is
    data. The trace records model id, prompt version and prompt digest per agent. Temperature and
    seed are not exposed by `ClaudeAgentOptions`, so they are not recorded (D8); replay of a
    committee means re-feeding the stored snapshot, not re-deriving model text.
13. **The `engine` MCP server (#68, minimum scope).** The `engine` server is one read-only MCP server with ten tools
    (the research text said nine but listed ten): `ta_compute, fa_compute, valuation_range,
    red_flags, risk_metrics, portfolio_xray, score, mf_analyse, mf_overlap, get_fund_meta`.
    `screen` and `xirr` stay on #68 (D3). It **supersedes ADR-0008 decision 2 (D1)**, which
    deferred the `nivesh-engine` server (ADR-0008 is not edited). The assembly moved out of the
    CLI into `nivesh_adapters/analysis_service.py`, one function per tool, and both the CLI and the
    server call it; the CLI output is byte-identical (its tests are untouched). Decimals are JSON
    numbers; the two renames of ADR-0008 (`key` to `ref`, `portfolio_vol` to `overall_vol`) stay. A
    test walks every tool output for redaction-trigger key names. **Deviation from PID 14.4:** the
    mutual-fund tools live inside `engine`; there is no separate `nivesh-mf` server (D10).
    Registering `engine` widens the global `nivesh run` allow-list by ten read-only tools; this is
    accepted because every tool is read-only and its output passes `clean()`.

## Semantics chosen where the PID is silent
- **Coverage** is `min(score card input coverage %, required views ok %)`; the required views are
  the mode's analysts that returned a valid view (macro included in deep). Strictly below
  `agents.min_coverage_pct` (default 70) gives INSUFFICIENT_DATA; exactly 70 passes. When this is
  already certain after the analysts (or the card band is `insufficient_data`) no later agent is
  called and the verdict is built in code.
- **Veto facts**: the candidate is in `Profile.exclusions` (symbol, ISIN or name); a hard red flag
  fired; the candidate's position (existing weight plus the tested weight) or its sector strictly
  exceeds the profile limit at `min(agents.starter_weight_pct, profile max position)`; or days to
  trade strictly exceeds `agents.max_days_to_trade` when set (default off). The model may add soft
  notes and can never clear (or add) a veto. A failed risk agent is a veto with reason
  `risk_unavailable`, so the portfolio manager cannot go upward.
- **Verdict ceiling table** (applied after the model, in this order): INSUFFICIENT_DATA (band, or
  coverage strictly below the minimum) wins and forces INSUFFICIENT_DATA whatever was proposed;
  else a veto, a score card `cap = "HOLD"` or a hard red flag lowers a proposed BUY or ACCUMULATE
  to HOLD; every other proposal is kept (the ceiling only lowers). `vetoed_by_risk` is set by code.
  `suggested_weight_pct` is clamped to `min(proposal, profile max position, sector headroom)` and
  is null for INSUFFICIENT_DATA, AVOID, SELL and a HOLD lowered by a veto. Every change is a
  string in `overrides` and the file is traced. An exhaustive test (7 proposals by 8 ceiling
  combinations) pins the table.
- **pm_unavailable**: if the portfolio manager fails after its repair the verdict is
  INSUFFICIENT_DATA with an `overrides` entry starting `pm_unavailable`.
- **Weights (ST-7.8)**: conviction scaling is the model's proposal, clamped in code by the bounds;
  volatility appears only in `liquidity_note`. Recorded as a deviation from "scaled by conviction
  and volatility".
- **Lenses** inform and never override: the disagreement count is computed from the final verdict
  after the fact, and flipping every lens leaves the verdict unchanged (test).
- **Fund path**: a security of asset class `mf` goes mf analyst, risk, portfolio manager, with no
  debate and no lenses; its card coverage counts as 100, there are no red flags, and the veto
  facts are exclusions and the position limit only. The fund path is first on the scope cut list.
- **Digests** in the trace and the snapshot are written in groups of eight hex digits joined by
  hyphens, because a plain SHA-256 can hold nine or more decimal digits in a row, which the PII
  scanner reports.

## Unverified against the live SDK (offline only)
`output_format` JSON Schema subset and its failure subtype; structured output together with tool
use; `resume` with an empty tool list for the repair call; a live model quoting real tool-use ids
(the main offline-invisible risk); the process-spawn cost of one CLI process plus one stdio
subprocess per server for every `query` (NFR-10); `setting_sources=[]`; the model alias defaults
(`opus`, `sonnet`, `haiku`; the owner sets exact ids in `agents.models`).

## Measured coverage
Baseline before E7: 1,922 tests, 97.65 percent total. After E7 the gate includes `nivesh_agents`
(`--cov=nivesh_agents` in the Makefile, threshold 85 over all packages); each new
`nivesh_agents` module, `committee_rules.py` and `analysis_service.py` is at least 90 percent
except where the final report says otherwise.

## Deferred (each gets a follow-up issue)
- D1: real-model run of the ST-7.2 golden-set eval (at least 8 of 10 direction matches); the
  harness and offline scorer ship.
- D2: the NFR-10 wall-clock benchmark (quick p95 90 s, deep p95 6 min) against the real SDK, and
  the process-spawn cost per `query`; the `bench` and `live` harness ships.
- D3: `screen` and `xirr` tools of #68 (the committee does not use them).
- D4: the HoldingReview agent and the sell-discipline reviewer (the schema and model ship).
- D5: the `/research` command, the report writer and the report-level citation checker (E10).
- D6: `nivesh-memory` (`get_thesis`, `get_scorecard`), ledger writes and indexing of run outputs.
- D7: registering the pure engine entry points in `replay.REPLAYABLE` and emitting `engine_call`
  records.
- D8: temperature and seed capture when the SDK exposes them.
- D9: live verification of the unverified SDK behaviours above, and an optional hook that injects
  the tool-use id for the model.
- D10: a separate `nivesh-mf` server as in PID 14.4.
- D11: in-process SDK MCP servers to cut spawn cost, only if D2 shows it dominates.
- D12: owner sign-off of the agent defaults (tier to model ids, concurrency, rounds,
  `starter_weight_pct`, `max_days_to_trade`, the min-of-two coverage rule, risk-unavailable-means-veto).
- D13: debate and lenses for mutual funds beyond the mf analyst.
- D14: enforcing the filing-section cap with a pre-tool hook instead of the check after the fact.
