# Running the scheduled jobs

Nivesh runs its unattended jobs (daily brief, login nudge, price and level monitors, nightly
thesis checks, weekly scoring and review, backup) from one command that something else calls
every five minutes:

    nivesh schedule run

Nothing is installed by Nivesh. The files in `docs/deploy/examples/` are text examples only; you
copy and enable the one that fits your machine. The jobs, their times (IST) and every number they
use are in `config/schedule.yaml`.

## How a tick behaves
- Idempotent. A job runs once per (job, trading date), recorded in the `job_run` table. A second
  tick, or two overlapping ticks, find the record and leave the job alone.
- Trading days. India jobs use the NSE calendar, US jobs the NYSE calendar. A job that needs a
  market day does not run on a weekend or a listed holiday. If the NSE holiday list for the year
  is missing, the job is skipped and you get one notice to complete `market.nse_holidays` in
  `config/nivesh.yaml`. `nivesh schedule status` warns while the list has fewer than
  `min_holidays_warn` dates.
- Retries. A failing job is tried up to 3 times in all, waiting 30 s and then 120 s. After that
  one failure notice goes to your own delivery channels (`delivery.channels`). A spent monthly
  cost cap is final at once and is not retried.
- Late and missed. A job later than its grace window is stored and shown as LATE. A job later
  than its `catchup_hours` is skipped as a "missed window": a laptop that wakes at night does not
  send the 08:00 brief.
- Time. Stored times are UTC and shown times are IST. The US pre-open brief is 30 minutes before
  the New York open, so it moves by an hour when US summer time starts and ends (18:30 IST in
  summer, 19:30 IST in winter).
- Data. No ingest job ships yet. The monitors, nightly checks and fund checks skip, with a line in
  `nivesh schedule status`, when the stored prices or fund data are older than the last session.
  Run your ingest before 17:30 IST until a scheduled ingest exists.
- Nothing spends money by surprise. The weekly review is the only job that calls a model; it stops
  at the monthly cost cap and sends one notice.

## Dry run and status

    nivesh schedule run --dry-run --now 2026-10-09T08:00:00+05:30
    nivesh schedule status

## Choosing a scheduler
Pick one. Each example calls the same command every 5 minutes. Replace `/opt/nivesh` with your
install directory.

- cron: `examples/nivesh-schedule.cron`
- systemd: `examples/nivesh-schedule.service` and `examples/nivesh-schedule.timer`
- launchd (macOS): `examples/com.nivesh.schedule.plist`
- A scheduled task of your own tool works too, if it can run the command above.

The container image does not run a scheduler yet; that is tracked separately.

## Owner actions
- Complete the NSE holiday list for each year in `config/nivesh.yaml`.
- Set numeric invalidation levels with `nivesh schedule invalidation SYMBOL LEVEL`. The invalidation
  monitor does nothing until you do.
- Configure `delivery.channels` and, for backups, `backup.target` and `backup.recipient`.
