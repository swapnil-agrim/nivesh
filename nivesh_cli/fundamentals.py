"""`nivesh fundamentals compare SYMBOL` (ADR-0016): the rows each provider stored for one security,
side by side. Read-only over the stored rows: no vendor adapter is built, no request is made, no
ledger row is written, and nothing is averaged or merged. A difference above the tolerance from
`fundamentals.compare` is named with both stored values; which figure to trust is the owner's call.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import duckdb
import typer

from nivesh_adapters import analysis_service as svc
from nivesh_adapters.providers import Capability, ProviderInfo, provider_infos
from nivesh_cli.common import user_errors
from nivesh_cli.engine import AsJson, AsOf, Symbol, _as_of, emit, reader
from nivesh_core.config import Settings
from nivesh_core.fundamentals_config import CompareSettings, FundamentalsSettings
from nivesh_core.market_store import get_shareholding_by_source, get_statement_rows_by_source
from nivesh_engine.crosscheck import (
    Comparison,
    Difference,
    compare,
    shareholding_figures,
    statement_figures,
)

fundamentals_app = typer.Typer(
    no_args_is_help=True, help="Stored fundamentals across providers (read-only)."
)
LIST_CAP = 20  # lines per list in the text output; --json has everything


@dataclass(frozen=True)
class CrossCheck:
    providers: tuple[ProviderInfo, ...]  # those with stored rows as of the date, most trusted first
    enabled: frozenset[str]
    tolerance: CompareSettings
    statements: Comparison
    shareholding: Comparison
    other_sources: tuple[str, ...]  # stored by sources no provider claims: never compared


def cross_check(
    duck: duckdb.DuckDBPyConnection, fundamentals: FundamentalsSettings, security_id: int,
    day: date,
) -> CrossCheck:  # fmt: skip
    """Compare what each provider stored for a security as known on `day` (stored rows only)."""
    infos = provider_infos(fundamentals)
    by_stmt = get_statement_rows_by_source(duck, security_id, day)
    by_share = get_shareholding_by_source(duck, security_id, day)
    s_figs = {i.name: statement_figures(by_stmt, i.sources, day) for i in infos}
    h_figs = {i.name: shareholding_figures(by_share, i.sources) for i in infos}
    order = [i.name for i in infos]
    tol = fundamentals.compare
    claimed = {s for i in infos for s in i.sources}
    return CrossCheck(
        tuple(i for i in infos if s_figs[i.name] or h_figs[i.name]),
        frozenset(s.name for s in fundamentals.providers if s.enabled),
        tol,
        compare(s_figs, order, tol.statements_tolerance_pct),
        compare(h_figs, order, tol.shareholding_tolerance_pp, points=True),
        tuple(sorted((by_stmt.keys() | by_share.keys()) - claimed)),
    )


def _num(v: Decimal, sign: str = "") -> str:
    return format(v.normalize(), f"{sign},f")


def _diff_line(d: Difference, points: bool) -> str:
    gap = f"{_num(d.delta, '+')} pp" if points else "base is zero"
    if not points and d.pct is not None:
        gap = f"{_num(d.pct, '+')}%"
    return f"  {d.figure}: {d.base_provider} {_num(d.base)} vs {d.provider} {_num(d.value)} ({gap})"


def _listed(names: tuple[str, ...]) -> str:
    more = f" and {len(names) - LIST_CAP} more" if len(names) > LIST_CAP else ""
    return ", ".join(names[:LIST_CAP]) + more


def _section(name: str, c: Comparison, points: bool) -> list[str]:
    if not c.providers:
        return []
    if c.single_provider:
        return [f"{name}: only {c.providers[0]} holds any; nothing to compare"]
    out = [
        f"{name}: {c.compared} compared against the first provider that holds each, "
        f"{c.agree} agree, {len(c.disagreements)} differ"
    ]
    out += [_diff_line(d, points) for d in c.disagreements[:LIST_CAP]]
    if len(c.disagreements) > LIST_CAP:
        out.append(f"  and {len(c.disagreements) - LIST_CAP} more, use --json")
    out += [f"  only in {p}: {_listed(keys)}" for p, keys in c.only_in.items()]
    return out


def _providers(cc: CrossCheck) -> list[str]:
    lines = ["providers with stored rows, most trusted first:"]
    for p in cc.providers:
        off = "" if p.name in cc.enabled else " (disabled)"
        caps = ", ".join(c for c in Capability if c in p.capabilities)
        lines += [
            f"  {p.name}{off}: {p.label} ({caps})",
            f"    revenue: {p.revenue_basis}",
            f"    profit: {p.profit_basis}",
            f"    filing dates: {p.filing_dates}",
        ]
    if len(cc.providers) == 1:
        return [
            *lines,
            f"single provider with stored rows ({cc.providers[0].name}): nothing to compare",
        ]
    t = cc.tolerance
    lines.append(
        f"tolerance: statements {_num(t.statements_tolerance_pct)}%, "
        f"shareholding {_num(t.shareholding_tolerance_pp)} pp"
    )
    return [*lines, *_section("statements", cc.statements, False),
            *_section("shareholding", cc.shareholding, True)]  # fmt: skip


def render(cc: CrossCheck, symbol: str) -> list[str]:
    """The text report (the lines `fa --cross-check` appends too)."""
    lines = _providers(cc) if cc.providers else [f"no provider has stored rows for {symbol}"]
    if cc.other_sources:
        lines.append(f"other stored sources (not compared): {', '.join(cc.other_sources)}")
    return lines


def cross_check_lines(
    duck: duckdb.DuckDBPyConnection, settings: Settings, security_id: int, symbol: str, day: date
) -> list[str]:
    """The comparison as an appendix to another report (`fa --cross-check`)."""
    found = cross_check(duck, settings.fundamentals, security_id, day)
    return ["", "cross-check across providers:", *render(found, symbol)]


def _result(cc: CrossCheck) -> dict[str, object]:
    t = cc.tolerance
    return {
        "providers": [
            {
                "name": p.name, "label": p.label, "enabled": p.name in cc.enabled,
                "sources": list(p.sources),
                "capabilities": [c.value for c in Capability if c in p.capabilities],
                "keyed_on": p.keyed_on.value, "revenue_basis": p.revenue_basis,
                "profit_basis": p.profit_basis, "filing_dates": p.filing_dates,
            }
            for p in cc.providers
        ],
        "tolerance": {
            "statements_pct": t.statements_tolerance_pct,
            "shareholding_pp": t.shareholding_tolerance_pp,
        },
        "single_provider": len(cc.providers) <= 1,
        "statements": cc.statements,
        "shareholding": cc.shareholding,
        "other_sources": list(cc.other_sources),
    }  # fmt: skip


@fundamentals_app.command("compare")
def compare_cmd(
    ctx: typer.Context, security: Symbol, as_of: AsOf = None, as_json: AsJson = False
) -> None:
    """What each provider stored for one security, side by side, with the declared basis of each.

    Reads stored rows only (no vendor call, nothing written). A difference above the tolerance is
    named with both values; nothing is averaged or merged."""
    with user_errors(), reader(ctx) as r:
        day = _as_of(as_of)
        sec = svc.resolve_security(r.sql, security)
        cc = cross_check(r.duck, r.settings.fundamentals, sec.id, day)
    if as_json:
        emit("fundamentals compare", sec.symbol, day, _result(cc), True)
        return
    typer.echo(f"fundamentals compare {sec.symbol} as of {day.isoformat()}")
    typer.echo("\n".join(render(cc, sec.symbol)))
