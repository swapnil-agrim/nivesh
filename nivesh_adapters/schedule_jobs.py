"""The jobs the scheduler runs (ST-11.2, ST-11.3, ST-11.7): plain functions of a `JobCtx`.

A job reaches the owner only through the injected `notifier`, never on its own, and a failed
delivery is a note in the result, not a failure of the job. Reports are built by the same
deterministic code as the matching commands.
"""

from collections.abc import Callable
from pathlib import Path

from nivesh_adapters.alerts_service import (
    Evaluation,
    dispatch,
    evaluate_kill_criteria,
    evaluate_mf_changes,
    evaluate_price_monitors,
)
from nivesh_adapters.brief_service import run_brief
from nivesh_adapters.delivery import (
    Message,
    Outcome,
    blocked_reason,
    build_message,
    plain_message,
)
from nivesh_adapters.investright_session import TokenStore, token_path
from nivesh_adapters.mf_report import run_doctor
from nivesh_adapters.report import to_html
from nivesh_adapters.review_service import run_holding_reviews
from nivesh_adapters.run_support import plain_run
from nivesh_adapters.scheduler import (
    NO_CHANNEL,
    JobCtx,
    JobResult,
    Notifier,
    Registry,
    calendars_for,
)
from nivesh_adapters.score_service import score_calls
from nivesh_adapters.stores import open_read_stores, open_sql
from nivesh_core import backup
from nivesh_core.config import Settings
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.paths import find_run_dir
from nivesh_core.pii_scan import scan_text

Job = Callable[[JobCtx], JobResult]


def send(notifier: Notifier, msg: Message) -> str:
    """Deliver and say what happened in a few words; never raises. A message that fails the
    personal-data scan is not handed over."""
    if blocked_reason(msg, page=True):
        return "not delivered (held by the personal-data scan)"
    try:
        outcomes: list[Outcome] = notifier(msg)
    except Exception:  # the text of a transport error can carry an address
        return "not delivered (notice failed)"
    if any(o.status == "sent" for o in outcomes):
        return "delivered"
    return f"not delivered ({NO_CHANNEL if not outcomes else 'channel refused it'})"


def _brief(ctx: JobCtx, notifier: Notifier, market: str) -> JobResult:
    out = run_brief(ctx.settings, ctx.profile, market, ctx.day, clock=ctx.clock)
    where = out.run_dir.relative_to(Path(ctx.settings.data_dir)).as_posix()
    msg = build_message(out.report, to_html(out.report), where)
    return JobResult("ok", f"brief saved; {send(notifier, msg)}", out.run_id)


def brief_in(ctx: JobCtx, notifier: Notifier) -> JobResult:
    """The India brief, saved and sent."""
    return _brief(ctx, notifier, "india")


def brief_us(ctx: JobCtx, notifier: Notifier) -> JobResult:
    """The US pre-open brief, saved and sent."""
    return _brief(ctx, notifier, "us")


def login_nudge(ctx: JobCtx, notifier: Notifier) -> JobResult:
    """One reminder to log in when no broker session is valid; the brief never needs it."""
    if TokenStore(token_path(Path(ctx.settings.data_dir)), clock=lambda: ctx.now).valid():
        return JobResult("ok", "session valid; no reminder needed")
    with open_sql(ctx.settings) as sql:
        held = latest_holdings(sql)
    since = (
        f"the holdings snapshot from {max(h.as_of for h in held).isoformat()}"
        if held
        else "no holdings snapshot (none is loaded)"
    )
    text = (
        f"No valid broker session for {ctx.day.isoformat()}. Log in once to the broker site "
        f"with `nivesh login`. Until then the brief uses {since}."
    )
    return JobResult(
        "ok", f"reminder {send(notifier, plain_message('nivesh: please log in', text))}"
    )


def rescore(ctx: JobCtx) -> JobResult:
    """Score every ledger call that has become measurable; rows are only added."""
    with open_read_stores(ctx.settings, writable_sql=True) as st:
        s = score_calls(st.sql, st.duck, ctx.settings, ctx.day)
    return JobResult("ok", f"scored {s.scored}, insufficient {s.insufficient}, pending {s.pending}")


def backup_job(ctx: JobCtx) -> JobResult:
    """The encrypted backup of the data directory, then removal of archives past retention."""
    cfg = ctx.settings.backup
    if not cfg.target or not cfg.recipient:
        return JobResult("skipped", "backup.target/backup.recipient not configured")
    target = Path(cfg.target)
    dest = backup.create_backup(Path(ctx.settings.data_dir), target, cfg.recipient, ctx.now)
    removed = backup.prune(target, cfg.retention_days, ctx.now)
    return JobResult("ok", f"wrote {dest.name}; pruned {len(removed)}")


NOTE_CHARS = 400


def _alerts_result(ctx: JobCtx, notifier: Notifier, evaluations: list[Evaluation]) -> JobResult:
    """Send and store what fired. When every check was skipped for old data the job is a skip
    that a later tick, inside the grace window, may redo."""
    stale = [e.stale for e in evaluations if e.stale]
    busy = [e for e in evaluations if not e.idle]
    if stale and len(stale) == len(busy):
        return JobResult("skipped", "; ".join(stale), final=False)
    fired = [f for e in evaluations for f in e.fired]
    with open_sql(ctx.settings, writable=True) as sql:
        out = dispatch(sql, fired, ctx.sched, calendars_for(ctx.settings), ctx.now, notifier)
    notes = [n for e in evaluations for n in e.notes] + stale
    tail = f"; skipped: {'; '.join(notes)}"[:NOTE_CHARS] if notes else ""
    return JobResult("ok", f"{len(fired)} fired: {out.line()}{tail}")


def monitors(ctx: JobCtx, notifier: Notifier) -> JobResult:
    """Price and level rules on the stored end-of-day bars of one market."""
    with open_read_stores(ctx.settings) as st:
        ev = evaluate_price_monitors(
            st.sql, st.duck, ctx.settings, ctx.sched, ctx.market, ctx.day, ctx.now
        )
    return _alerts_result(ctx, notifier, [ev])


def kill_criteria(ctx: JobCtx, notifier: Notifier) -> JobResult:
    """Nightly kill criteria of held theses and metadata changes of owned funds."""
    with open_read_stores(ctx.settings) as st:
        kill = evaluate_kill_criteria(
            st.sql, st.duck, ctx.settings, ctx.profile, ctx.sched, ctx.market, ctx.day, ctx.now
        )
        funds = evaluate_mf_changes(st.sql, st.duck, ctx.sched, ctx.day, ctx.now)
    return _alerts_result(ctx, notifier, [kill, funds])


def _save_summary(settings: Settings, run_id: int, lines: list[str]) -> None:
    """The text summary beside the run's own files. A line the scan would flag is withheld."""
    found = find_run_dir(Path(settings.data_dir), run_id)
    if found is not None:
        safe = [x if not scan_text(x) else "[line withheld]" for x in lines]
        (found / "summary.txt").write_text("\n".join(safe) + "\n")


def weekly_review(ctx: JobCtx, notifier: Notifier) -> JobResult:
    """Per-holding reviews (a metered run that stops at the monthly cap), their ledger rows and a
    short summary. Saved, not delivered, unless the schedule says `deliver_holdings`."""
    out = run_holding_reviews(ctx.settings, ctx.profile, ctx.day, clock=ctx.clock, strict=True)
    lines = [
        f"{it.target.symbol} {r.action} {r.confidence}"
        + (f" ({r.reasons[0].code})" if r.reasons else "")
        for it, r in zip(out.items, out.reviews, strict=True)
    ]
    summary = f"{len(out.reviews)} reviewed, {len(out.missing)} without thesis"
    if out.warning:
        summary += f"; {out.warning}"
    if out.run_id:
        _save_summary(ctx.settings, out.run_id, [f"Weekly holdings review {ctx.day}", *lines])
    if ctx.sched.deliver_holdings and lines:
        msg = plain_message("Weekly holdings review", "\n".join(lines))
        summary += f"; {send(notifier, msg)}"
    return JobResult("ok", summary, out.run_id or None)


def monthly_fund_review(ctx: JobCtx, notifier: Notifier) -> JobResult:
    """The fund doctor over held mutual funds: a verdict count, saved beside a plain run."""
    with open_read_stores(ctx.settings) as st:
        rep = run_doctor(st.duck, st.sql, ctx.settings, today=ctx.day)
    lines = [f"{f.facts.amfi_code} {f.facts.name}: {f.verdict.action.value}" for f in rep.funds]
    counts: dict[str, int] = {}
    for f in rep.funds:
        counts[f.verdict.action.value] = counts.get(f.verdict.action.value, 0) + 1
    tally = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    summary = f"{len(rep.funds)} funds reviewed" + (f" ({tally})" if tally else "")
    with plain_run(ctx.settings, "mf doctor", clock=ctx.clock) as m:
        _save_summary(ctx.settings, m.run_id, [f"Monthly fund review {ctx.day}", *lines])
        m.status = "ok"
    if ctx.sched.deliver_holdings and lines:
        msg = plain_message("Monthly fund review", "\n".join(lines))
        summary += f"; {send(notifier, msg)}"
    return JobResult("ok", summary, m.run_id)


def default_registry(notifier: Notifier) -> Registry:
    """Job name to function. `notifier` is the only way a job reaches the owner."""
    return {
        "brief_in": lambda c: brief_in(c, notifier),
        "brief_us": lambda c: brief_us(c, notifier),
        "login_nudge": lambda c: login_nudge(c, notifier),
        "monitors": lambda c: monitors(c, notifier),
        "monitors_us": lambda c: monitors(c, notifier),
        "kill_criteria": lambda c: kill_criteria(c, notifier),
        "rescore": rescore,
        "backup": backup_job,
        "weekly_review": lambda c: weekly_review(c, notifier),
        "monthly_fund_review": lambda c: monthly_fund_review(c, notifier),
    }
