"""Red-flag detector (ST-6.5): promoter pledge, auditor change, cash flow against profit,
receivable days, dilution and contingent liabilities. Pure and Decimal-only.

Each flag is `fired`, `clear` or `not_evaluable`. Missing data is never `clear`: a flag that cannot
be tested says so and why. A fired flag carries a severity (hard or soft) from the owner's config
and evidence made only of numbers, dates and filing identifiers, never quoted filing text. Only
data filed on or before the as-of date is used. The detector reports; it does not decide what to
do about a flag. A hard flag is a signal the scoring step reads, not an instruction.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.market_models import ShareholdingRow
from nivesh_engine import dmath
from nivesh_engine.statements import StatementRow, latest_as_of

Status = Literal["fired", "clear", "not_evaluable"]
QUANTUM = Decimal("0.0001")
ANNUAL_GAP_DAYS = (330, 400)


@dataclass(frozen=True)
class EightK:
    """A stored 8-K filing: whether it carries Item 4.01 (a change of certifying accountant)."""

    filing_id: int
    filed_at: date
    auditor_item: bool


@dataclass(frozen=True)
class FlagInputs:
    """Everything the detector reads. `auditor` is the stored 8-K filings (None: no source).
    `contingent_liabilities` is supplied by the caller (no stored item); `net_worth` defaults to
    the latest total_equity."""

    market: str
    rows: Sequence[StatementRow] = ()
    shareholding: Sequence[ShareholdingRow] = ()
    auditor: Sequence[EightK] | None = None
    contingent_liabilities: Decimal | None = None
    net_worth: Decimal | None = None


@dataclass(frozen=True)
class FlagResult:
    flag: str
    status: Status
    severity: Literal["hard", "soft"] | None
    evidence: list[str] = field(default_factory=list)
    reason: str = ""
    as_of: date | None = None
    kind: str = ""  # the config key that set the severity, when fired


def any_hard(results: Sequence[FlagResult]) -> bool:
    return any(r.status == "fired" and r.severity == "hard" for r in results)


def _num(value: Decimal) -> str:
    return f"{dmath.quantize(value, QUANTUM).normalize():f}"


class _Annual:
    """Annual figures visible on the as-of date: item -> {period end -> value}."""

    def __init__(self, rows: Sequence[StatementRow], as_of: date) -> None:
        self.by_item: dict[str, dict[date, Decimal]] = {}
        self.latest_equity: Decimal | None = None
        newest: date | None = None
        for r in latest_as_of([r for r in rows if r.period_end <= as_of], as_of):
            if r.period_type == "A":
                self.by_item.setdefault(r.item, {})[r.period_end] = r.value
            if r.item == "total_equity" and (newest is None or r.period_end > newest):
                newest, self.latest_equity = r.period_end, r.value

    def last(self, item: str, count: int) -> list[tuple[date, Decimal]]:
        """The latest `count` annual points of `item`, oldest first ([] when fewer or gappy)."""
        pts = sorted(self.by_item.get(item, {}).items())[-count:]
        if len(pts) < count:
            return []
        lo, hi = ANNUAL_GAP_DAYS
        if any(not lo <= (b[0] - a[0]).days <= hi for a, b in zip(pts, pts[1:], strict=False)):
            return []
        return pts


def _severity(cfg: AnalysisSettings, kind: str) -> Literal["hard", "soft"]:
    return cfg.flags.severity[kind]


def _result(
    flag: str, status: Status, as_of: date, reason: str, *, evidence: list[str] | None = None,
    cfg: AnalysisSettings | None = None, kind: str = "",
) -> FlagResult:  # fmt: skip
    sev = _severity(cfg, kind) if status == "fired" and cfg is not None else None
    return FlagResult(flag, status, sev, evidence or [], reason, as_of, kind)


def _not_evaluable(flag: str, as_of: date, reason: str) -> FlagResult:
    return _result(flag, "not_evaluable", as_of, reason)


# ---- individual flags ---------------------------------------------------------------------------
def _pledge(inputs: FlagInputs, as_of: date, cfg: AnalysisSettings) -> FlagResult:
    flag = "pledge"
    if inputs.market != "IN":
        return _not_evaluable(flag, as_of, "no promoter pledge disclosure for US securities")
    best: dict[date, ShareholdingRow] = {}
    for s in inputs.shareholding:
        if s.filed_at <= as_of and s.period_end <= as_of and s.promoter_pledged_pct is not None:
            if s.period_end not in best or s.filed_at > best[s.period_end].filed_at:
                best[s.period_end] = s
    series = [(k, best[k].promoter_pledged_pct) for k in sorted(best)]
    pts = [(d, v) for d, v in series if v is not None]
    if not pts:
        return _not_evaluable(flag, as_of, f"no promoter pledge reported on or before {as_of}")
    f = cfg.flags
    need = f.pledge_rising_quarters + 1
    last_end, last = pts[-1]
    high = last > f.pledge_pct
    rising: bool | None = None
    if len(pts) >= need:
        tail = [v for _, v in pts[-need:]]
        rising = all(b > a for a, b in zip(tail, tail[1:], strict=False))
    evidence = [f"pledged {_num(last)}% of promoter shares at {last_end}"]
    evidence += [f"pledge by quarter: {', '.join(f'{_num(v)} ({d})' for d, v in pts[-need:])}"]
    gap = f"the rising test is not evaluable (needs {need} quarters, have {len(pts)})"
    if high:
        kind = "pledge_high_and_rising" if rising else "pledge_high"
        why = f"pledge {_num(last)}% is above {_num(f.pledge_pct)}%"
        if rising is None:
            why += f"; {gap}"
        elif rising:
            why += " and rising"
        return _result(flag, "fired", as_of, why, evidence=evidence, cfg=cfg, kind=kind)
    if rising:
        why = f"pledge rose in each of the last {f.pledge_rising_quarters} quarters"
        return _result(flag, "fired", as_of, why, evidence=evidence, cfg=cfg, kind="pledge_rising")
    if rising is None:
        return _not_evaluable(flag, as_of, f"level is not above {_num(f.pledge_pct)}%; {gap}")
    return _result(flag, "clear", as_of, f"pledge {_num(last)}% is not above {_num(f.pledge_pct)}%"
                   " and not rising", evidence=evidence)  # fmt: skip


def _auditor(inputs: FlagInputs, as_of: date, cfg: AnalysisSettings) -> FlagResult:
    flag = "auditor_change"
    if inputs.market != "US":
        return _not_evaluable(flag, as_of, "no source for auditor changes outside SEC 8-K filings")
    since = as_of - timedelta(days=cfg.flags.auditor_lookback_days)
    seen = [f for f in inputs.auditor or () if since <= f.filed_at <= as_of]
    if not seen:
        return _not_evaluable(
            flag, as_of, f"no 8-K filings stored for the {cfg.flags.auditor_lookback_days} days "
            f"to {as_of}, so a change cannot be ruled out"
        )  # fmt: skip
    changes = sorted((f for f in seen if f.auditor_item), key=lambda f: (f.filed_at, f.filing_id))
    if changes:
        evidence = [f"8-K Item 4.01 filed {f.filed_at} (filing id {f.filing_id})" for f in changes]
        return _result(flag, "fired", as_of, f"{len(changes)} Item 4.01 filing(s) in the look-back",
                       evidence=evidence, cfg=cfg, kind="auditor_change")  # fmt: skip
    return _result(flag, "clear", as_of, f"no Item 4.01 among {len(seen)} stored 8-K filing(s)")


def _cfo_to_pat(book: _Annual, as_of: date, cfg: AnalysisSettings) -> FlagResult:
    flag, years = "cfo_to_pat", cfg.flags.cfo_to_pat_years
    pat = book.last("net_income", years)
    cfo = {e: v for e, v in book.by_item.get("cfo", {}).items()}
    if not pat or any(e not in cfo for e, _ in pat):
        return _not_evaluable(
            flag, as_of, f"needs {years} consecutive annual years of net income and cfo"
        )
    total_pat = sum((v for _, v in pat), Decimal(0))
    total_cfo = sum((cfo[e] for e, _ in pat), Decimal(0))
    if total_pat <= 0:
        return _not_evaluable(
            flag, as_of, f"net income over {years} years is non-positive ({_num(total_pat)})"
        )
    with localcontext(dmath.CONTEXT):
        ratio = total_cfo / total_pat
    evidence = [
        f"{years}y cfo {_num(total_cfo)} over net income {_num(total_pat)} = {_num(ratio)}",
        f"fiscal years ending {pat[0][0]} to {pat[-1][0]}",
    ]
    if ratio < cfg.flags.cfo_to_pat_min:
        why = f"cfo is {_num(ratio)} of net income, below {_num(cfg.flags.cfo_to_pat_min)}"
        return _result(flag, "fired", as_of, why, evidence=evidence, cfg=cfg, kind=flag)
    return _result(flag, "clear", as_of, f"cfo is {_num(ratio)} of net income", evidence=evidence)


def _receivable_days(book: _Annual, as_of: date, cfg: AnalysisSettings) -> FlagResult:
    flag = "receivable_days"
    pts = book.last("receivables", 2)
    rev = book.by_item.get("revenue", {})
    if not pts or any(e not in rev for e, _ in pts):
        return _not_evaluable(
            flag, as_of, "needs two consecutive annual years of receivables and revenue"
        )
    if any(rev[e] <= 0 for e, _ in pts):
        return _not_evaluable(flag, as_of, "revenue is not positive, receivable days are undefined")
    with localcontext(dmath.CONTEXT):
        days = [v * dmath.DAYS_PER_YEAR / rev[e] for e, v in pts]
        if days[0] <= 0:
            return _not_evaluable(flag, as_of, "prior-year receivable days are not positive")
        rise = (days[1] / days[0] - 1) * dmath.HUNDRED
    evidence = [
        f"receivable days {_num(days[0])} ({pts[0][0]}) to {_num(days[1])} ({pts[1][0]}), "
        f"{_num(rise)}%"
    ]
    if rise > cfg.flags.receivable_days_rise_pct:
        why = (
            f"receivable days rose {_num(rise)}%, above {_num(cfg.flags.receivable_days_rise_pct)}%"
        )
        return _result(flag, "fired", as_of, why, evidence=evidence, cfg=cfg, kind=flag)
    return _result(
        flag, "clear", as_of, f"receivable days changed {_num(rise)}%", evidence=evidence
    )


def _dilution(inputs: FlagInputs, book: _Annual, as_of: date, cfg: AnalysisSettings) -> FlagResult:
    flag = "dilution"
    if inputs.market == "IN":
        return _not_evaluable(
            flag, as_of, "no share count in India filings (share capital is a value)"
        )
    pts = book.last("shares_out", 2)
    if not pts:
        return _not_evaluable(flag, as_of, "needs two consecutive annual share counts")
    if pts[0][1] <= 0:
        return _not_evaluable(flag, as_of, "the prior-year share count is not positive")
    with localcontext(dmath.CONTEXT):
        growth = (pts[1][1] / pts[0][1] - 1) * dmath.HUNDRED
    evidence = [f"shares {_num(pts[0][1])} ({pts[0][0]}) to {_num(pts[1][1])} ({pts[1][0]})"]
    if growth > cfg.flags.dilution_pct_per_year:
        limit = _num(cfg.flags.dilution_pct_per_year)
        why = f"share count grew {_num(growth)}% in a year, above {limit}%"
        return _result(flag, "fired", as_of, why, evidence=evidence, cfg=cfg, kind=flag)
    return _result(
        flag, "clear", as_of, f"share count grew {_num(growth)}% in a year", evidence=evidence
    )


def _contingent(
    inputs: FlagInputs, book: _Annual, as_of: date, cfg: AnalysisSettings
) -> FlagResult:
    flag = "contingent_liabilities"
    if inputs.contingent_liabilities is None:
        return _not_evaluable(
            flag, as_of, "no contingent liabilities figure supplied (no source is stored)"
        )
    worth = inputs.net_worth if inputs.net_worth is not None else book.latest_equity
    if worth is None:
        return _not_evaluable(flag, as_of, "no net worth available to compare against")
    if worth <= 0:
        return _not_evaluable(flag, as_of, f"non-positive net worth ({_num(worth)})")
    with localcontext(dmath.CONTEXT):
        pct = inputs.contingent_liabilities / worth * dmath.HUNDRED
    evidence = [f"contingent liabilities {_num(inputs.contingent_liabilities)} over net worth "
                f"{_num(worth)} = {_num(pct)}%"]  # fmt: skip
    if pct > cfg.flags.contingent_pct_of_net_worth:
        limit = _num(cfg.flags.contingent_pct_of_net_worth)
        why = f"contingent liabilities are {_num(pct)}% of net worth, above {limit}%"
        return _result(flag, "fired", as_of, why, evidence=evidence, cfg=cfg, kind=flag)
    return _result(
        flag,
        "clear",
        as_of,
        f"contingent liabilities are {_num(pct)}% of net worth",
        evidence=evidence,
    )


def detect_flags(inputs: FlagInputs, *, as_of: date, cfg: AnalysisSettings) -> list[FlagResult]:
    """All flags as of `as_of`, always in the same sequence: pledge, auditor change, cash flow
    against profit, receivable days, dilution, contingent liabilities."""
    book = _Annual(inputs.rows, as_of)
    return [
        _pledge(inputs, as_of, cfg),
        _auditor(inputs, as_of, cfg),
        _cfo_to_pat(book, as_of, cfg),
        _receivable_days(book, as_of, cfg),
        _dilution(inputs, book, as_of, cfg),
        _contingent(inputs, book, as_of, cfg),
    ]
