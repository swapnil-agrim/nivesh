"""`engine`: read-only access to the deterministic analysis engines (#68, minimum scope). Every
number comes from the pure engines in `nivesh_engine`; this module only opens the stores for
reading, calls `nivesh_adapters.analysis_service` and returns the result with `as_of` and
`source`. Decimals are emitted as JSON numbers; a figure that cannot be computed says why.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from nivesh_adapters import analysis_service as svc
from nivesh_core.profile import Profile, load_profile
from nivesh_core.timeutil import ist_date
from nivesh_mcp import common
from nivesh_mcp.base import ReadOnlyServer
from nivesh_mcp.common import MarketCtx, env, parse_day

server = ReadOnlyServer("engine")
SOURCE = "nivesh-engine"


def _day(as_of: str | None) -> date:
    return parse_day(as_of, "as_of") if as_of else ist_date(common.now())


def _profile() -> Profile:
    return load_profile(Path(os.environ.get("NIVESH_CONFIG_DIR", "config")) / "profile.yaml")


@contextmanager
def _stores() -> Iterator[MarketCtx]:
    with common.market_ctx() as m:
        m.sql.execute("PRAGMA query_only = ON")
        yield m


def _out(subject: str | None, day: date, result: Any) -> dict[str, Any]:
    data = {"subject": subject, "result": svc.plain(result, number=common.num)}
    return env(data, day, SOURCE)


@server.tool
def ta_compute(security: str, as_of: str | None = None) -> dict[str, Any]:
    """Technical indicators, support and resistance levels and the positional setup (type,
    entry zone, stop, invalidation) for one security from stored daily bars up to `as_of`
    (ISO date, default today)."""
    day = _day(as_of)
    with _stores() as m:
        rep = svc.ta_report(m.duck, m.sql, m.settings, security, day)
    return _out(rep.symbol, day, rep.result)


@server.tool
def fa_compute(security: str, as_of: str | None = None) -> dict[str, Any]:
    """Growth, profitability, balance sheet and cash quality figures for one security, using
    only filings available on `as_of` (ISO date, default today)."""
    day = _day(as_of)
    with _stores() as m:
        rep = svc.fa_report(m.duck, m.sql, m.settings, security, day)
    return _out(rep.symbol, day, rep.result)


@server.tool
def valuation_range(security: str, as_of: str | None = None) -> dict[str, Any]:
    """Valuation multiples with own-history percentile and peer median, plus the reverse-DCF
    fair-value range for one security as of `as_of` (ISO date, default today)."""
    day = _day(as_of)
    with _stores() as m:
        rep = svc.valuation_report(m.duck, m.sql, m.settings, security, day)
    return _out(rep.symbol, day, rep.result)


@server.tool
def red_flags(security: str, as_of: str | None = None) -> dict[str, Any]:
    """Accounting and governance red flags for one security, each with status, severity and
    evidence; a flag that cannot be tested says so. Uses data available on `as_of`."""
    day = _day(as_of)
    with _stores() as m:
        rep = svc.flag_report(m.duck, m.sql, m.settings, security, day)
    return _out(rep.symbol, day, rep.result)


@server.tool
def risk_metrics(
    candidate: str | None = None, weight_pct: str | None = None, as_of: str | None = None
) -> dict[str, Any]:
    """Volatility, drawdown, beta and days to trade of the stored holdings, and with a
    `candidate` security plus its proposed `weight_pct` (percent, above 0 and below 100) the
    pro-forma weights and limit breaches. Both must be given together."""
    day = _day(as_of)
    with _stores() as m:
        result = svc.risk_report(m.duck, m.sql, m.settings, _profile(), day, candidate, weight_pct)
    return _out(candidate, day, result)


@server.tool
def portfolio_xray(as_of: str | None = None) -> dict[str, Any]:
    """Allocation, drift, concentration, limit checks, look-through and XIRR of the stored
    holdings as of `as_of` (ISO date, default today)."""
    day = _day(as_of)
    with _stores() as m:
        result = svc.xray_report(m.duck, m.sql, m.settings, _profile(), day)
    return _out(None, day, result)


@server.tool
def score(security: str, horizon: str = "long_term", as_of: str | None = None) -> dict[str, Any]:
    """Composite 0-100 score card of one security (factors, band, cap, input coverage), ranked
    inside the stored universe. `horizon` is long_term or positional."""
    day = _day(as_of)
    with _stores() as m:
        one = svc.score_one(m.duck, m.sql, m.settings, security, horizon, day)
    return _out(one.symbol, day, one)


@server.tool
def mf_analyse(scheme: str) -> dict[str, Any]:
    """Rolling returns, consistency against the benchmark, risk, holdings valuation and, when
    the scheme is held, the fund-doctor verdict for one mutual fund by AMFI code."""
    day = _day(None)
    with _stores() as m:
        rep = svc.mf_returns_report(m.duck, m.sql, m.settings, scheme)
        doctor = svc.doctor_report(m.duck, m.sql, m.settings, scheme.strip(), day)
    result = {
        "scheme": rep.inputs.amfi_code, "name": rep.inputs.name, "option": rep.inputs.option,
        "meta": rep.inputs.meta, "benchmark": rep.inputs.benchmark_symbol,
        "benchmark_reason": rep.inputs.benchmark_reason, "analytics": rep.analytics,
        "valuation": rep.valuation, "doctor": doctor,
    }  # fmt: skip
    return _out(rep.inputs.amfi_code, day, result)


@server.tool
def mf_overlap() -> dict[str, Any]:
    """Pairwise overlap of the owned mutual funds and the look-through exposure by stock and
    sector, from the latest stored holdings of each fund."""
    day = _day(None)
    with _stores() as m:
        rep = svc.overlap_report(m.duck, m.sql, m.settings)
    codes = rep.matrix.codes
    pairs = [
        {"a": a, "b": b, "overlap_pct": rep.matrix.get(a, b).overlap_pct,
         "common_isins": rep.matrix.get(a, b).common_isins}
        for i, a in enumerate(codes) for b in codes[i + 1 :]
    ]  # fmt: skip
    result = {"pairs": pairs, "look_through": rep.look_through, "skipped": rep.book.skipped}
    return _out(None, day, result)


@server.tool
def get_fund_meta(scheme: str) -> dict[str, Any]:
    """Stored scheme metadata for one mutual fund by AMFI code: name, AMC, category, plan,
    option, expense ratio, AUM, benchmark and manager."""
    day = _day(None)
    with _stores() as m:
        meta = svc.fund_meta(m.duck, m.sql, scheme)
    return _out(scheme.strip(), meta.as_of, {"meta": meta, "today": day})
