"""Profit attributable to the parent's shareholders, derived when `fa` reads it (ADR-0016 D10).

India net income from the statements providers is consolidated: it includes the minority's share.
Pure and Decimal-only. For one fiscal period the basis is the first that applies:
- `reported_minority`: a provider stored `minority_interest`; attributable = net income - minority.
- `eps_x_shares`: EPS x shares_out of the SAME period is the parent's profit (approximate: EPS is
  weighted over the year, shares are the period-end count). The gap (net income - EPS x shares) /
  (EPS x shares) is judged on its two-digit figure: within +-`GAP_TOLERANCE_PCT` no minority is
  detected and net income is used (`consolidated_no_minority_detected`, the gap is recorded);
  above it EPS x shares is used; below it minority interest cannot explain the difference (it can
  come from weighted EPS against period-end shares, buybacks or a data mismatch), so the
  consolidated figure is kept (`consolidated_minority_unknown`) with that reason.
- missing or non-positive EPS or shares: the consolidated figure, `consolidated_minority_unknown`,
  "minority interest not available". The number stays usable and labelled; it is never dropped
  and the basis is never switched silently. `value` is None only without consolidated net income.
Tapetide gives shares_out for the latest fiscal year only, so older years end up unknown.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from nivesh_engine import dmath

GAP_TOLERANCE_PCT = Decimal(3)  # not config: the rule never changes per owner (ADR-0016)
REPORTED_MINORITY = "reported_minority"
EPS_X_SHARES = "eps_x_shares"
NO_MINORITY = "consolidated_no_minority_detected"
UNKNOWN = "consolidated_minority_unknown"
CONSOLIDATED = "consolidated"  # the label of ratios that must stay on consolidated profit
_GAP = Decimal("0.01")


@dataclass(frozen=True)
class Attributable:
    value: Decimal | None  # the figure to use
    basis: str
    gap_pct: Decimal | None  # net income against EPS x shares, signed, two digits
    consolidated: Decimal | None
    reason: str | None = None

    def inputs(self) -> dict[str, object]:
        """What a metric's `inputs` shows: the figure used, the consolidated one and the label."""
        out: dict[str, object] = {
            "net_income": self.value, "net_income_consolidated": self.consolidated,
            "profit_basis": self.basis, "profit_gap_pct": self.gap_pct,
        }  # fmt: skip
        if self.reason:
            out["profit_reason"] = self.reason
        return out


def attributable_profit(
    net_income: Decimal | None,
    *,
    minority_interest: Decimal | None = None,
    eps: Decimal | None = None,
    shares_out: Decimal | None = None,
) -> Attributable:
    """The profit to use for one period from that period's own figures (see the module doc)."""
    if net_income is None:
        return Attributable(None, UNKNOWN, None, None, "no net income filed for the period")
    if minority_interest is not None:
        return Attributable(net_income - minority_interest, REPORTED_MINORITY, None, net_income)
    missing = [n for n, v in (("eps", eps), ("shares_out", shares_out)) if v is None]
    if missing or eps is None or shares_out is None:
        return _unknown(net_income, f"no {' and '.join(missing)} for the period")
    with localcontext(dmath.CONTEXT):
        parent = eps * shares_out
        if parent <= 0:
            return _unknown(net_income, "no positive EPS x shares for the period")
        gap = dmath.quantize((net_income - parent) / parent * dmath.HUNDRED, _GAP)
    if abs(gap) <= GAP_TOLERANCE_PCT:
        return Attributable(net_income, NO_MINORITY, gap, net_income)
    if gap > 0:
        return Attributable(parent, EPS_X_SHARES, gap, net_income)
    return Attributable(
        net_income, UNKNOWN, gap, net_income,
        f"net income is {gap:+.2f}% against EPS x shares, which minority interest cannot explain; "
        "it can come from weighted EPS against period-end shares, buybacks or a data mismatch, "
        "so the consolidated figure is kept",
    )  # fmt: skip


def _unknown(net_income: Decimal, why: str) -> Attributable:
    return Attributable(
        net_income, UNKNOWN, None, net_income, f"minority interest not available: {why}"
    )
