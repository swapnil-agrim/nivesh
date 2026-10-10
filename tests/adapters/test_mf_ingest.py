import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.mf_ingest import find_nav_gaps, ingest_nav
from nivesh_adapters.nav import AmfiNavAll, MfapiClient
from nivesh_adapters.quality import DataQualityError
from nivesh_core.errors import NiveshError
from nivesh_core.mf_models import NavPoint
from nivesh_core.mf_store import get_gaps, get_nav, last_nav_date, upsert_nav
from tests.mf_fx import FX, MON, Env, Net, make_env, mfapi_doc, navall_text, weekdays

D = Decimal
TODAY = date(2026, 3, 2)
SOURCES = {"mfapi", "amfi_navall"}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    with make_env(tmp_path) as e:
        yield e


def run(env: Env, net: Net, *, refresh: bool = False, today: date = TODAY, **kw: object) -> object:
    return ingest_nav(
        env.duck, env.sql, env.settings, "100001", refresh=refresh, today=today,
        mfapi=lambda: MfapiClient(net.client()), navall=lambda: AmfiNavAll(net.client()),
        **kw,
    )  # type: ignore[arg-type]  # fmt: skip


def sid(env: Env) -> int:
    row = env.master.by_amfi_code("100001")
    assert row is not None
    return row.id


def series(n: int, start: date = MON) -> dict[date, str]:
    return {d: f"{10 + i}.0000" for i, d in enumerate(weekdays(start, n))}


def test_first_run_stores_full_history(env: Env) -> None:
    pts = series(10)
    rep = run(env, Net(mfapi={"100001": mfapi_doc(pts)}))
    assert rep.added == 10 and rep.source == "mfapi"  # type: ignore[attr-defined]
    assert rep.last_date == max(pts)  # type: ignore[attr-defined]
    stored = get_nav(env.duck, sid(env))
    assert len(stored) == 10 and stored[0].nav == D("10.0000")


def test_second_run_adds_only_dates_after_last_nav_date(env: Env) -> None:
    run(env, Net(mfapi={"100001": mfapi_doc(series(10))}))
    rep = run(env, Net(mfapi={"100001": mfapi_doc(series(13))}), refresh=True)
    assert rep.added == 3  # type: ignore[attr-defined]
    assert len(get_nav(env.duck, sid(env))) == 13


def test_rerun_without_new_dates_adds_zero(env: Env) -> None:
    net = Net(mfapi={"100001": mfapi_doc(series(10))})
    run(env, net)
    rep = run(env, net, refresh=True)
    assert rep.added == 0 and rep.last_date == max(series(10))  # type: ignore[attr-defined]


def test_cached_fetch_uses_nav_ttl(env: Env) -> None:
    net = Net(mfapi={"100001": mfapi_doc(series(5))})
    run(env, net)
    run(env, net)  # within the 1d nav TTL: served from the cache
    assert len(net.requests) == 1
    run(env, net, refresh=True)
    assert len(net.requests) == 2


def test_fallback_to_navall_when_primary_unavailable_and_failure_recorded(env: Env) -> None:
    net = Net(mfapi_status=503, navall=navall_text({"100001": ("12.5000", date(2026, 3, 2))}))
    rep = run(env, net)
    assert rep.source == "amfi_navall" and rep.added == 1  # type: ignore[attr-defined]
    assert any("503" in f for f in rep.failed)  # type: ignore[attr-defined]
    assert get_nav(env.duck, sid(env))[0].source == "amfi_navall"


def test_rate_limited_primary_also_falls_back(env: Env) -> None:
    net = Net(mfapi_status=429, navall=navall_text({"100001": ("12.5000", date(2026, 3, 2))}))
    assert run(env, net).source == "amfi_navall"  # type: ignore[attr-defined]


def test_data_quality_error_from_primary_is_not_masked_by_fallback(env: Env) -> None:
    bad = mfapi_doc({MON: "0"})
    net = Net(mfapi={"100001": bad}, navall=navall_text({"100001": ("12.5000", date(2026, 3, 2))}))
    with pytest.raises(DataQualityError):
        run(env, net)
    assert last_nav_date(env.duck, sid(env)) is None


def test_navall_fallback_appends_one_point_per_run(env: Env) -> None:
    for day in (date(2026, 3, 2), date(2026, 3, 3)):
        net = Net(mfapi_status=503, navall=navall_text({"100001": ("12.5000", day)}))
        rep = run(env, net, refresh=True, today=day)
        assert rep.added == 1  # type: ignore[attr-defined]
    assert len(get_nav(env.duck, sid(env))) == 2
    same = run(env, net, refresh=True, today=date(2026, 3, 3))
    assert same.added == 0  # type: ignore[attr-defined]


def test_both_sources_failing_is_an_error(env: Env) -> None:
    net = Net(mfapi_status=503, navall_status=503)
    with pytest.raises(NiveshError, match="no NAV source"):
        run(env, net)


def test_code_missing_from_navall_is_reported(env: Env) -> None:
    net = Net(mfapi_status=503, navall=navall_text({"999999": ("1.0", date(2026, 3, 2))}))
    with pytest.raises(NiveshError, match="no NAV source"):
        run(env, net)


def test_navall_na_row_counts_as_skipped(env: Env) -> None:
    net = Net(mfapi_status=503, navall=navall_text({"100001": ("N.A.", date(2026, 3, 2))}))
    with pytest.raises(NiveshError, match="no NAV source"):
        run(env, net)


def test_cross_source_disagreement_beyond_tolerance_is_flagged_primary_wins(env: Env) -> None:
    day = date(2026, 3, 2)
    upsert_nav(env.duck, sid(env), [NavPoint(date=day, nav=D("100"), source="amfi_navall")])
    net = Net(mfapi={"100001": mfapi_doc({day: "101"})})
    rep = run(env, net)
    assert len(rep.flags) == 1 and "2026-03-02" in rep.flags[0]  # type: ignore[attr-defined]
    assert get_nav(env.duck, sid(env))[0].nav == D("101")  # primary wins
    net2 = Net(mfapi={"100001": mfapi_doc({day: "100.2"})})  # 0.2 percent < 0.5 percent
    assert run(env, net2, refresh=True).flags == []  # type: ignore[attr-defined]


def test_cross_check_fetches_secondary_for_latest_point(env: Env) -> None:
    day = date(2026, 3, 2)
    net = Net(
        mfapi={"100001": mfapi_doc({day: "101"})},
        navall=navall_text({"100001": ("90.0000", day)}),
    )
    rep = run(env, net, cross_check=True)
    assert len(rep.flags) == 1  # type: ignore[attr-defined]
    assert len(net.requests) == 2


def test_unknown_amfi_code_error_names_master_build(env: Env) -> None:
    with pytest.raises(NiveshError, match="nivesh master build"):
        ingest_nav(env.duck, env.sql, env.settings, "424242", today=TODAY)


def test_backfill_by_primary_after_amfi_only_run_recomputes_gaps(env: Env) -> None:
    net = Net(mfapi_status=503, navall=navall_text({"100001": ("12.0000", date(2026, 2, 27))}))
    first = run(env, net, today=date(2026, 2, 27))
    assert first.gaps == [] and first.gap_note  # type: ignore[attr-defined]  # AMFI-only: skipped
    full = {d: "12.0000" for d in weekdays(date(2026, 2, 2), 20) if d <= date(2026, 2, 27)}
    del full[date(2026, 2, 9)], full[date(2026, 2, 10)], full[date(2026, 2, 11)]
    del full[date(2026, 2, 12)], full[date(2026, 2, 13)], full[date(2026, 2, 16)]
    rep = run(env, Net(mfapi={"100001": mfapi_doc(full)}), refresh=True, today=date(2026, 2, 27))
    assert [(g.gap_start, g.gap_end, g.missing_days) for g in rep.gaps] == [  # type: ignore[attr-defined]
        (date(2026, 2, 9), date(2026, 2, 16), 6)
    ]
    fill = {**full, **{d: "12.0000" for d in weekdays(date(2026, 2, 9), 6)}}
    rep2 = run(env, Net(mfapi={"100001": mfapi_doc(fill)}), refresh=True, today=date(2026, 2, 27))
    assert rep2.gaps == [] and get_gaps(env.duck, sid(env)) == []  # type: ignore[attr-defined]


def test_gaps_stored_and_not_filled(env: Env) -> None:
    pts = {d: "10.0000" for d in weekdays(MON, 3)}  # 5, 6, 7 Jan
    pts[date(2026, 1, 15)] = "10.0000"  # 8, 9, 12, 13, 14 missing: exactly 5
    pts[date(2026, 1, 26)] = "10.0000"  # 16..23 missing: six weekdays
    rep = run(env, Net(mfapi={"100001": mfapi_doc(pts)}), today=date(2026, 1, 26))
    stored = get_gaps(env.duck, sid(env))
    assert stored == rep.gaps  # type: ignore[attr-defined]
    assert [(g.gap_start, g.gap_end) for g in stored] == [(date(2026, 1, 16), date(2026, 1, 23))]
    assert len(get_nav(env.duck, sid(env))) == len(pts)  # nothing was filled in


def test_gap_days_threshold_comes_from_config(tmp_path: Path) -> None:
    with make_env(tmp_path, nav_gap_days=2) as env:
        pts = {MON: "10", date(2026, 1, 9): "10"}  # 6, 7, 8 missing = 3 > 2
        rep = run(env, Net(mfapi={"100001": mfapi_doc(pts)}), today=date(2026, 1, 9))
        assert len(rep.gaps) == 1  # type: ignore[attr-defined]


# ---- pure gap finder -------------------------------------------------------------------------
def gaps(dates: list[date], holidays: frozenset[date] = frozenset(), limit: int = 5,
         until: date | None = None) -> list[tuple[date, date, int]]:  # fmt: skip
    return [
        (g.gap_start, g.gap_end, g.missing_days)
        for g in find_nav_gaps(dates, holidays, limit, until)
    ]


def test_gap_of_exactly_five_missing_weekdays_not_flagged() -> None:
    # Mon 5 -> Tue 13: missing 6, 7, 8, 9, 12 = exactly 5 weekdays
    assert gaps([date(2026, 1, 5), date(2026, 1, 13)]) == []


def test_gap_of_six_missing_weekdays_flagged_with_range() -> None:
    assert gaps([date(2026, 1, 5), date(2026, 1, 14)]) == [(date(2026, 1, 6), date(2026, 1, 13), 6)]


def test_weekend_alone_is_not_a_gap() -> None:
    assert gaps([date(2026, 1, 9), date(2026, 1, 12)]) == []
    assert gaps([date(2026, 1, 9), date(2026, 1, 12)], limit=1) == []


def test_configured_holiday_does_not_count_toward_gap() -> None:
    d = [date(2026, 1, 5), date(2026, 1, 14)]  # 6 missing weekdays
    assert len(gaps(d)) == 1
    assert gaps(d, frozenset({date(2026, 1, 8)})) == []  # one is a holiday: 5 left


def test_year_without_holiday_config_does_not_raise(env: Env) -> None:
    pts = {date(2025, 3, 3): "10", date(2025, 3, 4): "10"}
    net = Net(mfapi={"100001": mfapi_doc(pts)})
    rep = run(env, net, today=date(2025, 3, 5))
    assert rep.gaps == []  # type: ignore[attr-defined]


def test_tail_gap_after_last_nav_is_flagged() -> None:
    d = [date(2026, 1, 5)]
    assert gaps(d, until=date(2026, 1, 12)) == []  # 6,7,8,9,12 = 5 missing
    assert gaps(d, until=date(2026, 1, 13)) == [(date(2026, 1, 6), date(2026, 1, 13), 6)]
    assert gaps([], until=date(2026, 1, 13)) == []


def test_unsorted_or_duplicate_input_dates_are_normalised() -> None:
    d = [date(2026, 1, 14), date(2026, 1, 5), date(2026, 1, 5)]
    assert len(gaps(d)) == 1


def test_no_network_in_tests(env: Env) -> None:
    from nivesh_adapters.recorder import FixtureMissing

    with pytest.raises(FixtureMissing):  # the default client is a replay with no fixture
        ingest_nav(env.duck, env.sql, env.settings, "100001", today=TODAY)


# ---- metadata, twin and cost -----------------------------------------------------------------
from nivesh_adapters.mf_data import MfMetaClient  # noqa: E402
from nivesh_adapters.mf_ingest import held_funds, ingest_meta, scheme_universe  # noqa: E402
from nivesh_core.mf_store import latest_fund_meta  # noqa: E402
from tests.holdings_fx import holding  # noqa: E402
from tests.mf_fx import DG, G, meta_doc, save_holding  # noqa: E402

REG_NAME = "Example Bluechip Fund - Regular Plan - Growth"
DIR_NAME = "Example Bluechip Fund - Direct Plan - Growth"


def meta_net(**kw: object) -> Net:
    return Net(
        meta={
            "100001": meta_doc("100001", REG_NAME, ter="1.50"),
            "100010": meta_doc("100010", DIR_NAME, ter="0.60"),
        },
        **kw,  # type: ignore[arg-type]
    )


def run_meta(env: Env, net: Net, code: str = "100001", **kw: object) -> object:
    return ingest_meta(
        env.duck, env.sql, env.settings, code, today=TODAY,
        meta=lambda: MfMetaClient(net.client()), **kw,
    )  # type: ignore[arg-type]  # fmt: skip


def test_meta_stored_with_as_of_and_source(env: Env) -> None:
    rep = run_meta(env, meta_net())
    got = latest_fund_meta(env.duck, rep.security_id)  # type: ignore[attr-defined]
    assert got is not None and got.as_of == date(2026, 1, 12) and got.source == "mf_meta"
    assert got.expense_ratio == D("1.50") and got.plan == "regular"


def test_regular_holding_resolves_twin_ingests_its_meta_and_costs_it(env: Env) -> None:
    save_holding(env, G, quantity="100", price="1000")  # held on the growth ISIN: INR 100000
    rep = run_meta(env, meta_net())
    assert rep.twin.status == "found" and rep.twin.amfi_code == "100010"  # type: ignore[attr-defined]
    assert rep.twin_meta.expense_ratio == D("0.60")  # type: ignore[attr-defined]
    assert rep.value_inr == D("100000")  # type: ignore[attr-defined]
    assert (rep.cost.ter_gap_pct, rep.cost.inr_per_year) == (D("0.90"), D("900.00"))  # type: ignore[attr-defined]
    assert latest_fund_meta(env.duck, rep.security_id) is not None  # type: ignore[attr-defined]


def test_holding_on_the_non_preferred_isin_maps_to_the_same_scheme(env: Env) -> None:
    from tests.mf_fx import P

    save_holding(env, P, quantity="10", price="100")  # IDCW ISIN row, not the preferred one
    funds, skipped = held_funds(env.sql, env.settings)
    pref = env.master.by_amfi_code("100001")
    assert pref is not None and pref.isin == G
    assert [(f.amfi_code, f.nav_security_id, f.value_inr) for f in funds] == [
        ("100001", pref.id, D("1000"))
    ]
    assert skipped == []


def test_held_fund_without_amfi_code_is_skipped_not_guessed(env: Env) -> None:
    from nivesh_core.holdings_store import save_ingest
    from nivesh_core.timeutil import utcnow

    h = holding(
        isin="INF777Q01011", symbol="INF777Q01011", exchange="AMFI", asset_class="mf",
        name="Unlisted Fund", source="cas_rta", source_label="rta", price_basis="nav",
    )  # fmt: skip
    save_ingest(
        env.sql, kind="cas_rta", source_label="rta", digest=None, as_of=h.as_of, holdings=[h],
        txns=[], holder_refs=[""], warnings=[], now=utcnow(),
    )  # fmt: skip
    funds, skipped = held_funds(env.sql, env.settings)
    assert funds == [] and len(skipped) == 1 and "no AMFI code" in skipped[0]


def test_unheld_regular_fund_cost_is_unavailable_with_reason(env: Env) -> None:
    rep = run_meta(env, meta_net())
    assert rep.cost.available is False and "current value" in rep.cost.reason  # type: ignore[attr-defined]


def test_direct_plan_has_no_twin_or_cost(env: Env) -> None:
    rep = run_meta(env, meta_net(), "100010")
    assert rep.twin is None and rep.cost is None  # type: ignore[attr-defined]


def test_missing_twin_meta_gives_unavailable_cost_not_zero(env: Env) -> None:
    net = Net(meta={"100001": meta_doc("100001", REG_NAME, ter="1.50")})
    save_holding(env, G, quantity="100", price="1000")
    rep = run_meta(env, net)
    assert rep.cost.available is False and rep.cost.inr_per_year is None  # type: ignore[attr-defined]
    assert any("100010" in f for f in rep.failed)  # type: ignore[attr-defined]


def test_meta_unknown_code_and_dead_source(env: Env) -> None:
    with pytest.raises(NiveshError, match="nivesh master build"):
        run_meta(env, meta_net(), "424242")
    with pytest.raises(NiveshError, match="no metadata source"):
        run_meta(env, Net())


def test_scheme_universe_lists_one_row_per_code(env: Env) -> None:
    assert [s.amfi_code for s in scheme_universe(env.sql)] == ["100001", "100010"]


# ---- monthly holdings ------------------------------------------------------------------------
from nivesh_adapters.mf_data import MfHoldingsClient  # noqa: E402
from nivesh_adapters.mf_ingest import ingest_holdings  # noqa: E402
from nivesh_core.mf_store import get_fund_holdings, months_stored  # noqa: E402
from nivesh_core.security_master import build_master  # noqa: E402
from tests import pii_values as pv  # noqa: E402
from tests.market_fx import mrow  # noqa: E402

HOLD = json.loads((FX / "holdings_12m.json").read_text())


def seed_equities(env: Env) -> None:
    build_master(
        env.sql,
        [
            mrow("ALPHA", isin="INE000A01010", name="Example Alpha Industries"),
            mrow("BETA", isin="INE111A01011", name="Example Beta Bank"),
            mrow("GAMMA", isin="INE222B01012", name="Example Gamma Tech"),
        ],
        [],
    )


def run_holdings(env: Env, net: Net, code: str = "100001", **kw: object) -> object:
    return ingest_holdings(
        env.duck, env.sql, env.settings, code,
        holdings=lambda s, ref: MfHoldingsClient(net.client(), source=s, key_ref=ref), **kw,
    )  # type: ignore[arg-type]  # fmt: skip


def test_twelve_months_stored_for_one_fund(env: Env) -> None:
    seed_equities(env)
    rep = run_holdings(env, Net(holdings={"100001": HOLD}))
    assert rep.months_stored == 12 and rep.coverage_note is None  # type: ignore[attr-defined]
    assert len(months_stored(env.duck, sid(env))) == 12


def test_isin_maps_through_master_to_holding_security_id(env: Env) -> None:
    seed_equities(env)
    run_holdings(env, Net(holdings={"100001": HOLD}))
    rows = {r.isin: r for r in get_fund_holdings(env.duck, sid(env), date(2025, 12, 31))}
    alpha = env.master.lookup("INE000A01010").security_id
    assert alpha is not None and rows["INE000A01010"].holding_security_id == alpha
    assert rows["INE999Z01019"].holding_security_id is None  # not in the master


def test_unmapped_weight_reported_per_fund_month(env: Env) -> None:
    seed_equities(env)
    rep = run_holdings(env, Net(holdings={"100001": HOLD}))
    dec = next(m for m in rep.months if m.month_end == date(2025, 12, 31))  # type: ignore[attr-defined]
    assert (dec.mapped_pct, dec.unmapped_pct, dec.other_pct) == (D("75.85"), D("10.05"), D("14.10"))
    assert dec.lines == 6


def test_cash_and_derivative_lines_are_other_not_unmapped(env: Env) -> None:
    seed_equities(env)
    rep = run_holdings(env, Net(holdings={"100001": HOLD}))
    assert all(m.other_pct == D("14.10") for m in rep.months)  # type: ignore[attr-defined]
    kinds = {r.kind for r in get_fund_holdings(env.duck, sid(env), date(2025, 1, 31))}
    assert kinds == {"equity", "other"}


def test_nested_fund_line_is_reclassified_other(env: Env) -> None:
    seed_equities(env)
    doc = json.loads(json.dumps(HOLD))
    doc["months"] = doc["months"][-1:]
    doc["months"][0]["holdings"][0].update(isin=DG)  # a mutual fund held inside the fund
    rep = run_holdings(env, Net(holdings={"100001": doc}))
    nested = next(
        r for r in get_fund_holdings(env.duck, sid(env), date(2025, 12, 31)) if r.isin == DG
    )
    assert nested.kind == "other" and nested.holding_security_id is not None
    assert rep.months[0].other_pct == D("44.60")  # type: ignore[attr-defined]


def test_reingest_same_month_is_idempotent(env: Env) -> None:
    seed_equities(env)
    net = Net(holdings={"100001": HOLD})
    run_holdings(env, net)
    run_holdings(env, net, refresh=True)
    n = env.duck.execute("SELECT count(*) FROM fund_holding").fetchone()
    assert n == (72,)


def test_fewer_than_12_months_prints_coverage_shortfall(env: Env) -> None:
    seed_equities(env)
    doc = json.loads(json.dumps(HOLD))
    doc["months"] = doc["months"][-5:]
    rep = run_holdings(env, Net(holdings={"100001": doc}))
    assert rep.months_stored == 5 and "5 of 12" in rep.coverage_note  # type: ignore[attr-defined]


def test_holdings_source_comes_from_config_and_unknown_source_errors(tmp_path: Path) -> None:
    with make_env(tmp_path, holdings_source="amc") as env:
        seen: list[str] = []

        def make(source: str, ref: str) -> MfHoldingsClient:
            seen.append(source)
            return MfHoldingsClient(Net().client(), source=source, key_ref=ref)

        with pytest.raises(NiveshError):
            ingest_holdings(env.duck, env.sql, env.settings, "100001", holdings=make)
        assert seen == ["amc"]


def test_holdings_unknown_code_dead_source_and_bad_data(env: Env) -> None:
    with pytest.raises(NiveshError, match="nivesh master build"):
        run_holdings(env, Net(), "424242")
    with pytest.raises(NiveshError, match="no holdings source"):
        run_holdings(env, Net())
    bad = json.loads((FX / "holdings_bad.json").read_text())
    with pytest.raises(DataQualityError):
        run_holdings(env, Net(holdings={"100001": bad}))
    assert months_stored(env.duck, sid(env)) == []


def test_source_ref_never_logged_or_in_cache_params(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    seed_equities(env)
    monkeypatch.setenv("MFDATA_API_KEY", pv.mf_source_ref_value())
    env.settings.mf.holdings_source = "mfdata"
    run_holdings(env, Net(holdings={"100001": HOLD}))
    dump = repr(env.duck.execute("SELECT * FROM cache_entry").fetchall())
    assert pv.mf_source_ref_value() not in dump and pv.mf_source_ref_value() not in caplog.text
    assert pv.mf_param_name() not in dump
    sql_dump = repr(env.sql.execute("SELECT * FROM security").fetchall())
    assert pv.mf_source_ref_value() not in sql_dump


def test_holdings_fixtures_pii_clean() -> None:
    from nivesh_core.pii_scan import scan_paths

    assert scan_paths([FX]) == []
