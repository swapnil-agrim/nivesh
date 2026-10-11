"""Ledger services (ST-12.1 to ST-12.3): holding-review rows, the scoring job, the scorecard
loader and the year-to-date sold amount. Reads the stores and appends rows; the arithmetic is in
`nivesh_engine.ledger_score` and `nivesh_engine.scorecard`.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb

from nivesh_adapters.analysis_data import benchmark_for, index_ref, load_bars
from nivesh_adapters.report_templates import NOT_ENOUGH, ScorecardInput, ScoreGroup
from nivesh_adapters.report_templates import ScoreRow as ReportRow
from nivesh_agents.schemas import HoldingReview
from nivesh_core.config import Settings
from nivesh_core.ledger import (
    LedgerEntry,
    ScoreRow,
    effective_decisions,
    latest_scores,
    list_calls,
    record_scores,
)
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.calibration import CalCall, Calibration, calibrate
from nivesh_engine.ledger_score import ScoreResult, Series, score_call
from nivesh_engine.scorecard import (
    Bucket,
    CallKey,
    Dedup,
    Scorecard,
    ScoredCall,
    build_scorecard,
    dedupe_calls,
)

SCORE_VERSION = 1  # bump when a scoring rule changes, so new rows sit beside the old ones
BASIS = "split_adjusted_price"
BASIS_NOTE = "price index, not TRI: returns are split-adjusted prices without dividends"

_VERDICT_OF = {"ADD": "ACCUMULATE", "EXIT": "SELL", "TRIM": "TRIM", "HOLD": "HOLD"}


def holding_review_entry(
    review: HoldingReview, *, review_day: date, price_date: date | None, thesis_horizon: str,
    thesis_review_date: date, run_id: int, prompt_versions: dict[str, str],
    model_versions: dict[str, str],
) -> LedgerEntry | None:  # fmt: skip
    """The ledger row for one holding review (ADD becomes ACCUMULATE, EXIT becomes SELL);
    a REVIEW action is not a call, so it has no row."""
    verdict = _VERDICT_OF.get(review.action)
    if verdict is None:
        return None
    body = json.dumps(review.model_dump(mode="json"), sort_keys=True).encode()
    return LedgerEntry(
        run_id=run_id,
        security_id=review.security_id,
        verdict=verdict,
        horizon=thesis_horizon,
        conviction=review.confidence,
        review_date=thesis_review_date,
        benchmark_reason="not read for holding reviews",
        input_hash=hashlib.sha256(body).hexdigest(),
        prompt_versions=prompt_versions,
        model_versions=model_versions,
        reported=True,
        as_of=review_day,
        price_date=price_date,
        source="holding_review",
    )


@dataclass(frozen=True)
class ScoreSummary:
    """What one scoring run did: rows added by status, calls still waiting for data (no row
    yet), the number of ledger calls and the reasons a benchmark could not be used."""

    scored: int
    insufficient: int
    pending: int
    calls: int
    notes: tuple[str, ...] = ()


_LOOKBACK_DAYS = 10  # bars read before the earliest issue day, to find the entry bar


def issue_day(e: LedgerEntry) -> date:
    """The call's issue day: its `as_of`, or for an older row the IST date it was written."""
    if e.as_of is not None:
        return e.as_of
    return ist_date(e.created_at or utcnow())  # a stored row always has created_at


def _series(bars: list) -> Series:  # type: ignore[type-arg]
    return Series.of([(b.date, b.close) for b in bars])


def _row(entry_id: int, horizon: int, r: ScoreResult, bench: str | None) -> ScoreRow:
    return ScoreRow(
        entry_id=entry_id, horizon_weeks=horizon, status=r.status,  # type: ignore[arg-type]
        ret=r.ret, benchmark_ret=r.benchmark_ret, cost_pct=r.cost_pct, excess=r.excess,
        call_excess=r.call_excess, hit=r.hit, end_date=r.end_date, benchmark_symbol=bench,
        basis=BASIS, version=SCORE_VERSION,
    )  # fmt: skip


def _benchmark(
    sql: sqlite3.Connection, sec: SecurityRow, settings: Settings
) -> tuple[int | None, str | None, str]:
    """(security id, symbol, reason): `scoring.benchmarks` wins over `analysis.ta.benchmarks`."""
    name = settings.scoring.benchmarks.get(sec.market)
    ref = (
        index_ref(sql, name, "scoring.benchmarks")
        if name
        else benchmark_for(sql, sec, settings.analysis)
    )
    return ref.security_id, ref.symbol, ref.reason or ""


def score_calls(
    sql: sqlite3.Connection, duck: duckdb.DuckDBPyConnection, settings: Settings, today: date
) -> ScoreSummary:
    """Score every ledger call at every configured horizon that is not scored yet and append the
    rows. The stored `last_close` is never used: entry and end bars come from the same current
    series. A rerun adds only what became measurable since the last run."""
    sc = settings.scoring
    entries = list_calls(sql)
    if not entries:
        return ScoreSummary(0, 0, 0, 0)
    secs = SecurityMaster(sql).get_many({e.security_id for e in entries})
    done = {k for k, v in latest_scores(sql, SCORE_VERSION).items() if v.status == "scored"}
    bench_of = {
        m: _benchmark(sql, s, settings) for m, s in {s.market: s for s in secs.values()}.items()
    }
    notes = sorted({f"{m}: {b[2]}" for m, b in bench_of.items() if b[0] is None})
    first: dict[int, date] = {}
    for e in entries:
        first[e.security_id] = min(issue_day(e), first.get(e.security_id, date.max))
    start = min(first.values()) - timedelta(days=_LOOKBACK_DAYS)
    wanted = sorted(set(first) & set(secs))
    sec_bars = {i: _series(b) for i, b in load_bars(duck, wanted, start=start, end=today).items()}
    bench_ids = sorted({b[0] for b in bench_of.values() if b[0] is not None})
    bench_bars = {
        i: _series(b) for i, b in load_bars(duck, bench_ids, start=start, end=today).items()
    }
    rows: list[ScoreRow] = []
    pairs: list[tuple[int, int]] = []
    for e in entries:
        sec = secs.get(e.security_id)
        if sec is None or e.id is None:
            continue
        bid, bsym, _ = bench_of[sec.market]
        cost = sc.cost_pct.get(sec.market, Decimal(0))
        day = issue_day(e)
        pdate = e.price_date if e.as_of is not None else None
        for h in sc.horizons_weeks:
            pairs.append((e.id, h))
            if (e.id, h) in done:
                continue
            res = score_call(
                e.verdict, day, pdate, sec_bars.get(sec.id, Series.of([])),
                bench_bars.get(bid or 0, Series.of([])), h, cost, today, sc.stale_days,
            )  # fmt: skip
            if res is not None:
                rows.append(_row(e.id, h, res, bsym))
    before = latest_scores(sql, SCORE_VERSION)
    record_scores(sql, rows)
    after = latest_scores(sql, SCORE_VERSION)
    new = [r for k, r in after.items() if k not in before or before[k].id != r.id]
    return ScoreSummary(
        scored=sum(1 for r in new if r.status == "scored"),
        insufficient=sum(1 for r in new if r.status == "insufficient_data"),
        pending=sum(1 for k in pairs if k not in after),
        calls=len(entries),
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class ScorecardData:
    """Surviving calls at every configured horizon (a missing score row reads as pending), how
    many are waiting for data, and how many ledger rows were folded into calls."""

    calls: tuple[ScoredCall, ...]
    pending: int
    n_calls: int
    dedup: Dedup


def load_scored_calls(
    sql: sqlite3.Connection, settings: Settings, since: date | None = None
) -> ScorecardData:
    """Join the ledger, its latest scores, the run command, the market and the owner's
    decisions into one `ScoredCall` per surviving call and horizon. Only effective decision
    rows count, and a decision on a folded row marks the call it folded into as acted."""
    sc = settings.scoring
    entries = [e for e in list_calls(sql) if e.id is not None]
    dedup = dedupe_calls(
        [
            CallKey(e.id or 0, e.security_id, issue_day(e), e.verdict, e.corrects_id)
            for e in entries
        ],
        sc.primary_horizon_weeks,
    )
    keep = set(dedup.kept)
    by_id = {e.id: e for e in entries}
    if since is not None:
        keep = {i for i in keep if issue_day(by_id[i]) >= since}
    commands = dict(sql.execute("SELECT id, command FROM run").fetchall())
    secs = SecurityMaster(sql).get_many({e.security_id for e in entries})
    acted = {
        dedup.survivor_of[i]
        for i, rows in effective_decisions(sql).items()
        if i in dedup.survivor_of and any(r.decision in ("acted", "partial") for r in rows)
    }
    scores = latest_scores(sql, SCORE_VERSION)
    out: list[ScoredCall] = []
    for i in sorted(keep):
        e = by_id[i]
        sec = secs.get(e.security_id)
        for h in sc.horizons_weeks:
            row = scores.get((i, h))
            out.append(
                ScoredCall(
                    i,
                    e.verdict,
                    h,
                    sec.market if sec else "unknown",
                    e.conviction,
                    str(commands.get(e.run_id, "unknown")),
                    i in acted,
                    row.status if row else "pending",
                    row.call_excess if row else None,
                    row.hit if row else None,
                )  # fmt: skip
            )
    pending = sum(1 for c in out if c.status == "pending")
    return ScorecardData(tuple(out), pending, len(keep), dedup)


_TITLES = {
    "verdict": "By verdict",
    "market": "By market",
    "conviction": "By conviction",
    "command": "By command",
    "system_vs_acted": "System calls and calls acted on",
}


def _report_row(b: Bucket, label: str | None = None) -> ReportRow:
    return ReportRow(
        label or b.key, b.calls, b.scored, b.hit_pct, b.mean_excess, b.median_excess, b.worst,
        b.enough,
    )  # fmt: skip


def _weeks(key: str) -> str:
    return "1 week" if key == "1" else f"{key} weeks"


def scorecard_input(
    data: ScorecardData,
    settings: Settings,
    as_of: date,
    run_at: datetime,
    run_id: int | None = None,
) -> ScorecardInput:
    """The template input for the scorecard of `data`, with the basis note and the gaps."""
    sc = settings.scoring
    card: Scorecard = build_scorecard(data.calls, sc.min_bucket, sc.primary_horizon_weeks)
    rows = tuple(_report_row(b, _weeks(b.key)) for b in card.buckets["horizon"])
    groups = tuple(
        ScoreGroup(
            f"{title} ({sc.primary_horizon_weeks} weeks)", tuple(map(_report_row, card.buckets[d]))
        )
        for d, title in _TITLES.items()
    )
    notes: list[str] = []
    if data.pending:
        notes.append(f"{data.pending} call-horizon pairs are waiting for price data (pending)")
    d = data.dedup
    folded = d.corrected + d.same_day + d.in_window
    if folded:
        notes.append(
            f"{folded} ledger rows were folded into calls (corrected {d.corrected}, same day "
            f"{d.same_day}, repeats inside the window {d.in_window})"
        )
    notes.append(f"a bucket under {sc.min_bucket} scored calls reads {NOT_ENOUGH}")
    return ScorecardInput(
        run_at=run_at, as_of=as_of, rows=rows, run_id=run_id, basis_note=BASIS_NOTE,
        groups=groups, notes=tuple(notes),
    )  # fmt: skip


def calibration_for(data: ScorecardData, settings: Settings) -> Calibration:
    """Calibration over the surviving calls at the primary horizon: one entry per call."""
    sc = settings.scoring
    pool = [
        CalCall(c.call_id, c.conviction, c.hit, c.call_excess)
        for c in data.calls
        if c.horizon_weeks == sc.primary_horizon_weeks and c.status == "scored"
    ]
    return calibrate(pool, sc.calibration_min, sc.resamples, sc.resample_seed)


_EXIT_VERDICTS = ("SELL", "TRIM")


def ytd_sold_inr(
    sql: sqlite3.Connection, year_start: date, usd_inr: Decimal | float | str
) -> tuple[Decimal, int]:
    """Value in INR of the shares the owner reduced in the calendar year of `year_start`, from
    the decision ledger: effective `acted` and `partial` rows on SELL or TRIM calls, bucketed
    by trade date, quantity times price, USD converted at `usd_inr`. Returns the amount and the
    number of decision rows used. Several partial sales on one call add up."""
    rate = Decimal(str(usd_inr))
    end = date(year_start.year + 1, 1, 1)
    verdicts = dict(sql.execute("SELECT id, verdict FROM ledger_entry").fetchall())
    total, used = Decimal(0), 0
    for entry_id, rows in effective_decisions(sql).items():
        if verdicts.get(entry_id) not in _EXIT_VERDICTS:
            continue
        for r in rows:
            if r.decision == "skipped" or not year_start <= r.on_date < end:
                continue
            value = (r.qty or Decimal(0)) * (r.price or Decimal(0))  # acted rows have both
            total += value * rate if r.currency == "USD" else value
            used += 1
    return total, used
