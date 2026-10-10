"""Financial statements as standardised rows (ST-4.4/4.5). Pure, Decimal only.

EDGAR company facts become annual and discrete quarterly rows by `quarterise`:
- duration facts of about a year are annual (A); about 90 days are discrete quarters (Q);
- 10-Q cash-flow facts are year-to-date, so a missing discrete quarter is the YTD minus the
  previous YTD with the same start; Q4 is the fiscal year minus the nine-month YTD and carries the
  10-K `filed_at`;
- instant items (balance sheet, share count) use their `end` date: a 10-K value is both the
  annual and the Q4 value.
Rows are append-only by `filed_at` (restatements keep history); a later filing repeating the same
value is not a new row. Periods that fit none of these shapes (52/53-week oddities, changed
fiscal year ends) are skipped and returned in `QuarterisedFacts.skipped`, never guessed.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

PeriodType = Literal["A", "Q"]

# Standard item -> EDGAR concepts in priority order (first one with a value for a period wins).
CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasic"),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "total_debt": ("LongTermDebt", "LongTermDebtNoncurrent"),
    "total_equity": ("StockholdersEquity",),
    "shares_out": ("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"),
}
COVER_CONCEPTS = {"EntityCommonStockSharesOutstanding"}  # dei: dated at the cover, not a period
_RANK = {c: (item, i) for item, cs in CONCEPTS.items() for i, c in enumerate(cs)}
POSITIVE_OUTFLOWS = {"capex"}  # stored as a positive outflow whatever the filer's sign


class StatementRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    period_end: date
    period_type: PeriodType
    item: str
    value: Decimal
    currency: str
    filed_at: date


@dataclass(frozen=True)
class Fact:
    concept: str
    unit: str
    start: date | None
    end: date
    value: Decimal
    filed: date
    form: str


@dataclass
class QuarterisedFacts:
    rows: list[StatementRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def facts_from_companyfacts(doc: Any) -> list[Fact]:
    """EDGAR companyfacts JSON -> facts for the mapped concepts only (others ignored)."""
    out: list[Fact] = []
    for taxonomy in (doc.get("facts") or {}).values():
        for concept, body in taxonomy.items():
            if concept not in _RANK:
                continue
            for unit, items in (body.get("units") or {}).items():
                for f in items:
                    try:
                        out.append(
                            Fact(
                                concept=concept,
                                unit=unit,
                                start=date.fromisoformat(f["start"]) if f.get("start") else None,
                                end=date.fromisoformat(f["end"]),
                                value=Decimal(str(f["val"])),
                                filed=date.fromisoformat(f["filed"]),
                                form=str(f.get("form", "")),
                            )
                        )
                    except (KeyError, ValueError, InvalidOperation):
                        continue  # a malformed fact is dropped, never half-read
    return _align_cover_facts(out)


def _align_cover_facts(facts: list[Fact]) -> list[Fact]:
    """Cover-page (dei) facts are dated at the filing's cover date, not a period end. Re-date each
    to the latest real period end of the same filing so it never creates a period of its own;
    with no such period it is dropped."""
    real = [f for f in facts if f.concept not in COVER_CONCEPTS]
    out: list[Fact] = []
    for f in facts:
        if f.concept not in COVER_CONCEPTS:
            out.append(f)
            continue
        ends = [r.end for r in real if r.filed == f.filed and r.form == f.form and r.end <= f.end]
        if ends:
            out.append(replace(f, end=max(ends)))
    return out


def _currency(unit: str) -> str:
    return unit.split("/")[0]


def _kind(start: date | None, end: date) -> str:
    if start is None:
        return "instant"
    days = (end - start).days
    if 350 <= days <= 380:
        return "A"
    if 80 <= days <= 100:
        return "Q"
    if 170 <= days <= 190 or 260 <= days <= 285:
        return "YTD"
    return "other"


def quarterise(facts: Iterable[Fact]) -> QuarterisedFacts:
    """Facts -> annual and discrete quarterly rows (see module docstring)."""
    # 1. best concept per (item, start, end): lowest priority index wins
    best: dict[tuple[str, date | None, date], int] = {}
    mapped: list[tuple[str, int, Fact]] = []
    for f in facts:
        item, rank = _RANK[f.concept]
        mapped.append((item, rank, f))
        k = (item, f.start, f.end)
        best[k] = min(best.get(k, rank), rank)
    chosen = [(item, f) for item, rank, f in mapped if best[(item, f.start, f.end)] == rank]

    out = QuarterisedFacts()
    raw: list[StatementRow] = []

    def emit(
        item: str, end: date, ptype: PeriodType, value: Decimal, unit: str, filed: date
    ) -> None:
        if item in POSITIVE_OUTFLOWS:
            value = abs(value)
        raw.append(StatementRow(period_end=end, period_type=ptype, item=item, value=value,
                                currency=_currency(unit), filed_at=filed))  # fmt: skip

    # 2. direct rows; YTD durations are collected for differencing
    cumulative: dict[tuple[str, date], dict[date, list[Fact]]] = defaultdict(
        lambda: defaultdict(list)
    )
    direct_q: set[tuple[str, date]] = set()
    for item, f in chosen:
        kind = _kind(f.start, f.end)
        if kind == "instant":
            emit(item, f.end, "Q", f.value, f.unit, f.filed)
            if f.form.startswith("10-K"):
                emit(item, f.end, "A", f.value, f.unit, f.filed)
            continue
        if kind == "other":
            out.skipped.append(f"{item} {f.start}..{f.end}")
            continue
        if kind == "A":
            emit(item, f.end, "A", f.value, f.unit, f.filed)
        if kind == "Q":
            emit(item, f.end, "Q", f.value, f.unit, f.filed)
            direct_q.add((item, f.end))
        cumulative[(item, f.start or f.end)][f.end].append(f)  # start is set for durations

    # 3. discrete quarters from YTD differences (Q2, Q3 from cash-flow YTD; Q4 = FY - 9M)
    for (item, _start), by_end in cumulative.items():
        ends = sorted(by_end)
        for prev, cur in zip(ends, ends[1:], strict=False):
            if (item, cur) in direct_q:
                continue
            a = max(by_end[prev], key=lambda f: f.filed)
            b = max(by_end[cur], key=lambda f: f.filed)
            emit(item, cur, "Q", b.value - a.value, b.unit, max(a.filed, b.filed))

    # 4. a later filing repeating the same value is not a restatement
    last: dict[tuple[str, PeriodType, date], Decimal] = {}
    for r in sorted(raw, key=lambda r: (r.item, r.period_type, r.period_end, r.filed_at)):
        pk = (r.item, r.period_type, r.period_end)
        if last.get(pk) != r.value:
            out.rows.append(r)
            last[pk] = r.value
    return out


def latest_as_of(rows: Iterable[StatementRow], as_of: date | None = None) -> list[StatementRow]:
    """Point in time: per (item, period type, period end) the value with the latest `filed_at`
    not after `as_of` (no look-ahead). Sorted newest period first."""
    best: dict[tuple[str, str, date], StatementRow] = {}
    for r in rows:
        if as_of is not None and r.filed_at > as_of:
            continue
        k = (r.item, r.period_type, r.period_end)
        if k not in best or r.filed_at > best[k].filed_at:
            best[k] = r
    return sorted(best.values(), key=lambda r: (r.period_end, r.item), reverse=True)


# Revisions (ST-4.9) ---------------------------------------------------------------------------

Direction = Literal["up", "down", "flat"]


def revision(
    snapshots: Sequence[tuple[date, Decimal]], as_of: date, window_days: int
) -> Direction | None:
    """Direction of an estimate between the snapshot nearest before `as_of - window` and the
    latest one on or before `as_of`; None when history is shorter than the window (never a
    silent 'flat')."""
    hist = sorted((d, v) for d, v in snapshots if d <= as_of)
    if not hist:
        return None
    cutoff = as_of - timedelta(days=window_days)
    earlier = [v for d, v in hist if d <= cutoff]
    if not earlier:
        return None
    then, now = earlier[-1], hist[-1][1]
    return "up" if now > then else "down" if now < then else "flat"
