# ADR-0014: Scheduling and alerts (E11)

Status: accepted. E11 adds a headless scheduler that runs the daily brief, a login reminder, price
and level monitors, nightly kill-criteria and fund checks, weekly scoring, a weekly holdings review,
a monthly fund review and a backup (ST-11.1 to ST-11.4, ST-11.6, ST-11.7), and it closes #97
(an alert when a watched price enters its entry zone). It adds migration 0009 (forward-only,
additive: `job_run`, `alert`, `invalidation_level`), the `nivesh schedule` group
(`run`, `status`, `invalidation`) and the `nivesh alerts` command, `config/schedule.yaml`, text
examples for cron, systemd and launchd, **no MCP change** (the tool servers stay read-only), no
agent, prompt or schema change and no new dependency. Nothing is installed by Nivesh: the deploy
files are examples and nothing is installed from them. No test uses the network, a real clock, a
real notification or personal data. ST-11.5 is deferred (X1). Alerts and summaries are the owner's
own information, not investment advice.

## Decisions
- **D1 Entry point.** One idempotent tick, `nivesh schedule run [--now ISO] [--dry-run]`, called
  every 5 minutes by cron, systemd or launchd. A job is due when its instant for a trading date
  has passed and no finished `job_run` row exists. Overlapping ticks are safe: the claim is a
  primary-key insert inside `BEGIN IMMEDIATE`, committed before the job starts. Jobs are in-process
  functions of a `JobCtx`, never subprocesses or Typer commands.
- **D2 Idempotency.** The key is (job, trading date). A job that ended `ok`, `skipped` or `failed`
  is not run again for that date. A `running` row older than the job's `stale_minutes` (60; 240 for
  the two reviews, so a long metered review is not started twice) is reclaimed once and counts as an
  attempt. `job_run` is mutable (status changes); `alert` and `invalidation_level` are append-only
  and the database refuses update, delete and replace.
- **D3 Retries (NFR-12).** Three attempts in all, waiting 30 s then 120 s (`schedule.yaml`:
  `retries`, `backoff_s`) through an injected sleep. `BudgetRefused` and `CalendarUnknown` are final
  at once; everything else is retried, including the "market database in use" case (DuckDB has one
  writer), which ends as a failure with that reason and no later-tick retry. A final failure sends
  one notice through the delivery adapters with the type of the error and a redacted, path-free
  text; the personal-data scan still gates every channel. `notified` is 1 only when a channel took
  the notice; with no channel the row says "no channel configured".
- **D4 Calendars (NFR-19).** India jobs use the NSE calendar (also BSE), US jobs the NYSE calendar,
  both from `nivesh_engine.calendar`. A year with no NSE holiday list raises `CalendarUnknown`: the
  job is skipped with the reason and **one** notice is sent per gap, never a guess from weekdays.
  The shipped holiday list covers 2024-2026 and is built in (united per year with the configured
  `nse_holidays`), so a fresh install has data; from 2027 the owner extends it yearly. For a listed
  year `schedule status` warns while the list is shorter than `min_holidays_warn` (8), and India jobs
  may run on an unlisted holiday until the owner completes it (X5).
- **D5 Grace, late and missed window.** A job later than `grace_minutes` is stored late and shown as
  LATE. A job later than its own `catchup_hours` (3 for the brief, 2 for the reminder, 15 for the
  Saturday jobs) is skipped as a **missed window** instead of run, so a laptop that wakes at 22:00
  does not send the 08:00 brief. A missed window is recorded only on the IST date it happened.
- **D6 US pre-open.** US summer time is computed by rule, with no time-zone database: it runs from
  the **second Sunday** of March to the first Sunday of November (years from 2007). The brief runs
  30 minutes (`us_lead_min`) before the 09:30 New York open, which is 18:30 IST in summer and 19:30
  IST in winter; `at` in the yaml is informational for that job. The US monitors run at 05:30 IST
  the morning after the session (`day_offset: 1`), so Friday's session is covered on Saturday.
- **D7 Reviews, narrowed.** The weekly review (Saturday 09:00 IST) is the per-holding review
  service extracted from `review holdings` (a metered run at tier quick that stops at the monthly
  cap), its ledger rows, and a text summary beside the run (symbol, action, confidence, reason
  code; no quantities or values). The monthly fund review (first Saturday) is the fund doctor over
  held funds, summarised the same way. Both are **saved, not delivered**, unless `deliver_holdings`
  is true in `schedule.yaml`. The rebalance leg and the full report templates are deferred (X8).
- **D8 Numeric invalidation.** No price field existed, so `invalidation_level` stores an owner-set
  level per security (latest row wins, `nivesh schedule invalidation SYMBOL LEVEL`). The rule
  `close_below_invalidation` **does nothing until** the owner sets levels; securities without one
  are listed as skipped in `schedule status`. Nothing is guessed from the entry zone.
- **D9 Monitors are end-of-day** (intraday is X2). Big move: today's close against the standard
  deviation of the 20 daily returns before today (22 closes needed; today is left out of its own
  baseline); fires strictly above 2.5 times (`alerts.vol_mult`). 52-week break: a close beyond the
  252-session high or low on volume above 1.5 times the 20-day average. Watch zone: a close inside
  the entry zone after a close outside it, so a stay does not repeat (#97).
- **D10 Alert content.** Each alert stores what happened, the current ledger verdict ("no verdict
  yet" when none), a fixed next step per rule and the price levels. Text carries the symbol, the
  verdict and percentages or price levels only, never quantities or values. `scan_text` runs on every
  outgoing alert and digest; a finding keeps it from every channel and the alert is still stored
  as not sent, with the kinds of finding.
- **D11 Hygiene (ST-11.7).** The same rule on the same security goes out at most once per 5
  sessions of that security's market (sessions, not days; a weekday count with a note replaces a
  session count across a year with no holiday list). Only alerts that actually went out count as
  history, so a held-back or unsent alert never pushes the next one away. When more than 3 alerts
  would go out inside one rolling hour they go as one digest. A **daily cap** of 8 per IST day holds
  the rest back; held-back counts are told once, at the front of the next day's first message or, if
  nothing else fires, in a short message of their own.
- **D12 Kill criteria** (ST-11.6) run nightly for held equity and ETF positions with an active
  thesis. The portfolio X-ray is built once; each security's facts come from the review code, with no
  agent. A met machine criterion alerts (several on one security become one alert); a missing metric
  is reported as skipped, never as an alert. Only India holdings are scheduled for now.
- **D13 Fund changes** (ST-11.6): manager, category, expense ratio moved by more than 0.2 points
  and a merger signal, for owned funds, from `fund_meta_pair` (the two newest metadata dates). The
  merger signal is a proxy (a changed scheme code, or a name or source that says merged) and the
  alert says it is **best effort**. A metadata date alerts once. Style drift stays X4.
- **D14 Login reminder** (ST-11.3). At 08:45 IST on NSE sessions, if no broker session is valid
  for the IST day, one reminder says to log in and which holdings snapshot the brief will use.
  The brief and every other job work without a session and state the snapshot date.
- **D15 Persistence.** `alert(alert_id, rule, security_id, fired_at, trading_date, payload,
  delivered 0|1|2, digest_id)`. The send happens first and the row is inserted with its outcome,
  so a stored "delivered" is never a claim that did not happen. **Accepted gap:** a crash between
  the send and the insert leaves an alert that was sent but not recorded; the next run may send it
  again, because the history does not see it. A stored `delivered = 0` alert may have been
  held back, blocked or unsent.
- **D16 Configuration.** A separate `config/schedule.yaml` read through `ScheduleSettings`
  (`extra="forbid"`), not part of `Settings`; a missing file means the defaults, and a test pins
  the committed file to them. Every time, count and threshold the scheduler uses is a parameter.
- **D17 Stale data guard.** There is **no ingest job** yet (X9). Data-dependent jobs check first:
  the newest stored bar must reach the last finished session of the market (**bars stale**), the
  newest fund metadata must be at most `mf_stale_days` old (**fund meta stale**). A stale job is
  skipped with its reason and no notice. The skip is not final until the grace window ends, so a
  later tick can look again once the owner's ingest has run; after that the date is closed. The
  owner must ingest before 17:30 IST until X9 exists.
- **D18 Shared run helpers.** `plain_run`, `metered_run` and `held_securities` moved from
  `nivesh_cli` to `nivesh_adapters` with no `typer` import (a test pins this for every package
  below the CLI). They take an injected clock and warning function. `metered_run` raises
  `BudgetRefused` before any run row exists; the CLI wrappers keep their names, output and exit
  codes. For unattended use a `strict` flag also refuses a quick run once the cap is reached,
  because the gate alone only warns for quick runs.
- **D19 Runner timing.** The claim is committed before the job runs and no transaction is held
  while the job opens its own connections. The tick's instant is the job's clock, so run rows and
  run directories follow the injected time in tests.

## Deferred
- X1: results-day detection and the deep re-run with a what-changed comparison (ST-11.5); no India
  results-date detector exists.
- X2: intraday last-traded-price monitors (they need a valid broker session).
- X3: scheduler service in docker-compose, nightly backup wiring and report-store restore (#21).
- X4: style drift and exit-load change detection, and an explicit merger field in `fund_meta` (#65).
- X5: complete the NSE holiday list (owner data).
- X6: live verification of scheduled runs and notices (with #38 and #77).
- X7: `/review-portfolio` and `/review-funds` command files (#83).
- X8: weekly rebalance summary and the full portfolio and fund report templates.
- X9: a scheduled ingest job (prices, fund metadata) before the monitors.

## Consequences
- All stored times are UTC and all shown times are IST; the trading date is the market-local session
  date.
- The shipped holiday list is incomplete and the invalidation monitor is idle until levels are set;
  both are said plainly in `schedule status` and in the pull request.
- Reviews spend on a model only when the weekly job runs, and stop at the monthly cap.
- Rollback of the migration is a restore of the pre-migration backup; the tables are additive.
