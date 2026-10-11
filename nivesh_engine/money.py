"""Exact money text for reports: INR in lakh and crore, USD with grouping (ST-10.1).

Amounts are Decimal, rounded half up to two decimals. Text never carries a long raw digit run:
a lakh or crore unit keeps the mantissa short, and the rate date is passed in by the caller.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

LAKH = 10**5
CRORE = 10**7
_CENT = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _indian(whole: int) -> str:
    s = str(whole)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head, *groups, tail])


def _fixed(value: Decimal, grouper: str) -> str:
    whole, frac = f"{value:.2f}".split(".")
    return (_indian(int(whole)) if grouper == "in" else f"{int(whole):,}") + "." + frac


def inr_text(amount: Decimal) -> str:
    """Rupee text: plain Indian grouping below a lakh, then `n.nn lakh`, then `n.nn crore`."""
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if _q(a) < LAKH:
        return f"{sign}₹{_fixed(_q(a), 'in')}"
    lakh = _q(a / LAKH)
    if lakh < 100:
        return f"{sign}₹{lakh:.2f} lakh"
    return f"{sign}₹{_fixed(_q(a / CRORE), 'in')} crore"


def usd_text(amount: Decimal) -> str:
    """Dollar text with thousands grouping and two decimals."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${_fixed(_q(abs(amount)), 'us')}"


def usd_with_inr(usd: Decimal, rate: Decimal, rate_date: date) -> str:
    """`$100.00 (₹8,500.00 at 85.00 INR/USD on 2026-01-02)`: the rate and its date are shown."""
    return (
        f"{usd_text(usd)} ({inr_text(usd * rate)} at {_q(rate):.2f} INR/USD on "
        f"{rate_date.isoformat()})"
    )
