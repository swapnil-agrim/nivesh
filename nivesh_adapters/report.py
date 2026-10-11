"""One report model with two serialisers: Markdown and self-contained HTML (ST-10.1).

Model and news text is untrusted: every serialiser strips control characters, HTML is escaped,
and no link or script is ever built from text. Numbers the code derives itself are registered
as `Num` so the citation check can match them without guessing.
"""

import hashlib
import html
import json
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from nivesh_core.paths import write_private
from nivesh_core.timeutil import IST, to_iso
from nivesh_core.trace import Tracer
from nivesh_engine.money import inr_text

BANNER = "DRAFT - UNVERIFIED NUMBERS"
SUMMARY_LINES = 5
NONE_GIVEN = "(none given)"
FOOTER_HEAD = (
    "Personal research generated with AI for the owner’s own decisions. Not investment advice."
)


def footer_text(as_of: tuple[date, ...]) -> str:
    """CR-4 footer; the dates are the report's as-of set."""
    dates = ", ".join(d.isoformat() for d in as_of) or "n/a"
    return f"{FOOTER_HEAD} Data as of {dates}."


def clean(text: str) -> str:
    """Control characters removed (newline and tab kept)."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")


def _line(text: str) -> str:
    return " ".join(clean(text).split())


@dataclass(frozen=True)
class Num:
    """A number the renderer produced itself: value as displayed (in `unit`), its text and the
    decimals shown. Stored in displayed units so no long raw digit run reaches a saved file."""

    value: Decimal
    text: str
    decimals: int
    unit: str = ""

    @classmethod
    def show(cls, value: Decimal, decimals: int, unit: str = "") -> "Num":
        q = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
        sep = " " if unit in ("lakh", "crore") else ""
        return cls(q, f"{q:,.{decimals}f}{sep}{unit}", decimals, unit)

    @classmethod
    def inr(cls, amount: Decimal) -> "Num":
        text = inr_text(amount)
        body = text.removeprefix("-").removeprefix("₹")
        unit = body.split(" ")[1] if " " in body else ""
        value = Decimal(body.split(" ")[0].replace(",", ""))
        return cls(-value if text.startswith("-") else value, text, 2, unit)

    def as_json(self) -> dict[str, Any]:
        return {"decimals": self.decimals, "text": self.text, "unit": self.unit,
                "value": str(self.value)}  # fmt: skip


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    scopes: tuple[str, ...] = ()  # the security each row belongs to; "all" when not given


@dataclass(frozen=True)
class Block:
    """A body section. `checked` text is held to the citation check; labels are not."""

    id: str
    heading: str
    text: str
    checked: bool = True
    scope: str = "all"  # the security whose evidence this text may cite


@dataclass(frozen=True)
class Report:
    title: str
    run_at: datetime
    as_of: tuple[date, ...]
    summary: tuple[str, ...]
    decision: Table
    blocks: tuple[Block, ...]
    gaps: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    nums: tuple[Num, ...] = ()
    dates: tuple[date, ...] = ()  # dates the code shows besides `as_of` (e.g. review date)
    run_id: int | None = None
    banner: str = ""
    unmatched: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.summary) > SUMMARY_LINES:
            raise ValueError(f"summary has {len(self.summary)} lines; at most {SUMMARY_LINES}")
        object.__setattr__(self, "as_of", tuple(sorted(set(self.as_of))))

    @property
    def summary_lines(self) -> tuple[str, ...]:
        """Exactly five lines: short summaries are padded, never longer."""
        pad = (NONE_GIVEN,) * (SUMMARY_LINES - len(self.summary))
        return tuple(_line(s) for s in self.summary) + pad

    @property
    def footer(self) -> str:
        return footer_text(self.as_of)

    def run_time(self) -> str:
        return self.run_at.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")

    def checked_texts(self) -> list[tuple[str, str, str]]:
        """(location id, scope, text) for everything the citation check must verify."""
        out = [(f"summary.{i}", "all", s) for i, s in enumerate(self.summary, start=1)]
        for r, row in enumerate(self.decision.rows, start=1):
            scope = self.decision.scopes[r - 1] if r <= len(self.decision.scopes) else "all"
            out += [(f"decision.{r}.{c}", scope, cell) for c, cell in enumerate(row, start=1)]
        return out + [(b.id, b.scope, b.text) for b in self.blocks if b.checked]


def _cell(text: str) -> str:
    return _line(text).replace("|", "\\|")


def to_markdown(r: Report) -> str:
    out: list[str] = []
    if r.banner:
        out += [f"> **{r.banner}**", ""]
        out += [f"> - {_line(u)}" for u in r.unmatched] + [""]
    out += [f"# {_line(r.title)}", ""]
    run = f"Run time: {r.run_time()}" + (f" (run {r.run_id})" if r.run_id is not None else "")
    asof = ", ".join(d.isoformat() for d in r.as_of) or "n/a"
    out += [run, "", f"Data as of: {asof}", "", "## Summary", ""]
    out += [f"{i}. {s}" for i, s in enumerate(r.summary_lines, start=1)] + [""]
    out += ["## Decision table", "", "| " + " | ".join(_cell(h) for h in r.decision.headers) + " |"]
    out += ["| " + " | ".join("---" for _ in r.decision.headers) + " |"]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in r.decision.rows] + [""]
    for b in r.blocks:
        out += [f"## {_line(b.heading)}", "", clean(b.text).strip(), ""]
    out += ["## Data gaps", ""] + ([f"- {_line(g)}" for g in r.gaps] or ["- none"]) + [""]
    out += ["## Sources", ""] + ([f"- {_line(s)}" for s in r.sources] or ["- none"]) + [""]
    out += ["---", "", r.footer, ""]
    return "\n".join(out)


_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;"
    "line-height:1.5}table{border-collapse:collapse;width:100%}"
    "th,td{border:1px solid #888;padding:.3rem .5rem;text-align:left}"
    ".draft{border:2px solid #b00;padding:.5rem 1rem;color:#b00}footer{margin-top:2rem;"
    "font-size:.9rem;color:#555}"
)


def _e(text: str) -> str:
    return html.escape(clean(text))


def _paras(text: str) -> str:
    paras = [p for p in clean(text).strip().split("\n\n") if p.strip()]
    return "".join(f"<p>{'<br>'.join(html.escape(x) for x in p.split(chr(10)))}</p>" for p in paras)


def to_html(r: Report) -> str:
    h: list[str] = ["<!DOCTYPE html>", '<html lang="en"><head><meta charset="utf-8">']
    h += [f"<title>{_e(_line(r.title))}</title><style>{_STYLE}</style></head><body>"]
    if r.banner:
        items = "".join(f"<li>{_e(_line(u))}</li>" for u in r.unmatched)
        h += [f'<div class="draft"><strong>{_e(r.banner)}</strong><ul>{items}</ul></div>']
    run = f"Run time: {r.run_time()}" + (f" (run {r.run_id})" if r.run_id is not None else "")
    asof = ", ".join(d.isoformat() for d in r.as_of) or "n/a"
    h += [f"<h1>{_e(_line(r.title))}</h1><p>{_e(run)}</p><p>Data as of: {_e(asof)}</p>"]
    h += ["<h2>Summary</h2><ol>" + "".join(f"<li>{_e(s)}</li>" for s in r.summary_lines) + "</ol>"]
    head = "".join(f"<th>{_e(_line(c))}</th>" for c in r.decision.headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(_line(c))}</td>" for c in row) + "</tr>"
        for row in r.decision.rows
    )
    h += [f"<h2>Decision table</h2><table><tr>{head}</tr>{body}</table>"]
    for b in r.blocks:
        h += [f"<h2>{_e(_line(b.heading))}</h2>{_paras(b.text)}"]
    gaps = "".join(f"<li>{_e(_line(g))}</li>" for g in r.gaps) or "<li>none</li>"
    srcs = "".join(f"<li>{_e(_line(s))}</li>" for s in r.sources) or "<li>none</li>"
    h += [f"<h2>Data gaps</h2><ul>{gaps}</ul><h2>Sources</h2><ul>{srcs}</ul>"]
    h += [f"<footer>{_e(r.footer)}</footer></body></html>", ""]
    return "\n".join(h)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self._skip = tag in ("style", "title")

    def handle_endtag(self, tag: str) -> None:
        self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(doc: str) -> str:
    """Visible text of a report page (used to compare the two serialisations)."""
    p = _Text()
    p.feed(doc)
    return "\n".join(p.parts)


def to_json(r: Report) -> str:
    """Exact, sorted-key JSON; Decimals travel as strings."""
    data = {
        "banner": r.banner,
        "blocks": [{"checked": b.checked, "heading": b.heading, "id": b.id, "scope": b.scope,
                    "text": b.text} for b in r.blocks],
        "as_of": [d.isoformat() for d in r.as_of],
        "dates": [d.isoformat() for d in r.dates],
        "decision": {"headers": list(r.decision.headers),
                     "rows": [list(x) for x in r.decision.rows],
                     "scopes": list(r.decision.scopes)},
        "gaps": list(r.gaps),
        "nums": [n.as_json() for n in r.nums],
        "run_at": to_iso(r.run_at),
        "run_id": r.run_id,
        "sources": list(r.sources),
        "summary": list(r.summary),
        "title": r.title,
        "unmatched": list(r.unmatched),
    }  # fmt: skip
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def from_json(text: str) -> Report:
    d = json.loads(text)
    return Report(
        title=d["title"], run_at=datetime.fromisoformat(d["run_at"]),
        as_of=tuple(date.fromisoformat(x) for x in d["as_of"]), summary=tuple(d["summary"]),
        decision=Table(tuple(d["decision"]["headers"]),
                       tuple(tuple(x) for x in d["decision"]["rows"]),
                       tuple(d["decision"]["scopes"])),
        blocks=tuple(Block(b["id"], b["heading"], b["text"], b["checked"], b["scope"])
                      for b in d["blocks"]),
        dates=tuple(date.fromisoformat(x) for x in d["dates"]),
        gaps=tuple(d["gaps"]), sources=tuple(d["sources"]),
        nums=tuple(Num(Decimal(n["value"]), n["text"], n["decimals"], n["unit"])
                   for n in d["nums"]),
        run_id=d["run_id"], banner=d["banner"], unmatched=tuple(d["unmatched"]),
    )  # fmt: skip


FILES = ("report.md", "report.html", "report_input.json", "report.json")


def save_report(
    run_dir: Path, report: Report, facts: dict[str, Any], tracer: Tracer | None = None
) -> list[Path]:
    """Save the report beside its trace, owner-only and never over an existing file.

    `report_input.json` holds only facts the code produced (engine results, prices, rates,
    coverage), never model text: it is evidence for the citation check. `report.json` is the
    report itself, for later delivery. The trace gets a name and digest per file, not content.
    """
    contents = {
        "report.md": to_markdown(report),
        "report.html": to_html(report),
        "report_input.json": json.dumps(facts, sort_keys=True, default=str, ensure_ascii=False),
        "report.json": to_json(report),
    }
    paths: list[Path] = []
    for name, text in contents.items():
        write_private(run_dir / name, text)
        paths.append(run_dir / name)
        if tracer is not None:
            tracer.saved("report", name, hashlib.sha256(text.encode()).hexdigest())
    return paths
