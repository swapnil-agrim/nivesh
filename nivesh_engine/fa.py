"""Fundamental metrics as of a date (ST-6.3): growth, profitability, balance sheet, cash quality.

Pure and Decimal-only. Only rows filed on or before `as_of` are used (no look-ahead): a
restatement filed later never replaces the figure that was known on the date. Every metric is a
`Metric`: a value, or `available=False` with the reason and the inputs it did have (never zero,
never silently absent). All ratios use annual figures of the latest fiscal year filed; growth
uses annual points, and the last four quarters are compared with the same quarter a year earlier.

Conventions (recorded in the ADR): ROE is net income over the average of opening and closing
equity; ROIC is operating income x (1 - tax rate) over debt + equity - cash, the tax rate being
the filing's effective rate when sensible, else the owner's default (listed in the inputs);
accruals are (net income - operating cash flow) over closing assets; capex is a positive outflow.
India operating income is a profit-before-tax proxy, so EBIT, EBITDA, ROIC, ROCE and interest
cover are unavailable for India rather than mislabelled. Banks and NBFCs swap the EBITDA-based
and working-capital metrics for asset quality, margin, funding and capital metrics. Coverage is
the share of applicable metrics that are available; a metric that cannot apply to the security
(for example promoter pledge for a US company) is marked "not applicable" and not counted.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.market_models import ShareholdingRow
from nivesh_engine import dmath
from nivesh_engine.metric import Metric, coverage_pct, na, ok
from nivesh_engine.statements import StatementRow, latest_as_of

QUANTUM = Decimal("0.00000001")
NOT_APPLICABLE = "not applicable"
BANK_ITEMS = ("gnpa_pct", "nnpa_pct", "nim_pct", "casa_pct", "car_pct")
_EBIT_BASED = (
    "operating_margin_pct", "ebitda_margin_pct", "roic_pct", "roce_pct", "net_debt_ebitda",
    "interest_cover",
)  # fmt: skip
_INDIA_GAPS = (
    "current_ratio", "cfo_to_pat", "fcf_margin_pct", "accruals_ratio_pct", "receivable_days",
    "inventory_days", "payable_days", "working_capital_days",
)  # fmt: skip
_NOT_FOR_FINANCIALS = frozenset(
    {
        "gross_margin_pct", "operating_margin_pct", "ebitda_margin_pct", "roic_pct", "roce_pct",
        "net_debt_ebitda", "debt_equity", "interest_cover", "current_ratio", "cfo_to_pat",
        "fcf_margin_pct", "accruals_ratio_pct", "receivable_days", "inventory_days",
        "payable_days", "working_capital_days",
    }
)  # fmt: skip
_INDIA_PBT = (
    "India operating_income is profit before tax, not EBIT; it is never presented as EBIT or EBITDA"
)
_INDIA_VOCAB = "India filings carry no balance-sheet or cash-flow items for this metric"


@dataclass(frozen=True)
class FaResult:
    as_of: date
    market: str
    sector_kind: str
    fiscal_year_end: date | None
    metrics: dict[str, Metric] = field(default_factory=dict)
    coverage_pct: Decimal | None = None
    data_through: date | None = None


def sector_kind_of(sector: str | None, rows: Sequence[StatementRow], cfg: AnalysisSettings) -> str:
    """ "financial" for a configured financial sector name or when bank items are present."""
    names = {s.lower() for s in cfg.fa.financial_sectors}
    if sector and sector.strip().lower() in names:
        return "financial"
    return "financial" if any(r.item in BANK_ITEMS for r in rows) else "general"


@dataclass(frozen=True)
class _Book:
    annual: Mapping[str, Mapping[date, Decimal]]
    quarterly: Mapping[str, Mapping[date, Decimal]]
    latest_any: Mapping[str, Decimal]
    anchors: Sequence[date]  # annual period ends, oldest first
    fy: date | None
    as_of: date

    def at(self, item: str, end: date | None = None) -> Decimal | None:
        when = end or self.fy
        return None if when is None else self.annual.get(item, {}).get(when)


def _book(rows: Sequence[StatementRow], as_of: date) -> tuple[_Book, date | None]:
    seen = latest_as_of([r for r in rows if r.period_end <= as_of], as_of)
    annual: dict[str, dict[date, Decimal]] = {}
    quarterly: dict[str, dict[date, Decimal]] = {}
    newest: dict[str, tuple[date, Decimal]] = {}
    for r in seen:  # newest period first
        target = annual if r.period_type == "A" else quarterly
        target.setdefault(r.item, {})[r.period_end] = r.value
        if r.item not in newest or r.period_end > newest[r.item][0]:
            newest[r.item] = (r.period_end, r.value)
    anchors = sorted(set(annual.get("revenue", {})) | set(annual.get("net_income", {})))
    through = max((r.filed_at for r in seen), default=None)
    book = _Book(
        annual, quarterly, {k: v[1] for k, v in newest.items()}, anchors,
        anchors[-1] if anchors else None, as_of,
    )  # fmt: skip
    return book, through


def _q(value: Decimal) -> Decimal:
    return dmath.quantize(value, QUANTUM)


def _gather(book: _Book, items: Sequence[str]) -> tuple[dict[str, Decimal], list[str]]:
    got: dict[str, Decimal] = {}
    missing: list[str] = []
    for item in items:
        v = book.at(item)
        if v is not None and item == "capex":
            v = abs(v)  # an outflow whatever the filer's sign
        if v is None:
            missing.append(item)
        else:
            got[item] = v
    return got, missing


def _calc(
    book: _Book, items: Sequence[str], fn: Callable[[dict[str, Decimal]], Decimal | str]
) -> Metric:
    """Gather the FY figures, then `fn` returns the value or the reason it is not defined."""
    if book.fy is None:
        return na(f"no annual statements filed on or before {book.as_of}")
    got, missing = _gather(book, items)
    if missing:
        return na(f"no {', '.join(missing)} filed for the fiscal year ending {book.fy}", **got)
    out = fn(got)
    return na(out, **got) if isinstance(out, str) else ok(_q(out), **got)


def _div(a: Decimal, b: Decimal, denominator: str) -> Decimal | str:
    return f"{denominator} is zero" if b == 0 else a / b


def _pct(a: Decimal, b: Decimal, denominator: str) -> Decimal | str:
    out = _div(a, b, denominator)
    return out if isinstance(out, str) else out * dmath.HUNDRED


def _series(book: _Book, item: str, count: int) -> list[tuple[date, Decimal]]:
    pts = sorted(book.annual.get(item, {}).items())
    return pts[-count:] if len(pts) >= count else []


def _cagr(book: _Book, item: str, years: int) -> Metric:
    have = len(book.annual.get(item, {}))
    pts = _series(book, item, years + 1)
    if not pts:
        return na(f"need {years + 1} annual points, have {have}", item=item)
    (d0, v0), (d1, v1) = pts[0], pts[-1]
    if not 330 * years <= (d1 - d0).days <= 400 * years:
        return na(f"{item} annual points are not {years + 1} consecutive fiscal years", item=item)
    if v0 <= 0 or v1 <= 0:
        return na(
            f"{item} start and end values must be positive for a growth rate", start=v0, end=v1
        )
    value = dmath.cagr(v0, v1, 365 * years) * dmath.HUNDRED
    return ok(_q(value), start=v0, end=v1, points=len(pts), years=years)


def _growth_stdev(book: _Book, years: int) -> Metric:
    have = len(book.annual.get("revenue", {}))
    pts = _series(book, "revenue", years + 1)
    if not pts:
        return na(f"need {years + 1} annual points, have {have}", item="revenue")
    if any(a <= 0 for _, a in pts[:-1]):
        return na("a prior-year revenue is not positive", item="revenue")
    rates = [b / a - 1 for (_, a), (_, b) in zip(pts, pts[1:], strict=False)]
    sd = dmath.stdev(rates)
    if sd is None:
        return na("a standard deviation needs at least two growth rates", rates=len(rates))
    return ok(_q(sd * dmath.HUNDRED), rates=len(rates))


def _yoy(book: _Book, n: int) -> Metric:
    q = sorted(book.quarterly.get("revenue", {}).items(), reverse=True)
    if len(q) <= n:
        return na(f"need {n + 1} quarterly revenue rows, have {len(q)}")
    end, cur = q[n]
    prior = [v for e, v in q if 350 <= (end - e).days <= 380]
    if not prior:
        return na("no prior-year quarter for revenue", period_end=end)
    if prior[0] <= 0:
        return na("prior-year quarter revenue is not positive", period_end=end)
    return ok(_q((cur / prior[0] - 1) * dmath.HUNDRED), period_end=end, revenue=cur, prior=prior[0])


def _roe(book: _Book) -> Metric:
    if book.fy is None:
        return na(f"no annual statements filed on or before {book.as_of}")
    ni, eq = book.at("net_income"), book.at("total_equity")
    if ni is None or eq is None:
        miss = [n for n, v in (("net_income", ni), ("total_equity", eq)) if v is None]
        return na(f"no {', '.join(miss)} filed for the fiscal year ending {book.fy}")
    earlier = sorted(e for e in book.annual.get("total_equity", {}) if e < book.fy)
    opening = book.at("total_equity", earlier[-1]) if earlier else None
    if opening is None:
        return na("no prior-year equity filed to average with", net_income=ni, total_equity=eq)
    if eq <= 0 or opening <= 0:
        return na("non-positive equity: return on equity is not meaningful", opening=opening,
                  closing=eq)  # fmt: skip
    avg = (eq + opening) / 2
    return ok(_q(ni / avg * dmath.HUNDRED), net_income=ni, average_equity=avg)


def _tax_rate(book: _Book, default: Decimal) -> tuple[Decimal, str]:
    tax, pre = book.at("income_tax"), book.at("pretax_income")
    if tax is not None and pre is not None and pre > 0 and 0 <= tax <= pre:
        return tax / pre * dmath.HUNDRED, "filing"
    return default, "default_assumption"


def _roic(book: _Book, default_tax: Decimal) -> Metric:
    rate, source = _tax_rate(book, default_tax)

    def fn(g: dict[str, Decimal]) -> Decimal | str:
        capital = g["total_debt"] + g["total_equity"] - g["cash"]
        if capital <= 0:
            return "non-positive invested capital (debt + equity - cash)"
        nopat = g["operating_income"] * (1 - rate / dmath.HUNDRED)
        return nopat / capital * dmath.HUNDRED

    m = _calc(book, ("operating_income", "total_debt", "total_equity", "cash"), fn)
    return Metric(m.value, m.available, m.reason, {**m.inputs, "tax_rate_pct": rate,
                                                   "tax_source": source})  # fmt: skip


def _ebitda(g: dict[str, Decimal]) -> Decimal:
    return g["operating_income"] + g["depreciation_amortization"]


def _positive_eq(g: dict[str, Decimal]) -> str | None:
    return None if g["total_equity"] > 0 else "non-positive equity: ratio is not meaningful"


def _debt_equity(g: dict[str, Decimal]) -> Decimal | str:
    return _positive_eq(g) or g["total_debt"] / g["total_equity"]


def _net_debt_ebitda(g: dict[str, Decimal]) -> Decimal | str:
    e = _ebitda(g)
    return "non-positive EBITDA: leverage multiple is not meaningful" if e <= 0 else (
        (g["total_debt"] - g["cash"]) / e
    )  # fmt: skip


def _interest_cover(g: dict[str, Decimal]) -> Decimal | str:
    return _div(g["operating_income"], abs(g["interest_expense"]), "interest expense")


def _cfo_to_pat(g: dict[str, Decimal]) -> Decimal | str:
    if g["net_income"] <= 0:
        return "non-positive net income: cash conversion is not meaningful"
    return g["cfo"] / g["net_income"]


def _days(g: dict[str, Decimal], numerator: str, base: str) -> Decimal | str:
    return _div(g[numerator] * dmath.DAYS_PER_YEAR, g[base], base)


def _cogs_days(g: dict[str, Decimal], numerator: str) -> Decimal | str:
    cogs = g["revenue"] - g["gross_profit"]
    if cogs <= 0:
        return "non-positive cost of goods sold (revenue - gross_profit)"
    return g[numerator] * dmath.DAYS_PER_YEAR / cogs


def _working_capital(m: Mapping[str, Metric]) -> Metric:
    r, i, p = (m[n].value for n in ("receivable_days", "inventory_days", "payable_days"))
    if r is None or i is None or p is None:
        return na("needs receivable, inventory and payable days")
    return ok(_q(r + i - p), receivable_days=r, inventory_days=i, payable_days=p)


def _bank_metrics(book: _Book) -> dict[str, Metric]:
    out: dict[str, Metric] = {}
    for item in BANK_ITEMS:
        v = book.latest_any.get(item)
        out[item] = na(f"no {item} filed on or before the as-of date") if v is None else ok(_q(v))
    out["credit_cost_pct"] = na(
        "no provision item in the stored vocabulary (credit cost needs provisions and loans)"
    )
    return out


def _holding(
    shareholding: Sequence[ShareholdingRow], as_of: date, market: str
) -> dict[str, Metric]:
    if market != "IN":
        why = f"{NOT_APPLICABLE}: promoter holding and pledge are disclosed in India only"
        return {"promoter_change_4q_pp": na(why), "promoter_pledged_pct": na(why)}
    best: dict[date, ShareholdingRow] = {}
    for s in shareholding:
        if s.filed_at <= as_of and s.period_end <= as_of:
            if s.period_end not in best or s.filed_at > best[s.period_end].filed_at:
                best[s.period_end] = s
    rows = [best[k] for k in sorted(best)]
    if not rows:
        reason = "no shareholding pattern filed on or before the as-of date"
        return {"promoter_change_4q_pp": na(reason), "promoter_pledged_pct": na(reason)}
    last = rows[-1]
    change = (
        na(f"need 5 quarters of promoter holding, have {len(rows)}")
        if len(rows) < 5
        else na("promoter holding missing in the latest or reference quarter")
    )
    if len(rows) >= 5 and last.promoter_pct is not None and rows[-5].promoter_pct is not None:
        change = ok(
            _q(last.promoter_pct - rows[-5].promoter_pct),
            period_end=last.period_end, reference=rows[-5].period_end,
        )  # fmt: skip
    pledged = (
        na("no pledge percentage reported", period_end=last.period_end)
        if last.promoter_pledged_pct is None
        else ok(_q(last.promoter_pledged_pct), period_end=last.period_end)
    )
    return {"promoter_change_4q_pp": change, "promoter_pledged_pct": pledged}


def _general_metrics(book: _Book, market: str, default_tax: Decimal) -> dict[str, Metric]:
    out: dict[str, Metric] = {
        "gross_margin_pct": _calc(book, ("gross_profit", "revenue"),
                                  lambda g: _pct(g["gross_profit"], g["revenue"], "revenue")),
        "operating_margin_pct": _calc(
            book, ("operating_income", "revenue"),
            lambda g: _pct(g["operating_income"], g["revenue"], "revenue"),
        ),
        "ebitda_margin_pct": _calc(
            book, ("operating_income", "depreciation_amortization", "revenue"),
            lambda g: _pct(_ebitda(g), g["revenue"], "revenue"),
        ),
        "roic_pct": _roic(book, default_tax),
        "roce_pct": _calc(
            book, ("operating_income", "total_assets", "current_liabilities"),
            lambda g: _pct(g["operating_income"], g["total_assets"] - g["current_liabilities"],
                           "capital employed"),
        ),
        "net_debt_ebitda": _calc(
            book, ("total_debt", "cash", "operating_income", "depreciation_amortization"),
            _net_debt_ebitda,
        ),
        "debt_equity": _calc(book, ("total_debt", "total_equity"), _debt_equity),
        "interest_cover": _calc(book, ("operating_income", "interest_expense"), _interest_cover),
        "current_ratio": _calc(book, ("current_assets", "current_liabilities"),
                               lambda g: _div(g["current_assets"], g["current_liabilities"],
                                              "current liabilities")),
        "cfo_to_pat": _calc(book, ("cfo", "net_income"), _cfo_to_pat),
        "fcf_margin_pct": _calc(book, ("cfo", "capex", "revenue"),
                                lambda g: _pct(g["cfo"] - g["capex"], g["revenue"], "revenue")),
        "accruals_ratio_pct": _calc(book, ("net_income", "cfo", "total_assets"),
                                    lambda g: _pct(g["net_income"] - g["cfo"], g["total_assets"],
                                                   "total_assets")),
        "receivable_days": _calc(book, ("receivables", "revenue"),
                                 lambda g: _days(g, "receivables", "revenue")),
        "inventory_days": _calc(book, ("inventory", "revenue", "gross_profit"),
                                lambda g: _cogs_days(g, "inventory")),
        "payable_days": _calc(book, ("payables", "revenue", "gross_profit"),
                              lambda g: _cogs_days(g, "payables")),
    }  # fmt: skip
    out["working_capital_days"] = _working_capital(out)
    if market == "IN":
        for name in _EBIT_BASED:
            out[name] = na(_INDIA_PBT)
        for name in _INDIA_GAPS:
            out[name] = na(_INDIA_VOCAB)
    return out


def fa_compute(
    rows: Sequence[StatementRow],
    shareholding: Sequence[ShareholdingRow],
    *,
    as_of: date,
    sector_kind: str,
    cfg: AnalysisSettings,
    market: str | None = None,
) -> FaResult:
    """Fundamental metrics using only data filed on or before `as_of`.

    `rows` holds every filed version of every statement row; `market` is "IN" or "US" (inferred
    from the row currency when omitted); `sector_kind` is "general" or "financial"."""
    mkt = market or ("IN" if any(r.currency == "INR" for r in rows) else "US")
    years = cfg.fa.growth_years
    with localcontext(dmath.CONTEXT):
        book, through = _book(rows, as_of)
        m: dict[str, Metric] = {
            f"revenue_cagr_{years}y_pct": _cagr(book, "revenue", years),
            f"net_income_cagr_{years}y_pct": _cagr(book, "net_income", years),
            f"eps_cagr_{years}y_pct": _cagr(book, "eps", years),
            **{f"revenue_yoy_q{n}_pct": _yoy(book, n) for n in range(4)},
            "revenue_growth_stdev_pp": _growth_stdev(book, years),
            "net_margin_pct": _calc(book, ("net_income", "revenue"),
                                    lambda g: _pct(g["net_income"], g["revenue"], "revenue")),
            "roe_pct": _roe(book),
        }  # fmt: skip
        if sector_kind == "financial":
            m.update(_bank_metrics(book))
        else:
            m.update(_general_metrics(book, mkt, cfg.fa.default_tax_rate_pct))
        m.update(_holding(shareholding, as_of, mkt))
        if sector_kind == "financial":
            m = {k: v for k, v in m.items() if k not in _NOT_FOR_FINANCIALS}
        visible = [s.filed_at for s in shareholding if s.filed_at <= as_of]
        through = max([d for d in (through, *visible) if d is not None], default=None)
        counted = [v for v in m.values() if not (v.reason or "").startswith(NOT_APPLICABLE)]
        return FaResult(as_of, mkt, sector_kind, book.fy, dict(sorted(m.items())),
                        coverage_pct(counted), through)  # fmt: skip
