"""`nivesh mf ...` (E5 mutual fund intelligence). Registered by main.py. Read-only."""

from datetime import date
from decimal import Decimal
from typing import Annotated

import typer

from nivesh_adapters import analysis_service as svc
from nivesh_adapters.mf_data import MfHoldingsClient, MfMetaClient
from nivesh_adapters.mf_ingest import (
    FundInputs,
    HoldingsReport,
    MetaReport,
    NavReport,
    ingest_holdings,
    ingest_meta,
    ingest_nav,
)
from nivesh_adapters.mf_report import (
    DiscoverReport,
    FundReport,
    ValuationReport,
    discover,
    run_doctor,
)
from nivesh_adapters.nav import AmfiNavAll, MfapiClient
from nivesh_cli.common import user_errors
from nivesh_cli.market import stores
from nivesh_core.errors import NiveshError
from nivesh_core.timeutil import ist_date, utcnow
from nivesh_engine.fund_screen import Constraints
from nivesh_engine.mf_returns import FundAnalytics

mf_app = typer.Typer(no_args_is_help=True, help="Mutual fund data and analytics (read-only).")


def _mfapi() -> MfapiClient:  # seams: tests inject MockTransport clients
    return MfapiClient()


def _navall() -> AmfiNavAll:
    return AmfiNavAll()


def _meta_client() -> MfMetaClient:
    return MfMetaClient()


def _holdings_client(source: str, key_ref: str) -> MfHoldingsClient:
    return MfHoldingsClient(source=source, key_ref=key_ref)


def _today() -> date:  # seam: tests pin the date
    return ist_date(utcnow())


def _show(value: object, unit: str = "") -> str:
    return "unavailable" if value is None else f"{value}{unit}"


def _print_nav(rep: NavReport) -> None:
    typer.echo(f"{rep.amfi_code} {rep.scheme}: added {rep.added}, source {rep.source}")
    typer.echo(f"last NAV date: {rep.last_date.isoformat() if rep.last_date else 'none'}")
    if rep.gap_note:
        typer.echo(f"gaps: {rep.gap_note}")
    elif rep.gaps:
        for g in rep.gaps:
            typer.echo(
                f"gap: {g.gap_start.isoformat()} to {g.gap_end.isoformat()} "
                f"({g.missing_days} weekdays without NAV)"
            )
    else:
        typer.echo("gaps: none")
    for line in rep.flags:
        typer.echo(f"flag: {line}")
    if rep.stale:
        typer.echo("note: some data is stale (source failed, cached copy used)")
    for line in rep.failed:
        typer.echo(f"failed source {line}", err=True)


@mf_app.command("nav")
def mf_nav(
    ctx: typer.Context,
    codes: Annotated[list[str], typer.Argument(help="AMFI scheme code(s).")],
    refresh: Annotated[bool, typer.Option(help="Bypass the NAV cache.")] = False,
    cross_check: Annotated[
        bool, typer.Option(help="Also read the fallback source and flag disagreements.")
    ] = False,
) -> None:
    """Fetch NAV history (MFapi, AMFI NAVAll fallback), store it incrementally, flag gaps."""
    bad = 0
    with user_errors(), stores(ctx) as st:
        for code in codes:
            try:
                _print_nav(
                    ingest_nav(
                        st.duck,
                        st.sql,
                        st.settings,
                        code,
                        refresh=refresh,
                        cross_check=cross_check,
                        mfapi=_mfapi,
                        navall=_navall,
                    )  # fmt: skip
                )
            except NiveshError as e:
                typer.echo(f"error: {e}", err=True)
                bad += 1
    if bad:
        raise typer.Exit(1)


def _print_meta(rep: MetaReport) -> None:
    m = rep.meta
    typer.echo(f"{m.amfi_code} {m.scheme_name} (as of {m.as_of.isoformat()}, source {m.source})")
    typer.echo(
        f"category: {_show(m.category)}; plan: {_show(m.plan)}; option: {_show(m.option)}; "
        f"AMC: {_show(m.amc)}"
    )
    typer.echo(
        f"TER: {_show(m.expense_ratio, '%')}; AUM: {_show(m.aum_crore, ' crore')}; "
        f"benchmark: {_show(m.benchmark)}"
    )
    since = m.manager_since.isoformat() if m.manager_since else None
    typer.echo(f"manager: {_show(m.manager)} since {_show(since)}")
    if rep.twin is not None:
        who = rep.twin.amfi_code or ", ".join(rep.twin.candidates) or "none"
        extra = f" ({rep.twin.reason})" if rep.twin.reason else ""
        typer.echo(f"direct twin: {rep.twin.status} {who}{extra}")
    if rep.cost is not None:
        c = rep.cost
        gap = _show(c.ter_gap_pct, " percentage points")
        if c.available:
            typer.echo(
                f"TER cost: {gap}; INR {c.inr_per_year} per year on current value "
                f"INR {rep.value_inr}"
            )
        else:
            typer.echo(f"TER cost: {c.reason}; gap {gap}")
    for line in rep.failed:
        typer.echo(f"failed source {line}", err=True)


@mf_app.command("meta")
def mf_meta(
    ctx: typer.Context,
    codes: Annotated[list[str], typer.Argument(help="AMFI scheme code(s).")],
    refresh: Annotated[bool, typer.Option(help="Bypass the metadata cache.")] = False,
) -> None:
    """Store scheme metadata; for a regular plan show its direct twin and the TER cost."""
    bad = 0
    with user_errors(), stores(ctx) as st:
        for code in codes:
            try:
                _print_meta(
                    ingest_meta(
                        st.duck, st.sql, st.settings, code, refresh=refresh, meta=_meta_client
                    )
                )
            except NiveshError as e:
                typer.echo(f"error: {e}", err=True)
                bad += 1
    if bad:
        raise typer.Exit(1)


def _print_holdings(rep: HoldingsReport) -> None:
    typer.echo(f"{rep.amfi_code}: months stored {rep.months_stored}")
    for m in rep.months:
        typer.echo(
            f"{m.month_end.isoformat()}: mapped {m.mapped_pct}%, unmapped {m.unmapped_pct}%, "
            f"other {m.other_pct}% ({m.lines} lines)"
        )
    if rep.coverage_note:
        typer.echo(rep.coverage_note)
    if rep.stale:
        typer.echo("note: some data is stale (source failed, cached copy used)")
    for line in rep.failed:
        typer.echo(f"failed source {line}", err=True)


@mf_app.command("holdings")
def mf_holdings(
    ctx: typer.Context,
    codes: Annotated[list[str], typer.Argument(help="AMFI scheme code(s).")],
    refresh: Annotated[bool, typer.Option(help="Bypass the holdings cache.")] = False,
) -> None:
    """Store monthly portfolio holdings, map ISINs through the master, report unmapped weight."""
    bad = 0
    with user_errors(), stores(ctx) as st:
        for code in codes:
            try:
                _print_holdings(
                    ingest_holdings(
                        st.duck,
                        st.sql,
                        st.settings,
                        code,
                        refresh=refresh,
                        holdings=_holdings_client,
                    )  # fmt: skip
                )
            except NiveshError as e:
                typer.echo(f"error: {e}", err=True)
                bad += 1
    if bad:
        raise typer.Exit(1)


def _print_returns(inp: FundInputs, res: FundAnalytics) -> None:
    typer.echo(f"{inp.amfi_code} {inp.name}: {res.points} NAV points")
    if not res.comparable:
        typer.echo(f"not comparable: {res.reason}")
        return
    if inp.benchmark_symbol and inp.benchmark_reason is None:
        typer.echo(
            f"benchmark: {inp.benchmark_symbol} ({res.benchmark_label}); aligned with "
            f"{_show(res.coverage_pct, '%')} of NAV dates"
        )
    else:
        typer.echo(f"benchmark: unavailable ({inp.benchmark_reason})")
    for w in res.rolling:
        head = f"window {w.window_days}d: {w.windows} windows"
        if w.reason:
            typer.echo(f"{head}; returns unavailable ({w.reason})")
        else:
            typer.echo(
                f"{head}; median return {w.median_return_pct}%, min {w.min_return_pct}%, "
                f"max {w.max_return_pct}%"
            )
        if w.relative_reason is None:
            typer.echo(
                f"  beat benchmark in {w.beat_pct}% of windows; median excess "
                f"{w.median_excess_pct}%"
            )
        else:
            typer.echo(f"  vs benchmark unavailable ({w.relative_reason})")
    typer.echo(f"std dev (annualised): {_metric(res.std_dev_pct, res.std_dev_reason, '%')}")
    if res.max_drawdown:
        d = res.max_drawdown
        typer.echo(
            f"max drawdown: {d.pct}% (peak {d.peak_date.isoformat()}, "
            f"trough {d.trough_date.isoformat()})"
        )
    else:
        typer.echo(f"max drawdown: unavailable ({res.max_drawdown_reason})")
    typer.echo(
        "downside capture: " + _metric(res.downside_capture_pct, res.downside_capture_reason, "%")
    )
    typer.echo(f"Sortino: {_metric(res.sortino, res.sortino_reason)}")


def _print_valuation(val: ValuationReport) -> None:
    if val.pe is None or val.pb is None:
        typer.echo(f"valuation: unavailable ({val.why_no_ratio()})")
        return
    month = val.month_end.isoformat() if val.month_end else "n/a"
    typer.echo(
        f"valuation of holdings at {month} ({val.stocks_with_data} of {val.stocks_total} stocks "
        "had price and statement data):"
    )
    for label, now, hist in (("P/E", val.pe, val.pe_history), ("P/B", val.pb, val.pb_history)):
        if now.value is None:
            typer.echo(f"  {label}: unavailable ({now.reason})")
            continue
        head = f"  {label}: {now.value} (weights covered {now.coverage_pct}%)"
        if hist is None or hist.reason or hist.ratio is None:
            why = hist.reason if hist else "no history"
            typer.echo(f"{head}; own history unavailable ({why})")
        else:
            typer.echo(
                f"{head}; own {hist.months_used}-month history median {hist.median}, min "
                f"{hist.minimum}, max {hist.maximum}; now {hist.ratio}x median ({hist.label})"
            )


def _metric(value: object, reason: str | None, unit: str = "") -> str:
    return f"unavailable ({reason})" if value is None else f"{value}{unit}"


@mf_app.command("returns")
def mf_returns(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="AMFI scheme code.")],
    benchmark: Annotated[
        str | None, typer.Option(help="Index symbol; default from mf.benchmarks.")
    ] = None,
) -> None:
    """Rolling returns, consistency vs the benchmark (a price index, not TRI) and risk."""
    with user_errors(), stores(ctx) as st:
        rep = svc.mf_returns_report(st.duck, st.sql, st.settings, code, benchmark)
    inp, res, val = rep.inputs, rep.analytics, rep.valuation
    _print_returns(inp, res)
    _print_valuation(val)


@mf_app.command("overlap")
def mf_overlap(ctx: typer.Context) -> None:
    """Pairwise overlap of owned funds and look-through exposure by stock and sector."""
    with user_errors(), stores(ctx) as st:
        rep = svc.overlap_report(st.duck, st.sql, st.settings)
    book, matrix, lt = rep.book, rep.matrix, rep.look_through
    for line in book.skipped:
        typer.echo(f"skipped: {line}", err=True)
    typer.echo("overlap (sum of min weights over common equity ISINs; latest stored month)")
    if len(matrix.codes) < 2:
        typer.echo("fewer than two funds with stored holdings; no pairs to compare")
    for i, a in enumerate(matrix.codes):
        for b in matrix.codes[i + 1 :]:
            o = matrix.get(a, b)
            typer.echo(f"{a} vs {b}: {o.overlap_pct}% ({o.common_isins} common ISINs)")
    typer.echo(
        f"look-through of INR {lt.total_inr} (top {len(lt.stocks)} of {lt.stock_count} stocks)"
    )
    for s in lt.stocks:
        who = ", ".join(f"{c.source} {c.exposure_inr}" for c in s.contributions)
        typer.echo(
            f"{s.isin} {s.sector or 'sector unmapped'}: INR {s.exposure_inr} "
            f"({s.exposure_pct}%) [{who}]"
        )
    for sec in lt.sectors:
        typer.echo(f"sector {sec.sector}: INR {sec.exposure_inr} ({sec.exposure_pct}%)")
    typer.echo(f"sector unmapped: INR {lt.unmapped_sector_inr} ({lt.unmapped_sector_pct}%)")
    typer.echo(f"other (cash, debt, derivatives, funds): INR {lt.other_inr} ({lt.other_pct}%)")
    for e in lt.excluded:
        typer.echo(f"excluded {e.amfi_code} {e.name}: {e.reason}")


def _print_exit_load(rep: FundReport) -> None:
    e = rep.verdict.exit_load
    if e is None:
        return
    if e.available:
        typer.echo(
            f"  exit load: INR {e.amount_inr} ({e.percent}% on {e.lots_in_load} lot(s) held "
            f"under {e.days} days)"
        )
    else:
        typer.echo(f"  exit load: unavailable ({e.reason})")


def _print_tax(rep: FundReport) -> None:
    t = rep.verdict.tax_impact
    if t is None:
        return
    if not t.lots:
        typer.echo(f"  tax impact: unavailable ({t.reason})")
        return
    typer.echo(
        f"  tax impact (informational): gain INR {_show(t.total_gain)}; "
        f"tax INR {_show(t.total_tax_inr)}" + (f" ({t.reason})" if t.total_tax_inr is None else "")
    )
    for lot in t.lots:
        term = {None: "term unknown", True: "long term", False: "short term"}[lot.long_term]
        typer.echo(
            f"    lot {lot.acquired_on.isoformat()}: {lot.holding_days} days, {term}, "
            f"gain INR {_show(lot.gain)}, tax INR {_show(lot.tax_inr)}"
        )


def _print_verdict(rep: FundReport) -> None:
    f, v = rep.facts, rep.verdict
    typer.echo(f"{f.amfi_code} {f.name} (plan {_show(f.plan)})")
    typer.echo(
        f"  trailing 1y return (display only, never a reason): {_show(f.trailing_1y_pct, '%')}"
    )
    _print_exit_load(rep)
    _print_tax(rep)
    typer.echo(f"  action: {v.action.value}")
    for r in v.reasons:
        value = "unavailable" if r.value is None else r.value
        limit = "" if r.threshold is None else f" (limit {r.threshold})"
        note = f" [{r.note}]" if r.note else ""
        typer.echo(f"  reason {r.code.value}: {r.metric} {value}{limit}{note}")
    for d in v.deferred:
        typer.echo(f"  deferred: {d}")


@mf_app.command("doctor")
def mf_doctor(
    ctx: typer.Context,
    code: Annotated[str | None, typer.Argument(help="One AMFI code; default all held.")] = None,
) -> None:
    """KEEP / SWITCH_TO_DIRECT / REPLACE / CONSOLIDATE / REVIEW per held fund, with reasons."""
    with user_errors():
        with stores(ctx) as st:
            rep = run_doctor(st.duck, st.sql, st.settings, today=_today(), only=code)
        for line in rep.skipped:
            typer.echo(f"skipped: {line}", err=True)
        if not rep.funds:
            raise NiveshError(
                "no held mutual funds to review" if code is None else f"{code} is not held"
            )
        for fr in rep.funds:
            _print_verdict(fr)


def _print_discovery(rep: DiscoverReport) -> None:
    typer.echo(
        f"category {rep.category!r}: {rep.considered} stored scheme(s) screened "
        "(the schemes you have loaded, not the whole market)"
    )
    if not rep.result.ranked:
        typer.echo(f"no candidates ({rep.result.reason or 'none passed'})")
    for n, r in enumerate(rep.result.ranked, start=1):
        c = r.candidate
        typer.echo(f"{n}. {r.amfi_code} {r.name}: score {r.score} (lower is better)")
        typer.echo(
            f"   ranks: consistency {r.ranks['consistency']}, downside {r.ranks['downside']}, "
            f"cost {r.ranks['cost']}, valuation {r.ranks['valuation']}"
        )
        typer.echo(
            f"   beat benchmark {_show(c.beat_pct, '%')}, median excess "
            f"{_show(c.median_excess_pct, '%')}, downside capture "
            f"{_show(c.downside_capture_pct, '%')}, TER {_show(c.ter_pct, '%')}, "
            f"valuation ratio {_show(c.valuation_ratio)}"
        )
        if r.overlap is None:
            typer.echo("   overlap vs owned funds: unavailable (no stored holdings to compare)")
        else:
            typer.echo(
                f"   overlap vs owned funds: {r.overlap.overlap_pct}% with {r.overlap.with_fund}"
            )
        if r.unavailable:
            typer.echo(f"   unavailable (ranked last): {', '.join(r.unavailable)}")
    for x in rep.result.rejected:
        typer.echo(f"rejected {x.amfi_code} {x.name}: {'; '.join(x.reasons)}")
    for line in rep.not_loaded:
        typer.echo(f"not loaded {line}")


@mf_app.command("discover")
def mf_discover(
    ctx: typer.Context,
    category: Annotated[str, typer.Option(help="Category text as stored in the metadata.")],
    max_ter: Annotated[float | None, typer.Option(help="Maximum TER, percent.")] = None,
    min_aum: Annotated[float | None, typer.Option(help="Minimum AUM, crore rupees.")] = None,
    min_tenure: Annotated[float | None, typer.Option(help="Minimum manager tenure, years.")] = None,
) -> None:
    """Up to five direct-plan candidates of a category, ranked, with overlap vs owned funds."""
    k = Constraints(
        min_aum_crore=None if min_aum is None else Decimal(str(min_aum)),
        max_ter=None if max_ter is None else Decimal(str(max_ter)),
        min_tenure_years=None if min_tenure is None else Decimal(str(min_tenure)),
    )
    with user_errors(), stores(ctx) as st:
        rep = discover(st.duck, st.sql, st.settings, category, k)
    _print_discovery(rep)
