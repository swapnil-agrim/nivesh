"""Alerts from stored data (ST-11.4, ST-11.6): evaluate the rules for what the owner holds and
watches, then decide, store and send. Read-only on the market data; the only rows written are
the alerts themselves. A check whose data is older than the last finished session is skipped
and said so, never run on stale numbers.
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb

from nivesh_adapters.analysis_data import load_bars
from nivesh_adapters.analysis_service import xray_report
from nivesh_adapters.delivery import Message, Outcome, blocked_reason, plain_message
from nivesh_adapters.holdings_service import held_securities
from nivesh_adapters.market_ingest import calendar_for
from nivesh_adapters.review_service import holdings_of, review_facts
from nivesh_core.alerts import (
    Alert,
    count_alerts,
    has_alert_for,
    invalidation_levels,
    next_digest_id,
    record_alert,
    sent_alerts,
)
from nivesh_core.config import Settings
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.ledger import latest_call
from nivesh_core.mf_store import fund_meta_pair
from nivesh_core.profile import Profile
from nivesh_core.schedule_config import ScheduleSettings
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_core.thesis_store import active_theses
from nivesh_core.timeutil import IST, ist_date
from nivesh_core.watch import watched
from nivesh_engine.alert_rules import (
    BIG_MOVE_CLOSES,
    Hit,
    Past,
    Pending,
    below_invalidation,
    big_move,
    break_52w,
    entered_zone,
    hygiene,
    mf_changes,
)
from nivesh_engine.bars import Bar
from nivesh_engine.calendar import Calendar, last_trading_day
from nivesh_engine.review_rules import criterion_status
from nivesh_engine.schedule import is_stale
from nivesh_engine.xray import Xray

NO_VERDICT = "no verdict yet"
BAR_WINDOW_DAYS = 420  # a little over 252 sessions
RESEARCH = "run nivesh research {symbol}"
THESIS = "review the thesis"
NEXT_STEP = {
    "close_below_invalidation": THESIS,
    "kill_criterion": THESIS,
    "mf_manager_change": "run nivesh mf doctor {symbol}",
    "mf_category_change": "run nivesh mf doctor {symbol}",
    "mf_ter_change": "run nivesh mf doctor {symbol}",
    "mf_merger": "run nivesh mf doctor {symbol}",
}


@dataclass(frozen=True)
class Fired:
    """A rule that fired for one security, ready for the hygiene decision."""

    rule: str
    security_id: int
    symbol: str
    market: str
    trading_date: date
    verdict: str
    detail: str
    next_step: str
    levels: tuple[str, ...] = ()
    extra: tuple[tuple[str, str], ...] = ()  # more payload text, e.g. the metadata date


@dataclass(frozen=True)
class Evaluation:
    fired: tuple[Fired, ...] = ()
    notes: tuple[str, ...] = ()  # securities or checks that were skipped, with the reason
    stale: str | None = None  # set when the whole evaluation was skipped for old data
    idle: bool = False  # nothing to check at all (no positions, no funds)


def verdict_of(sql: sqlite3.Connection, security_id: int) -> str:
    call = latest_call(sql, security_id)
    return NO_VERDICT if call is None else call.verdict


def make_fired(hit: Hit, row: SecurityRow, verdict: str, day: date) -> Fired:
    step = NEXT_STEP.get(hit.rule, RESEARCH).format(symbol=row.symbol)
    return Fired(
        hit.rule, row.id, row.symbol, row.market, day, verdict, hit.detail, step, hit.levels
    )


def build_alert(
    f: Fired, at: datetime, delivered: int, digest_id: int | None = None,
    extra: dict[str, str] | None = None,
) -> Alert:  # fmt: skip
    """The stored row: what happened, the current verdict, a next step and the price levels.
    Symbols, verdicts and percentages only; never quantities or values of holdings."""
    payload: dict[str, object] = {
        "what": f"{f.symbol} {f.detail}", "verdict": f.verdict, "next_step": f.next_step,
        "levels": list(f.levels),
    }  # fmt: skip
    payload.update(dict(f.extra))
    payload.update(extra or {})
    return Alert.model_validate({
        "rule": f.rule, "security_id": f.security_id, "fired_at": at,
        "trading_date": f.trading_date, "payload": payload, "delivered": delivered,
        "digest_id": digest_id,
    })  # fmt: skip


def fresh_bars(
    duck: duckdb.DuckDBPyConnection, ids: list[int], market: str, settings: Settings, day: date,
    at: datetime,
) -> tuple[dict[int, list[Bar]], date, str | None]:  # fmt: skip
    """(bars, the session they must reach, why they are stale or None). Bars are stale when the
    newest one among `ids` is older than the last finished session of the market, so a check
    never runs on old numbers."""
    bars = load_bars(duck, ids, start=day - timedelta(days=BAR_WINDOW_DAYS), end=day)
    expected = min(day, last_trading_day(calendar_for(market, settings), at))
    newest = max((b[-1].date for b in bars.values() if b), default=None)
    if is_stale(newest, expected):
        return bars, expected, f"bars stale (latest {newest or 'none'})"
    return bars, expected, None


def evaluate_price_monitors(
    sql: sqlite3.Connection, duck: duckdb.DuckDBPyConnection, settings: Settings,
    sched: ScheduleSettings, market: str, day: date, at: datetime,
) -> Evaluation:  # fmt: skip
    """Daily-move, 52-week, invalidation and watch-zone rules on the stored bars of the equity
    and ETF holdings and the watchlist of one market (`IN` or `US`), as of session `day`."""
    held = {h.security_id for h in held_securities(sql) if h.market == market}
    zones = {w.security_id: w for w in watched(sql) if w.entry_low is not None}
    rows = SecurityMaster(sql).get_many(held | set(zones))
    ids = sorted(i for i, r in rows.items() if r.market == market and (i in held or i in zones))
    if not ids:
        return Evaluation(notes=("nothing held or watched in this market",), idle=True)
    bars, expected, stale = fresh_bars(duck, ids, market, settings, day, at)
    if stale is not None:
        return Evaluation(stale=stale)
    levels = invalidation_levels(sql)
    cfg = sched.alerts
    fired: list[Fired] = []
    notes: list[str] = []
    for sid in ids:
        row, series = rows[sid], bars.get(sid) or []
        if not series or series[-1].date < expected:
            notes.append(f"no recent bar: {row.symbol}")
            continue
        verdict = verdict_of(sql, sid)
        closes = [b.close for b in series]
        hits: list[Hit | None] = []
        if sid in held:
            if len(closes) < BIG_MOVE_CLOSES:
                notes.append(f"not enough history: {row.symbol}")
            hits += [big_move(closes, cfg.vol_mult)]
            hits += [break_52w(series, settings.analysis.setups.breakout_volume_mult)]
            if sid not in levels:
                notes.append(f"no numeric invalidation: {row.symbol}")
            else:
                hits += [below_invalidation(closes[-1], levels[sid])]
        zone = zones.get(sid)
        if zone is not None and len(closes) >= 2:
            hits += [entered_zone(closes[-2], closes[-1], zone.entry_low, zone.entry_high)]
        fired += [make_fired(h, row, verdict, day) for h in hits if h is not None]
    return Evaluation(tuple(fired), tuple(notes))


def _facts_metrics(
    sql: sqlite3.Connection, duck: duckdb.DuckDBPyConnection, settings: Settings,
    profile: Profile, sid: int, day: date, xray: Xray,
) -> dict[str, Decimal | None]:  # fmt: skip
    holding = holdings_of(sql, sid)[0]
    return dict(review_facts(duck, sql, settings, profile, holding, day, xray=xray).metrics)


def evaluate_kill_criteria(
    sql: sqlite3.Connection, duck: duckdb.DuckDBPyConnection, settings: Settings,
    profile: Profile, sched: ScheduleSettings, market: str, day: date, at: datetime,
) -> Evaluation:  # fmt: skip
    """Machine-checkable kill criteria of the active theses of held equity and ETF positions.
    The portfolio X-ray is built once; one security that fails is skipped with a note. A
    criterion whose metric has no value is reported, never alerted. No agent is involved."""
    theses = active_theses(sql)
    held = [h for h in held_securities(sql) if h.market == market and h.security_id in theses]
    todo = [h for h in held if any(c.machine for c in theses[h.security_id].kill_criteria)]
    if not todo:
        return Evaluation(
            notes=("no machine-checkable kill criteria on held positions",), idle=True
        )
    ids = sorted(h.security_id for h in todo)
    _, _, stale = fresh_bars(duck, ids, market, settings, day, at)
    if stale is not None:
        return Evaluation(stale=stale)
    xray = xray_report(duck, sql, settings, profile, day)["xray"]
    rows = SecurityMaster(sql).get_many(ids)
    fired: list[Fired] = []
    notes: list[str] = []
    for h in todo:
        row = rows[h.security_id]
        try:
            metrics = _facts_metrics(sql, duck, settings, profile, h.security_id, day, xray)
        except Exception as e:  # one security must not stop the others
            notes.append(f"facts unavailable: {row.symbol} ({type(e).__name__})")
            continue
        met: list[str] = []
        for c in theses[h.security_id].kill_criteria:
            if not c.machine:
                continue
            value = metrics.get(c.metric or "")
            status = criterion_status(c, value)
            if status == "unknown":
                notes.append(f"metric {c.metric} unavailable: {row.symbol}")
            elif status == "met":
                met.append(f"{c.text} ({c.metric} is {value})")
        if met:
            hit = Hit("kill_criterion", "kill criterion met: " + "; ".join(met))
            fired.append(make_fired(hit, row, verdict_of(sql, h.security_id), day))
    return Evaluation(tuple(fired), tuple(notes))


def owned_funds(sql: sqlite3.Connection) -> dict[int, SecurityRow]:
    """Security rows of the mutual funds in the latest holdings."""
    master = SecurityMaster(sql)
    out: dict[int, SecurityRow] = {}
    for h in latest_holdings(sql):
        if h.asset_class != "mf":
            continue
        row = master.by_amfi_code(h.amfi_code) if h.amfi_code else None
        rows = [row] if row else (master.by_isin(h.isin) if h.isin else [])
        if rows:
            out[rows[0].id] = rows[0]
    return out


def evaluate_mf_changes(
    sql: sqlite3.Connection, duck: duckdb.DuckDBPyConnection, sched: ScheduleSettings,
    day: date, at: datetime,
) -> Evaluation:  # fmt: skip
    """Manager, category, expense-ratio and (best effort) merger changes between the two newest
    metadata rows of each owned fund. A metadata date alerts once."""
    funds = owned_funds(sql)
    if not funds:
        return Evaluation(notes=("no owned funds",), idle=True)
    pairs = {sid: fund_meta_pair(duck, sid) for sid in funds}
    newest = max((cur.as_of for cur, _ in pairs.values() if cur is not None), default=None)
    floor = day - timedelta(days=sched.mf_stale_days)
    if is_stale(newest, floor):
        return Evaluation(stale=f"fund meta stale (latest {newest or 'none'})")
    fired: list[Fired] = []
    for sid, (cur, prev) in sorted(pairs.items()):
        if cur is None:
            continue
        for hit in mf_changes(prev, cur, sched.alerts.ter_pp):
            if has_alert_for(sql, hit.rule, sid, "meta_as_of", cur.as_of.isoformat()):
                continue
            f = make_fired(hit, funds[sid], verdict_of(sql, sid), day)
            fired.append(replace(f, extra=(("meta_as_of", cur.as_of.isoformat()),)))
    return Evaluation(tuple(fired))


# ---- hygiene, sending and storing ----------------------------------------------------------------
Notifier = Callable[[Message], list[Outcome]]
HISTORY_DAYS = 21  # calendar days of sent alerts read for the repeat window
CARRIED = "carried"
HELD_NOTICE = "held_back_notice"


@dataclass(frozen=True)
class DispatchSummary:
    sent: int = 0  # alerts sent one by one
    digested: int = 0  # alerts sent inside a digest
    repeats: int = 0  # held back as repeats
    over_cap: int = 0  # held back by the daily cap
    unsent: int = 0  # chosen to send, but no channel took them
    notice: bool = False  # a "held back yesterday" notice went out on its own

    def line(self) -> str:
        return (
            f"{self.sent} sent, {self.digested} in a digest, {self.repeats} repeats held, "
            f"{self.over_cap} over the daily cap, {self.unsent} not delivered"
        )


def alert_text(f: Fired) -> str:
    return f"{f.symbol}: {f.detail}. Verdict: {f.verdict}. Next step: {f.next_step}."


def _try_send(notifier: Notifier, msg: Message) -> tuple[bool, str]:
    """(taken by a channel, why a check blocked it). A message that fails the personal-data
    scan is never handed to the notifier; a notifier that raises is a failed send."""
    why = blocked_reason(msg, page=True)
    if why:
        return False, why
    try:
        outcomes = notifier(msg)
    except Exception:  # the text of a transport error can carry an address
        return False, ""
    blocked = "; ".join(o.detail for o in outcomes if o.status == "blocked")
    return any(o.status == "sent" for o in outcomes), blocked


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=IST)
    return start, start + timedelta(days=1)


def held_back_yesterday(sql: sqlite3.Connection, at: datetime) -> int:
    """Alerts the daily cap held back on the previous IST day."""
    start, end = _day_bounds(ist_date(at) - timedelta(days=1))
    return count_alerts(sql, start, end, '%"suppressed": "cap"%')


def _carried(sql: sqlite3.Connection, at: datetime) -> bool:
    start, end = _day_bounds(ist_date(at))
    return count_alerts(sql, start, end, f'%"{CARRIED}": "held back"%') > 0


def _held_note(n: int) -> str:
    return f"{n} alerts held back yesterday"


def dispatch(
    sql: sqlite3.Connection, fired: Sequence[Fired], sched: ScheduleSettings,
    calendars: Mapping[str, Calendar], at: datetime, notifier: Notifier,
) -> DispatchSummary:  # fmt: skip
    """Apply the hygiene rules to what fired, send what is allowed, then store every alert.
    A send comes first so that a stored 'delivered' is never a claim that did not happen; a
    crash between the two can send an alert again on the next run (ADR-0014)."""
    since = ist_date(at) - timedelta(days=HISTORY_DAYS)
    past = [
        Past(a.rule, a.security_id, a.trading_date, a.fired_at) for a in sent_alerts(sql, since)
    ]
    pending = [Pending(f.rule, f.security_id, f.market, f.trading_date) for f in fired]
    plan = hygiene(pending, past, calendars, at, sched.alerts)
    notes = dict(plan.notes)
    extra: dict[int, dict[str, str]] = {i: {} for i in range(len(fired))}
    for i, why in notes.items():
        extra[i]["note"] = why
    held_n = held_back_yesterday(sql, at)
    carry = held_n > 0 and not _carried(sql, at)
    delivered: dict[int, tuple[int, int | None]] = {}
    sent = digested = unsent = 0
    first = True
    for i in plan.send:
        text = alert_text(fired[i])
        if carry and first:
            text = f"{_held_note(held_n)}.\n{text}"
            extra[i][CARRIED] = "held back"
        first = False
        ok, blocked = _try_send(notifier, plain_message(f"nivesh alert: {fired[i].symbol}", text))
        if blocked:
            extra[i]["blocked"] = blocked
        delivered[i] = (1 if ok else 0, None)
        sent += ok
        unsent += not ok
    if plan.digest:
        digest_id = next_digest_id(sql)
        lines = [alert_text(fired[i]) for i in plan.digest]
        if carry and first:
            lines.insert(0, f"{_held_note(held_n)}.")
            extra[plan.digest[0]][CARRIED] = "held back"
        body = "\n".join(lines)
        ok, blocked = _try_send(
            notifier, plain_message(f"nivesh alerts ({len(plan.digest)})", body)
        )
        for i in plan.digest:
            if blocked:
                extra[i]["blocked"] = blocked
            delivered[i] = (2 if ok else 0, digest_id if ok else None)
        digested += len(plan.digest) if ok else 0
        unsent += 0 if ok else len(plan.digest)
        first = False
    for i, why in plan.held:
        extra[i]["suppressed"] = why
    notice = False
    if carry and first:  # nothing else went out today: say it on its own, once
        notice, _ = _try_send(
            notifier, plain_message("nivesh: alerts held back", _held_note(held_n))
        )
        marker = Alert.model_validate({
            "rule": HELD_NOTICE, "fired_at": at, "trading_date": ist_date(at),
            "payload": {"what": _held_note(held_n), CARRIED: "held back"},
            "delivered": 1 if notice else 0,
        })  # fmt: skip
        record_alert(sql, marker)
    for i, f in enumerate(fired):
        flag, member_of = delivered.get(i, (0, None))
        record_alert(sql, build_alert(f, at, flag, member_of, extra[i]))
    held = dict(plan.held)
    return DispatchSummary(
        sent, digested, sum(1 for w in held.values() if w == "dedupe"),
        sum(1 for w in held.values() if w == "cap"), unsent, notice,
    )  # fmt: skip
