"""The result cell of every analysis engine: a value, or a reason it is not available.

A metric that cannot be computed is `available=False` with a stated reason, never zero and never
silently absent. `inputs` names the figures the value was derived from (for the audit trail).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from nivesh_engine import dmath


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    available: bool
    reason: str | None = None
    inputs: Mapping[str, object] = field(default_factory=dict)


def ok(value: Decimal, **inputs: object) -> Metric:
    return Metric(value, True, None, dict(inputs))


def na(reason: str, **inputs: object) -> Metric:
    if not reason:
        raise ValueError("a missing metric must say why")
    return Metric(None, False, reason, dict(inputs))


def coverage_pct(metrics: Iterable[Metric]) -> Decimal | None:
    """Share of metrics that are available, in percent (two digits); None for no metrics."""
    cells = list(metrics)
    if not cells:
        return None
    have = sum(1 for m in cells if m.available)
    return dmath.quantize(dmath.HUNDRED * have / len(cells), Decimal("0.01"))
