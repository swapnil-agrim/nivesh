"""Purpose-built report builders (ST-10.2): typed input in, `Report` out, no store access.

Every number the code formats itself goes through `_Fmt`, which registers it so the citation
check can match it. Text a model wrote (theses, cases, entry zones, levels) is passed through
as is and is held to the evidence by the check.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from nivesh_adapters.mf_report import DoctorReport
from nivesh_adapters.report import Block, Num, Report, Table, to_markdown
from nivesh_agents.committee import SecurityResult
from nivesh_agents.schemas import AnalystView, CommitteeVerdict
from nivesh_engine.citations import figures
from nivesh_engine.money import usd_text
from nivesh_engine.scoring import ScoreCard

ACTIONS = ("HOLD", "ADD", "TRIM", "EXIT", "REVIEW")
BRIEF_WORDS = 400
NOT_ADVICE = "Facts and arithmetic from your own records, not advice."


class _Fmt:
    """Formats code-produced numbers and remembers each one."""

    def __init__(self) -> None:
        self.nums: list[Num] = []
        self.dates: list[date] = []

    def _add(self, n: Num) -> str:
        self.nums.append(n)
        return n.text

    def pct(self, v: Decimal | None, dp: int = 1) -> str:
        return "n/a" if v is None else self._add(Num.show(v, dp, "%"))

    def num(self, v: Decimal | None, dp: int = 2) -> str:
        return "n/a" if v is None else self._add(Num.show(v, dp))

    def inr(self, v: Decimal) -> str:
        return self._add(Num.inr(v))

    def usd(self, v: Decimal) -> str:
        self._add(Num.show(abs(v), 2))
        return usd_text(v)

    def day(self, d: date) -> str:
        self.dates.append(d)
        return d.isoformat()

    def code(self, text: str) -> str:
        """Register every number in a line the code itself wrote (engine and service output)."""
        for t in figures(text):
            if t.day is not None:
                self.dates.append(t.day)
            elif t.value is not None:
                self.nums.append(Num(t.value, t.text, t.decimals, t.unit))
        return text

    def lines(self, items: tuple[str, ...] | list[str]) -> list[str]:
        return [self.code(x) for x in items]


def _sections(*items: tuple[str, str, str, bool], scope: str = "all") -> tuple[Block, ...]:
    return tuple(Block(i, h, t or "No data.", c, scope) for i, h, t, c in items)


def _bullets(lines: list[str] | tuple[str, ...]) -> str:
    return "\n".join(f"- {x}" for x in lines)


def _report(
    f: _Fmt, title: str, run_at: datetime, run_id: int | None, as_of: tuple[date, ...],
    summary: list[str], decision: Table, blocks: tuple[Block, ...], gaps: list[str],
    sources: list[str],
) -> Report:  # fmt: skip
    return Report(
        title=title, run_at=run_at, as_of=as_of, summary=tuple(summary[:5]), decision=decision,
        blocks=blocks, gaps=tuple(gaps), sources=tuple(sources), nums=tuple(f.nums),
        dates=tuple(f.dates), run_id=run_id,
    )  # fmt: skip


# ---- research note -------------------------------------------------------------------------
@dataclass(frozen=True)
class ResearchInput:
    result: SecurityResult
    symbol: str
    name: str
    currency: str  # INR or USD
    run_at: datetime
    card: ScoreCard | None = None
    close: Decimal | None = None
    close_date: date | None = None
    usd_inr: tuple[Decimal, date] | None = None  # rate and the date it is from
    valuation: tuple[str, ...] = ()  # engine-produced lines
    gaps: tuple[str, ...] = ()
    run_id: int | None = None


def _view_block(
    bid: str, heading: str, view: AnalystView | None, missing: str, gaps: list[str]
) -> tuple[str, str, str, bool]:
    if view is None:
        gaps.append(missing)
        return bid, heading, f"Data gap: {missing}", False
    lines = [view.thesis, *(f"{p.claim}" for p in view.key_points)]
    lines += [f"Risk: {r}" for r in view.risks] + [f"Data gap: {g}" for g in view.data_gaps]
    gaps += view.data_gaps
    return bid, heading, _bullets(lines), True


def _zone(v: CommitteeVerdict) -> str:
    z = v.entry_zone
    return "none given" if z is None else f"{z.low} to {z.high} {z.ccy}"


def _price_line(inp: ResearchInput, f: _Fmt) -> str | None:
    if inp.close is None or inp.close_date is None:
        return None
    day = f.day(inp.close_date)
    if inp.currency == "USD" and inp.usd_inr is not None:
        rate, rate_day = inp.usd_inr
        usd, inr = f.usd(inp.close), f.inr(inp.close * rate)
        return f"Last close {usd} ({inr} at {f.num(rate)} INR/USD on {f.day(rate_day)}), {day}"
    amount = f.inr(inp.close) if inp.currency == "INR" else f.usd(inp.close)
    return f"Last close {amount}, {day}"


def research_note(inp: ResearchInput) -> Report:
    """Research note: verdict box, thesis, fundamentals, valuation, technical setup, news and
    catalysts, bull vs bear, risks and invalidation, sizing."""
    f, res = _Fmt(), inp.result
    v, views, gaps = res.verdict, res.views, list(inp.gaps)
    weight = "none" if v.suggested_weight_pct is None else f.pct(v.suggested_weight_pct)
    cov = f.pct(v.coverage_pct, 0)
    review = f.day(v.review_date)
    row = (f"{inp.name} ({inp.symbol})", v.verdict, v.conviction, weight, _zone(v), review)
    decision = Table(
        ("Security", "Verdict", "Conviction", "Weight", "Entry zone", "Review by"),
        (row,),
        (str(v.security_id),),
    )
    price = _price_line(inp, f)
    box = [f"Verdict {v.verdict}, conviction {v.conviction}, horizon {v.horizon}, coverage {cov}."]
    box += ["The risk manager vetoed this idea."] if v.vetoed_by_risk else []
    box += [price] if price else []
    card = inp.card
    if card is not None and card.composite is not None:
        box.append(f"Composite score {f.num(card.composite, 1)} ({card.band}).")
    fundamentals = views.get("fundamental")
    tech = views.get("technical")
    news = views.get("news")
    thesis = [x.thesis for x in (fundamentals, tech, news) if isinstance(x, AnalystView)]
    levels = ""
    if isinstance(tech, AnalystView) and tech.levels is not None:
        lv = tech.levels
        parts = [
            f"{k} {x}"
            for k, x in (
                ("entry low", lv.entry_low),
                ("entry high", lv.entry_high),
                ("stop", lv.stop),
                ("invalidation level", lv.invalidation),
            )
            if x is not None
        ]
        levels = ("\nSetup " + (lv.setup_type or "none") + ": " + ", ".join(parts)) if parts else ""
    tech_id, tech_head, tech_text, tech_checked = _view_block(
        "technical", "Technical setup", tech if isinstance(tech, AnalystView) else None,
        "no technical view in this run", gaps,
    )  # fmt: skip
    if not inp.valuation:
        gaps.append("no valuation range supplied")
    risk_lines = [v.risk_notes, *v.invalidation, *res.risk.risks, *res.risk.veto_reasons]
    sizing = [f"Suggested weight {weight}."]
    wb = res.risk.weight_bounds
    sizing.append(f"Risk bounds {f.pct(wb.min_pct, 0)} to {f.pct(wb.max_pct, 0)}.")
    sizing += [f"Entry zone {_zone(v)}."] + list(v.overrides)
    blocks = _sections(
        ("verdict", "Verdict box", _bullets(box), False),
        ("thesis", "Investment thesis", _bullets(thesis) if thesis else "", True),
        _view_block("fundamentals", "Fundamentals",
                    fundamentals if isinstance(fundamentals, AnalystView) else None,
                    "no fundamental view in this run", gaps),
        ("valuation", "Valuation", _bullets(f.lines(inp.valuation)), True),
        (tech_id, tech_head, tech_text + levels, tech_checked),
        _view_block("news", "News and catalysts", news if isinstance(news, AnalystView) else None,
                    "no news view in this run", gaps),
        ("cases", "Bull vs bear", f"Bull: {v.bull_case}\nBear: {v.bear_case}", True),
        ("risks", "Risks and invalidation", _bullets(risk_lines), True),
        ("sizing", "Sizing", _bullets(sizing), True),
        scope=str(v.security_id),
    )  # fmt: skip
    summary = [f"{inp.name}: {v.verdict} with {v.conviction} conviction over {v.horizon}",
               f"Suggested weight {weight}; entry zone {_zone(v)}",
               f"Coverage of the evidence {cov}"]  # fmt: skip
    summary += [price] if price else []
    return _report(
        f, f"Research note: {inp.name}", inp.run_at, inp.run_id, (v.as_of,), summary, decision,
        blocks, gaps + list(res.failures),
        ["engine score card and risk facts", "analyst views and committee verdict (model text)"],
    )  # fmt: skip


# ---- portfolio review ----------------------------------------------------------------------
@dataclass(frozen=True)
class HoldingRow:
    name: str
    action: str  # one of ACTIONS
    weight_pct: Decimal | None
    reason: str


@dataclass(frozen=True)
class AllocationRow:
    name: str
    actual_pct: Decimal
    target_pct: Decimal | None


@dataclass(frozen=True)
class PortfolioInput:
    run_at: datetime
    as_of: date
    total_inr: Decimal | None
    health: tuple[str, ...] = ()
    allocation: tuple[AllocationRow, ...] = ()
    concentration: tuple[str, ...] = ()
    holdings: tuple[HoldingRow, ...] = ()
    doctor: DoctorReport | None = None
    rebalance: tuple[str, ...] = ()
    tax: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    run_id: int | None = None


def _doctor_lines(doc: DoctorReport | None, f: _Fmt) -> list[str]:
    if doc is None:
        return []
    out = []
    for fr in doc.funds:
        gap = f.pct(fr.facts.ter_gap_pct, 2)
        out.append(
            f"{fr.verdict.name}: {fr.verdict.action.value} (cost gap to a direct plan {gap})"
        )
    return out + [f"skipped {s}" for s in doc.skipped]


def portfolio_review(inp: PortfolioInput) -> Report:
    """Portfolio review: health, allocation vs target, concentration, holding actions, fund
    doctor summary, rebalance moves, tax notes and data gaps."""
    f = _Fmt()
    for h in inp.holdings:
        if h.action not in ACTIONS:
            raise ValueError(f"unknown holding action {h.action!r}")
    rows = tuple(
        (h.name, h.action, f.pct(h.weight_pct), h.reason) for h in inp.holdings
    )  # fmt: skip
    alloc = [
        f"{a.name}: {f.pct(a.actual_pct)} (target "
        f"{'not set' if a.target_pct is None else f.pct(a.target_pct)})"
        for a in inp.allocation
    ]
    doctor = _doctor_lines(inp.doctor, f)
    gaps = list(inp.gaps)
    for text, label in ((alloc, "allocation"), (inp.concentration, "concentration"),
                        (inp.holdings, "holding actions")):  # fmt: skip
        if not text:
            gaps.append(f"no {label} data")
    total = "unknown" if inp.total_inr is None else f.inr(inp.total_inr)
    blocks = _sections(
        ("health", "Health summary", _bullets(f.lines(inp.health)), True),
        ("allocation", "Allocation vs target", _bullets(alloc), True),
        ("concentration", "Concentration and look-through", _bullets(f.lines(inp.concentration)),
         True),
        ("fund_doctor", "Fund doctor summary", _bullets(doctor), True),
        ("rebalance", "Rebalance moves", _bullets(f.lines(inp.rebalance)), True),
        ("tax", "Tax notes", _bullets([NOT_ADVICE, *f.lines(inp.tax)]), True),
    )  # fmt: skip
    counts = {a: sum(1 for h in inp.holdings if h.action == a) for a in ACTIONS}
    tally = ", ".join(f"{a} {f.num(Decimal(n), 0)}" for a, n in counts.items())
    summary = [f"Portfolio value {total} as of {f.day(inp.as_of)}", f"Holding actions: {tally}"]
    summary += f.lines(inp.health[:3])
    return _report(
        f, "Portfolio review", inp.run_at, inp.run_id, (inp.as_of,), summary,
        Table(("Holding", "Action", "Weight", "Reason"), rows), blocks, gaps,
        ["stored holdings and prices", "engine xray, review, fund doctor and tax services"],
    )  # fmt: skip


# ---- fund doctor ---------------------------------------------------------------------------
def fund_doctor(
    doc: DoctorReport, *, run_at: datetime, as_of: date, run_id: int | None = None
) -> Report:
    """Fund doctor: one row per fund with its facts and the action the rules decided."""
    f = _Fmt()
    rows, blocks, gaps = [], [], list(doc.skipped)
    for i, fr in enumerate(doc.funds, start=1):
        fa, vd = fr.facts, fr.verdict
        reasons = "; ".join(f"{r.code.value} ({r.metric})" for r in vd.reasons) or "none"
        rows.append((vd.name, vd.action.value, reasons))
        lines = [
            f"Cost gap to a direct plan {f.pct(fa.ter_gap_pct, 2)}",
            f"Overlap with other holdings {f.pct(fa.overlap_pct)}",
            f"Beat rate {f.pct(fa.beat_pct)}; median excess {f.pct(fa.median_excess_pct, 2)}",
            f"Downside capture {f.pct(fa.downside_capture_pct)}; max drawdown "
            f"{f.pct(fa.max_drawdown_pct)}",
        ] + [f"{k} unavailable: {why}" for k, why in sorted(fa.unavailable.items())]
        blocks.append(Block(f"fund.{i}", vd.name, _bullets(lines), True))
        gaps += [f"{vd.name}: {k} unavailable" for k in sorted(fa.unavailable)]
    if not doc.funds:
        blocks.append(Block("none", "Funds", "No fund was reviewed.", False))
        gaps.append("no mutual fund holdings reviewed")
    summary = [f"{f.num(Decimal(len(doc.funds)), 0)} funds reviewed as of {f.day(as_of)}"]
    summary += [f"{r[0]}: {r[1]}" for r in rows[:4]]
    return _report(
        f, "Fund doctor", run_at, run_id, (as_of,), summary,
        Table(("Fund", "Action", "Reasons"), tuple(rows)), tuple(blocks), gaps,
        ["stored NAV history and fund facts", "engine fund doctor rules"],
    )  # fmt: skip


# ---- ideas ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class IdeaEntry:
    rank: int
    symbol: str
    market: str
    label: str
    verdict: CommitteeVerdict


@dataclass(frozen=True)
class IdeasInput:
    run_at: datetime
    as_of: date
    preset: str
    entries: tuple[IdeaEntry, ...]
    wanted: int
    qualified: int
    message: str = ""
    notes: tuple[str, ...] = ()
    run_id: int | None = None


def ideas_report(inp: IdeasInput) -> Report:
    """Ideas: thesis, entry zone, invalidation, weight and what would prove each wrong."""
    f = _Fmt()
    rows, blocks = [], []
    for e in inp.entries:
        v = e.verdict
        w = "none" if v.suggested_weight_pct is None else f.pct(v.suggested_weight_pct)
        rows.append((f.num(Decimal(e.rank), 0), e.symbol, v.verdict, v.conviction, _zone(v), w))
        text = _bullets([
            f"Thesis: {v.bull_case}", f"Entry zone: {_zone(v)}", f"Suggested weight: {w}",
            f"Invalidation: {'; '.join(v.invalidation) or 'none given'}",
            f"What would prove this wrong: {v.bear_case}",
            f"Review by {f.day(v.review_date)}",
        ])  # fmt: skip
        blocks.append(
            Block(f"idea.{e.rank}", f"{e.rank}. {e.symbol} ({e.market})", text, True,
                  str(v.security_id))
        )  # fmt: skip
    got = len(inp.entries)
    line = f"{f.num(Decimal(got), 0)} of {f.num(Decimal(inp.wanted), 0)} asked-for ideas qualified"
    if not blocks:
        blocks.append(Block("none", "Ideas", inp.message or "No idea qualified.", False))
    summary = [line, f"Preset {inp.preset}, as of {f.day(inp.as_of)}"] + (
        [f.code(inp.message)] if inp.message else []
    )  # fmt: skip
    return _report(
        f, "Ideas", inp.run_at, inp.run_id, (inp.as_of,), summary,
        Table(("Rank", "Security", "Verdict", "Conviction", "Entry zone", "Weight"), tuple(rows),
              tuple(str(e.verdict.security_id) for e in inp.entries)),
        tuple(blocks), list(inp.notes),
        ["engine screen and score card", "committee verdicts (model text)"],
    )  # fmt: skip


# ---- brief ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class BriefInput:
    run_at: datetime
    as_of: date
    markets: str
    sections: tuple[tuple[str, tuple[str, ...]], ...]  # heading and lines, in reading arrangement
    table: Table
    unavailable: tuple[tuple[str, str], ...] = ()  # what is missing and why
    run_id: int | None = None


def words(text: str) -> int:
    return len(text.split())


def prose_words(markdown: str) -> int:
    """Words of a rendered brief that count toward the budget: everything above the data gaps
    except table rows (so footer, gaps and sources are free)."""
    head = markdown.split("## Data gaps")[0]
    return words("\n".join(x for x in head.splitlines() if not x.startswith("|")))


def brief_report(inp: BriefInput) -> Report:
    """Market brief: one table and at most 400 words of text. Lines are trimmed from the end
    until the rendered brief fits, and the trim is said."""
    secs = [(h, list(lines)) for h, lines in inp.sections]
    gaps = [f"{what}: unavailable ({why})" for what, why in inp.unavailable]
    trimmed = False
    while True:
        f = _Fmt()
        blocks = tuple(
            Block(f"brief.{i}", h, _bullets(f.lines(ls)) or "Nothing to report.", True)
            for i, (h, ls) in enumerate(secs, start=1)
        )  # fmt: skip
        summary = [f"Brief for {inp.markets}, as of {f.day(inp.as_of)}"]
        table = Table(inp.table.headers, tuple(tuple(f.lines(r)) for r in inp.table.rows))
        notes = gaps + (["brief trimmed to fit 400 words"] if trimmed else [])
        report = _report(
            f, f"Market brief: {inp.markets}", inp.run_at, inp.run_id, (inp.as_of,), summary,
            table, blocks, notes, ["stored index bars, rates, flows, news and calendar"],
        )  # fmt: skip
        last = next((s for s in reversed(secs) if s[1]), None)
        if prose_words(to_markdown(report)) <= BRIEF_WORDS or last is None:
            return report
        last[1].pop()
        trimmed = True


# ---- scorecard -----------------------------------------------------------------------------
@dataclass(frozen=True)
class ScoreRow:
    horizon: str
    calls: int
    scored: int
    hit_pct: Decimal | None


@dataclass(frozen=True)
class ScorecardInput:
    run_at: datetime
    as_of: date
    rows: tuple[ScoreRow, ...]
    run_id: int | None = None


def scorecard_report(inp: ScorecardInput) -> Report:
    """Scorecard: past calls against what happened; says so plainly when nothing is scored."""
    f = _Fmt()
    scored = sum(r.scored for r in inp.rows)
    table = tuple(
        (r.horizon, f.num(Decimal(r.calls), 0), f.num(Decimal(r.scored), 0), f.pct(r.hit_pct))
        for r in inp.rows
    )  # fmt: skip
    if scored == 0:
        body = "Not enough data yet: no call has reached its scoring date."
        gaps = ["no scored calls"]
    else:
        body = f"{f.num(Decimal(scored), 0)} calls scored across {len(inp.rows)} horizons."
        gaps = []
    return _report(
        f, "Scorecard", inp.run_at, inp.run_id, (inp.as_of,), [body],
        Table(("Horizon", "Calls", "Scored", "Hit rate"), table),
        (Block("scorecard", "Scorecard", body, False),), gaps, ["append-only call ledger"],
    )  # fmt: skip


# ---- engine note (/ta and /fa without a committee) -----------------------------------------
@dataclass(frozen=True)
class EngineNoteInput:
    kind: str  # "ta" or "fa"
    symbol: str
    name: str
    as_of: date
    run_at: datetime
    result: dict[str, Any]  # the engine result in plain form
    run_id: int | None = None
    analyst: tuple[str, ...] = ()  # an analyst's view lines, model text, when one was asked for
    gaps: tuple[str, ...] = ()  # gaps the caller found (e.g. the analyst call failed)


_TA_LEVELS = ("entry_low", "entry_high", "stop", "invalidation", "target", "reward_risk")
_FA_GROUPS = (
    ("Growth", ("revenue", "eps_", "net_income_cagr", "growth")),
    ("Profitability", ("margin", "roe", "roce", "roic")),
    ("Balance sheet", ("debt", "current_ratio", "interest_cover", "promoter")),
    ("Cash quality", ("cfo", "fcf", "accruals", "days", "working_capital")),
)


def _level(setup: dict[str, Any], key: str, gaps: list[str]) -> Decimal | None:
    """A setup level that is a plain value or a metric with an availability and a reason."""
    v = setup.get(key)
    if isinstance(v, dict):
        if not v.get("available") or v.get("value") is None:
            gaps.append(f"{key}: {v.get('reason') or 'unavailable'}")
            return None
        v = v["value"]
    return None if v is None else Decimal(str(v))


def _metrics(f: _Fmt, metrics: dict[str, Any], gaps: list[str]) -> list[str]:
    """One line per available metric (2 decimals); each unavailable one becomes a gap."""
    out: list[str] = []
    for key, m in metrics.items():
        if m.get("available") and m.get("value") is not None:
            out.append(f"{key}: {f.num(Decimal(str(m['value'])))}")
        else:
            gaps.append(f"{key}: {m.get('reason') or 'unavailable'}")
    return out


def _fa_blocks(f: _Fmt, metrics: dict[str, Any], gaps: list[str]) -> tuple[Block, ...]:
    lines = _metrics(f, metrics, gaps)
    taken: set[str] = set()
    blocks = []
    for head, needles in _FA_GROUPS:
        mine = [x for x in lines if x not in taken and any(n in x.split(":")[0] for n in needles)]
        taken.update(mine)
        blocks.append((head.lower().replace(" ", "_"), head, _bullets(mine), False))
    rest = [x for x in lines if x not in taken]
    return _sections(*blocks, ("other", "Other measures", _bullets(rest), False))


def engine_note(inp: EngineNoteInput) -> Report:
    """Engine-only note for `/ta` and `/fa`: indicators or quality measures, levels and setups,
    and every measure the stored data could not support listed as a gap. No model text."""
    f, res = _Fmt(), inp.result
    gaps: list[str] = list(inp.gaps)
    day = f.day(inp.as_of)
    if inp.kind == "ta":
        ind, setup = res["indicators"], res["setup"]
        last = ind.get("last_bar_date")
        got = {k: _level(setup, k, gaps) for k in _TA_LEVELS}
        lvl = [f"{k}: {f.num(x)}" for k, x in got.items() if x is not None]
        name = setup.get("setup") or "none"
        if setup.get("reason"):
            gaps.append(f"setup: {setup['reason']}")
        levels = f"Setup {name}" + ("\n" + _bullets(lvl) if lvl else "")
        blocks = _sections(
            ("indicators", "Trend and momentum", _bullets(_metrics(f, ind["values"], gaps)), False),
            ("levels", "Levels and setup", levels, False),
        )  # fmt: skip
        stamp = f"last bar {f.day(date.fromisoformat(last))}" if last else "no bars stored"
        summary = [f"Technical read of {inp.name} as of {day}", f"Setup: {name}", stamp]
        cells = (
            f"{inp.name} ({inp.symbol})",
            name,
            *(
                f.num(Decimal(str(setup[k]))) if setup.get(k) else "n/a"
                for k in ("entry_low", "entry_high", "stop", "target")
            ),
        )
        decision = Table(("Security", "Setup", "Entry low", "Entry high", "Stop", "Target"),
                         (cells,))  # fmt: skip
    else:
        blocks = _fa_blocks(f, res["metrics"], gaps)
        cov = f.pct(Decimal(str(res["coverage_pct"])), 0) if res.get("coverage_pct") else "n/a"
        through = res.get("data_through")
        fy = res.get("fiscal_year_end")
        summary = [f"Fundamental read of {inp.name} as of {day}", f"Coverage of measures {cov}"]
        summary += [f"Filings used up to {f.day(date.fromisoformat(through))}"] if through else []
        cells = (f"{inp.name} ({inp.symbol})", cov,
                 f.day(date.fromisoformat(fy)) if fy else "n/a",
                 f.day(date.fromisoformat(through)) if through else "n/a")  # fmt: skip
        decision = Table(("Security", "Coverage", "Fiscal year end", "Data through"), (cells,))
    if inp.analyst:
        blocks += (Block("analyst", "Analyst view", _bullets(inp.analyst), True),)
    kind = "Technical" if inp.kind == "ta" else "Fundamental"
    return _report(
        f, f"{kind} note: {inp.name}", inp.run_at, inp.run_id, (inp.as_of,), summary, decision,
        blocks, gaps, ["stored prices and filings through the analysis engines"]
        + (["analyst view (model text)"] if inp.analyst else []),
    )  # fmt: skip
