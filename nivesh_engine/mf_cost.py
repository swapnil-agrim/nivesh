"""Scheme identity and cost (ST-5.2): plan and option from the scheme name, the direct-plan twin
of a regular scheme, and the TER difference in percent and rupees per year.

Pure and Decimal-only: no I/O, no clock, no float. Scheme names are untrusted external text; they
are only normalised and compared here, never interpolated into any instruction.
"""

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

Plan = Literal["direct", "regular"]
Option = Literal["growth", "idcw", "other"]

_IDCW_WORDS = {"idcw", "dividend", "div", "payout", "reinvestment", "reinvest"}
_STRIP = _IDCW_WORDS | {"direct", "dir", "regular", "plan", "growth", "option", "of"}
CENT = Decimal("0.01")


def _tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.casefold())


def plan_of(name: str) -> Plan | None:
    """direct / regular from the name; unknown stays None (it is not assumed regular)."""
    words = set(_tokens(name))
    if words & {"direct", "dir"}:
        return "direct"
    return "regular" if "regular" in words else None


def option_of(name: str) -> Option:
    words = set(_tokens(name))
    if words & _IDCW_WORDS:
        return "idcw"
    return "growth" if "growth" in words else "other"


def normalise_scheme(name: str) -> str:
    """Case-folded name without plan, option and punctuation words (`&` reads as `and`)."""
    words = _tokens(name.replace("&", " "))
    return " ".join(w for w in words if w not in _STRIP)


@dataclass(frozen=True)
class SchemeInfo:
    amfi_code: str
    name: str
    amc: str | None = None

    @property
    def plan(self) -> Plan | None:
        return plan_of(self.name)

    @property
    def option(self) -> Option:
        return option_of(self.name)


@dataclass(frozen=True)
class TwinResult:
    status: Literal["found", "ambiguous", "none"]
    amfi_code: str | None
    candidates: list[str] = field(default_factory=list)
    reason: str | None = None


def resolve_direct_twin(regular: SchemeInfo, universe: list[SchemeInfo]) -> TwinResult:
    """The direct-plan scheme with the same AMC, normalised name and option; never a guess."""
    if regular.plan is None:
        return TwinResult("none", None, [], "plan is unknown (no Direct/Regular in the name)")
    if regular.plan != "regular":
        return TwinResult("none", None, [], "scheme is not a regular plan")
    want = normalise_scheme(regular.name)
    found = sorted(
        u.amfi_code
        for u in universe
        if u.amfi_code != regular.amfi_code
        and u.plan == "direct"
        and u.option == regular.option
        and normalise_scheme(u.name) == want
        and (u.amc is None or regular.amc is None or u.amc.casefold() == regular.amc.casefold())
    )
    if len(found) == 1:
        return TwinResult("found", found[0], found)
    if found:
        return TwinResult("ambiguous", None, found, f"ambiguous: {len(found)} direct candidates")
    return TwinResult(
        "none", None, [], "no direct plan with the same name and option in the master"
    )


@dataclass(frozen=True)
class CostResult:
    available: bool
    ter_gap_pct: Decimal | None
    inr_per_year: Decimal | None
    reason: str | None = None


def ter_cost(
    regular_ter: Decimal | None,
    direct_ter: Decimal | None,
    value_inr: Decimal | None,
    *,
    twin: TwinResult | None = None,
) -> CostResult:
    """Regular minus direct TER (percent) and rupees per year on the current value."""
    if regular_ter is None or direct_ter is None or value_inr is None:
        missing = []
        if regular_ter is None:
            missing.append("regular TER")
        if direct_ter is None:
            missing.append("no direct twin" if twin and twin.status != "found" else "direct TER")
        if value_inr is None:
            missing.append("current value")
        return CostResult(False, None, None, "unavailable: " + ", ".join(missing))
    gap = regular_ter - direct_ter
    if gap <= 0:
        return CostResult(False, gap, None, "no saving: the direct TER is not lower")
    inr = (value_inr * gap / 100).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return CostResult(True, gap, inr)
