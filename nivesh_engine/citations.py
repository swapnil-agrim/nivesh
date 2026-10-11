"""Citation check (ST-10.6): every number in a report must trace to a tool output or an engine
result, within the rounding of its last displayed digit. Pure and exact: Decimal only.

A figure is a number as written (`1,23,456.70`, `12.5%`, `₹1.20 crore`, `2026-01-02`). It
matches evidence when it equals an evidence value, or the value read in the unit it was
written in, within half a unit of its last displayed digit. A derived figure (a percent change
nobody registered) is never inferred: it stays unmatched and is listed. Evidence next to a
redacted span cannot confirm anything.
"""

import re
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

SCALES = {
    "lakh": Decimal(10) ** 5, "crore": Decimal(10) ** 7, "cr": Decimal(10) ** 7,
    "k": Decimal(10) ** 3, "m": Decimal(10) ** 6, "bn": Decimal(10) ** 9,
    "thousand": Decimal(10) ** 3, "million": Decimal(10) ** 6, "billion": Decimal(10) ** 9,
}  # fmt: skip
INDIAN = ("lakh", "crore", "cr")
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
REDACTED = "[REDACTED]"
ALL = "all"  # evidence scope that serves every security
ONE = Decimal(1)
HUNDRED = Decimal(100)

_ISO = re.compile(r"(?<![\w-])(\d{4})-(\d{2})-(\d{2})(?![\w-])")
_DMY = re.compile(r"(?<![\w])(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})(?![\w])")
_NUM = re.compile(
    r"(?<![\w.])(?P<sign>[-+−](?=\d))?"
    r"(?P<cur>[₹$]|USD|INR)?"
    r"(?P<sign2>[-+−](?=\d))?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<unit>%|x(?![A-Za-z])|lakh(?![A-Za-z])|crore(?![A-Za-z])|cr(?![A-Za-z])"
    r"|k(?![A-Za-z])|m(?![A-Za-z])|bn(?![A-Za-z])|thousand(?![A-Za-z])|million(?![A-Za-z])"
    r"|billion(?![A-Za-z])))?"
)
_ORDINAL = re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?P<n>\d{1,3})[.)](?=\s)", re.M)
_EVIDENCE_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Figure:
    """One number or date as written. `value` carries the sign; `scale` is the unit's
    multiplier (1 for none, percent and `x`)."""

    text: str
    start: int
    value: Decimal | None
    decimals: int
    unit: str
    scale: Decimal
    day: date | None = None

    @property
    def is_date(self) -> bool:
        return self.day is not None


def _spans_of_dates(text: str) -> list[tuple[int, int, date]]:
    out: list[tuple[int, int, date]] = []
    for m in _ISO.finditer(text):
        try:
            out.append((m.start(), m.end(), date(int(m[1]), int(m[2]), int(m[3]))))
        except ValueError:
            continue
    for m in _DMY.finditer(text):
        month = MONTHS.get(m[2].lower())
        if month is None:
            continue
        try:
            out.append((m.start(), m.end(), date(int(m[3]), month, int(m[1]))))
        except ValueError:
            continue
    return out


def figures(text: str) -> list[Figure]:
    """Numbers and dates in `text`, left to right. Skipped: a number glued to letters
    (`SMA200`, `Q3FY26`, `3G`) and a list ordinal at the start of a line."""
    dates = _spans_of_dates(text)
    ordinals = {m.start("n") for m in _ORDINAL.finditer(text)}
    out = [Figure(text[a:b], a, None, 0, "", ONE, d) for a, b, d in dates]
    for m in _NUM.finditer(text):
        a, b = m.start("num"), m.end()
        if a in ordinals or any(s <= a < e for s, e, _ in dates):
            continue
        before = text[m.start() - 1] if m.start() > 0 else ""
        after = text[m.end("num") : m.end("num") + 1]
        if before.isalpha() or (after.isalpha() and not m["unit"]):
            continue
        raw = m["num"]
        value = Decimal(raw.replace(",", ""))
        if any(c in "-−" for c in (m["sign"] or "") + (m["sign2"] or "")):
            value = -value
        unit = m["unit"] or ""
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        out.append(Figure(text[m.start() : b], m.start(), value, decimals,
                         unit, SCALES.get(unit, ONE)))  # fmt: skip
    return sorted(out, key=lambda t: t.start)


@dataclass(frozen=True)
class Miss:
    """A number that no evidence supports."""

    block_id: str
    figure: str
    context: str
    reason: str


@dataclass(frozen=True)
class Validation:
    checked: int
    unmatched: tuple[Miss, ...]

    @property
    def clean(self) -> bool:
        return not self.unmatched


@dataclass
class Evidence:
    """Numbers and dates that code or tools produced, each tagged with the security they
    belong to (or `all`). A scope matches its own evidence and the shared one."""

    numbers: dict[str, list[Decimal]] = field(default_factory=dict)
    days: dict[str, set[date]] = field(default_factory=dict)
    registered: set[tuple[Decimal, int, str]] = field(default_factory=set)
    redacted: bool = False
    _sorted: bool = False

    def add_number(self, value: Decimal, scope: str = ALL) -> None:
        self.numbers.setdefault(scope, []).append(value)
        self._sorted = False

    def add_day(self, day: date, scope: str = ALL) -> None:
        self.days.setdefault(scope, set()).add(day)

    def add_registered(self, value: Decimal, decimals: int, unit: str) -> None:
        """A number the renderer produced itself: it always matches as written."""
        self.registered.add((abs(value), decimals, unit))

    def add_text(self, text: str, scope: str = ALL) -> None:
        """Every number and ISO date in a tool output. A number touching a redacted span is
        not evidence (its digits are gone or cut), and the pool remembers redaction."""
        if REDACTED in text:
            self.redacted = True
        for _, _, d in _spans_of_dates(text):
            self.add_day(d, scope)
        flat = _ISO.sub(" ", text)
        for m in _EVIDENCE_NUM.finditer(flat):
            near = flat[max(0, m.start() - len(REDACTED)) : m.end() + len(REDACTED)]
            if REDACTED in near and (
                flat[max(0, m.start() - len(REDACTED)) : m.start()].endswith(REDACTED)
                or flat[m.end() : m.end() + len(REDACTED)].startswith(REDACTED)
            ):
                continue
            try:
                self.add_number(Decimal(m.group(0)), scope)
            except InvalidOperation:
                continue

    def add_json(self, obj: Any, scope: str = ALL) -> None:
        """Numbers and dates inside a parsed JSON value (strings included)."""
        if isinstance(obj, dict):
            for v in obj.values():
                self.add_json(v, scope)
        elif isinstance(obj, list | tuple):
            for v in obj:
                self.add_json(v, scope)
        elif isinstance(obj, bool) or obj is None:
            return
        elif isinstance(obj, int | Decimal):
            self.add_number(Decimal(obj), scope)
        elif isinstance(obj, str):
            self.add_text(obj, scope)

    def near(self, scope: str, target: Decimal, window: Decimal) -> bool:
        """Is there evidence within `window` of `target` in this scope or the shared one?"""
        if not self._sorted:
            for pool in self.numbers.values():
                pool.sort()
            self._sorted = True
        for key in {scope, ALL}:
            pool = self.numbers.get(key, [])
            i = bisect_left(pool, target - window)
            if i < len(pool) and pool[i] <= target + window:
                return True
        return False

    def has_day(self, scope: str, day: date) -> bool:
        return day in self.days.get(scope, ()) or day in self.days.get(ALL, ())


def _window(decimals: int, max_digits: int) -> Decimal:
    """Half a unit of the last displayed digit; digits beyond `max_digits` are not held to a
    tighter window."""
    return Decimal(5).scaleb(-(min(decimals, max_digits) + 1))


def matches(figure: Figure, evidence: Evidence, scope: str = ALL, max_digits: int = 6) -> bool:
    """True when the figure is registered, or equals evidence in some reading of its unit."""
    if figure.day is not None:
        return evidence.has_day(scope, figure.day)
    if figure.value is None:
        return False
    v, w = figure.value, _window(figure.decimals, max_digits)
    if (abs(v), figure.decimals, figure.unit) in evidence.registered:
        return True
    readings = [(v, w)]
    if figure.unit == "%":
        readings.append((v / HUNDRED, w / HUNDRED))
    if figure.scale != ONE:
        raw, raw_w = v * figure.scale, w * figure.scale
        readings.append((raw, raw_w))
        if figure.unit in INDIAN:
            for other in ("lakh", "crore"):
                readings.append((raw / SCALES[other], raw_w / SCALES[other]))
    return any(evidence.near(scope, t, win) for t, win in readings)


def validate(
    blocks: Iterable[tuple[str, str, str]], evidence: Evidence, max_digits: int = 6
) -> Validation:
    """`blocks` are (block id, scope, text). Every number and date is checked; the unmatched
    are listed with their context."""
    checked = 0
    misses: list[Miss] = []
    for block_id, scope, text in blocks:
        for t in figures(text):
            checked += 1
            if matches(t, evidence, scope, max_digits):
                continue
            ctx = " ".join(text[max(0, t.start - 24) : t.start + len(t.text) + 24].split())
            why = "no evidence in the run"
            if evidence.redacted:
                why = "cannot verify: some evidence was redacted"
            misses.append(Miss(block_id, t.text, ctx, why))
    return Validation(checked, tuple(misses))
