"""The fundamentals provider seam (ADR-0016 D1, D12): registry and roles, then one contract suite
that every provider's harness (`tests/provider_fx.py`) must pass. Fixtures only, no network."""

import dataclasses
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from nivesh_adapters import fundamentals_tapetide
from nivesh_adapters.edgar import RateLimiter
from nivesh_adapters.fundamentals_tapetide import TapetideFundamentals
from nivesh_adapters.market_ingest import Run, ingest_fundamentals
from nivesh_adapters.providers import (
    BUILTIN,
    REGISTRY,
    Capability,
    CollectContext,
    FundamentalsProvider,
    ProviderInfo,
    ProviderResult,
    configured,
    provider_class,
    provider_infos,
    provider_sources,
    revenue_basis_of,
)
from nivesh_adapters.recorder import FixtureMissing
from nivesh_core.config import MarketSettings, Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import ConfigError
from nivesh_core.fundamentals_config import FundamentalsSettings, PlanSettings, ProviderSpec
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.market_store import write_fundamentals
from nivesh_core.security_master import SecurityMaster, SecurityRow, build_master
from tests import tapetide_fx as tx
from tests.adapters.test_fundamentals_ingest import Bse, india
from tests.conftest import MemoryKeyring
from tests.market_fx import mrow
from tests.provider_fx import (
    FAKE_CRED,
    HARNESSES,
    HOSTILE,
    TODAY,
    UPSTOX_CRED,
    FakeAdapter,
    FakeProvider,
    FakeVendor,
    Harness,
    Key,
    UpstoxVendor,
    fake_cred,
    upstox_adapter,
    upstox_cred,
)

BANK_ITEMS = {"gnpa_pct", "nnpa_pct", "nim_pct", "casa_pct", "car_pct"}


@dataclass
class World:
    sql: Any
    duck: duckdb.DuckDBPyConnection
    master: SecurityMaster
    settings: Settings
    sec: SecurityRow
    mp: pytest.MonkeyPatch


@pytest.fixture
def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> Iterator[World]:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(sql, [mrow("RELIANCE", isin="INE002A01018", bse_code="500325")], [])
    duck = open_duck(tmp_path / "nivesh.duckdb")
    master = SecurityMaster(sql)
    settings = Settings(data_dir=str(tmp_path), market=MarketSettings())
    # an adapter that builds its own pacing never sleeps in a test (a fake clock)
    monkeypatch.setattr(
        fundamentals_tapetide, "RateLimiter", lambda n: RateLimiter(n, lambda: 0.0, lambda s: None)
    )
    yield World(sql, duck, master, settings, master.by_symbol("RELIANCE")[0], monkeypatch)
    duck.close()
    sql.close()


# --- registry and roles ---


def test_capability_has_exactly_the_seven_declared_members() -> None:
    assert [c.value for c in Capability] == [
        "statements", "shareholding", "pledge", "actions", "estimates", "bank_metrics", "news",
    ]  # fmt: skip


def test_provider_info_is_frozen() -> None:
    info = provider_class("tapetide").info
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.name = "other"  # type: ignore[misc]


def test_the_builtin_providers_declare_their_identity_credential_and_metering() -> None:
    tape, bse = provider_class("tapetide").info, provider_class("bse_xbrl").info
    assert (tape.keyed_on, tape.credential, tape.metered) == ("nse_symbol", "TAPETIDE_TOKEN", True)
    assert (bse.keyed_on, bse.credential, bse.metered) == ("bse_code", None, False)
    assert tape.sources == ("tapetide", "tapetide_est") and bse.sources == ("bse_xbrl",)
    assert (tape.filing_dates, bse.filing_dates) == ("mixed", "declared")
    assert "minority interest" in tape.profit_basis and "net of excise" in tape.revenue_basis


def test_an_unknown_name_is_a_config_error_that_lists_the_registered_names() -> None:
    with pytest.raises(ConfigError, match="unknown fundamentals provider 'nosuch'") as e:
        provider_class("nosuch")
    assert "bse_xbrl" in str(e.value) and "tapetide" in str(e.value)


def test_a_registered_class_wins_and_is_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "fake", FakeProvider)
    assert provider_class("fake") is FakeProvider
    with pytest.raises(ConfigError, match="registered: bse_xbrl, fake, tapetide"):
        provider_class("nosuch")


def test_configured_returns_enabled_specs_in_config_order_with_their_info() -> None:
    got = configured(FundamentalsSettings())
    assert [(s.name, i.name) for s, i in got] == [
        ("tapetide", "tapetide"),
        ("bse_xbrl", "bse_xbrl"),
    ]
    assert all(isinstance(i, ProviderInfo) for _, i in got)  # upstox is disabled: never looked up


def test_a_disabled_provider_is_skipped_and_fallbacks_run_last() -> None:
    specs = [
        ProviderSpec(name="bse_xbrl", role="fallback"),
        ProviderSpec(name="tapetide", plan=PlanSettings(daily=10)),
    ]
    assert [s.name for s, _ in configured(FundamentalsSettings(providers=specs))] == [
        "tapetide", "bse_xbrl",
    ]  # fmt: skip
    specs[1] = specs[1].model_copy(update={"enabled": False})
    assert [s.name for s, _ in configured(FundamentalsSettings(providers=specs))] == ["bse_xbrl"]


def test_an_unregistered_enabled_name_fails_when_resolved_not_at_import() -> None:
    cfg = FundamentalsSettings(providers=[ProviderSpec(name="nosuch")])
    with pytest.raises(ConfigError, match="unknown fundamentals provider 'nosuch'"):
        configured(cfg)


def test_provider_sources_list_enabled_then_disabled_and_ignore_unregistered_disabled() -> None:
    assert provider_sources(FundamentalsSettings()) == (
        "tapetide", "tapetide_est", "bse_xbrl", "upstox",  # upstox: registered but disabled
    )  # fmt: skip
    off = [
        ProviderSpec(name="tapetide", enabled=False),
        ProviderSpec(name="bse_xbrl"),
        ProviderSpec(name="nosuch", enabled=False),
    ]
    assert provider_sources(FundamentalsSettings(providers=off)) == (
        "bse_xbrl", "tapetide", "tapetide_est",
    )  # fmt: skip


def test_provider_infos_are_the_enabled_providers_then_the_disabled_registered_ones() -> None:
    assert [i.name for i in provider_infos(FundamentalsSettings())] == [
        "tapetide", "bse_xbrl", "upstox",
    ]  # fmt: skip
    off = [ProviderSpec(name="upstox"), ProviderSpec(name="tapetide", enabled=False),
           ProviderSpec(name="nosuch", enabled=False)]  # fmt: skip
    got = provider_infos(FundamentalsSettings(providers=off))
    assert [i.name for i in got] == ["upstox", "tapetide"]  # the unregistered name adds nothing
    assert got[0] is provider_class("upstox").info


def test_provider_sources_include_a_registered_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "fake", FakeProvider)
    cfg = FundamentalsSettings(providers=[ProviderSpec(name="fake"), ProviderSpec(name="bse_xbrl")])
    assert provider_sources(cfg) == ("fake", "bse_xbrl")


def test_revenue_basis_is_declared_for_india_only() -> None:
    cfg = FundamentalsSettings()
    assert revenue_basis_of(cfg, "IN") == provider_class("tapetide").info.revenue_basis
    assert "net of excise" in (revenue_basis_of(cfg, "IN") or "")
    assert revenue_basis_of(cfg, "US") is None
    assert revenue_basis_of(FundamentalsSettings(providers=[]), "IN") is None
    only_bse = FundamentalsSettings(providers=[ProviderSpec(name="bse_xbrl")])
    assert revenue_basis_of(only_bse, "IN") == provider_class("bse_xbrl").info.revenue_basis


# --- the ingest runs providers by role, and a registered fake needs no production edit ---


def settings_with(world: World, *specs: ProviderSpec) -> Settings:
    return Settings(
        data_dir=world.settings.data_dir, market=MarketSettings(),
        fundamentals=FundamentalsSettings(providers=list(specs)),
    )  # fmt: skip


def ingest(world: World, settings: Settings, **kw: Any) -> Any:
    return ingest_fundamentals(world.duck, world.sql, settings, world.sec, today=TODAY, **kw)


def sources(world: World) -> list[str]:
    rows = world.duck.execute("select distinct source from fundamental order by 1").fetchall()
    return [r[0] for r in rows]


def test_a_fake_provider_registered_by_a_test_is_ingested_end_to_end(world: World) -> None:
    world.mp.setitem(REGISTRY, "fake", FakeProvider)
    world.mp.setenv(FAKE_CRED, fake_cred())
    vendor = FakeVendor()
    settings = settings_with(world, ProviderSpec(name="fake", credential=f"ref:{FAKE_CRED}"))
    rep = ingest(world, settings, adapters={"fake": lambda: _fake_adapter(vendor)})
    assert vendor.calls == 1 and rep.failed == [] and rep.rows == 7 and rep.shareholding == 2
    assert sources(world) == ["fake"]
    assert (
        rep.tapetide_calls is None and rep.quota is None
    )  # the report's quota fields are Tapetide's


def _fake_adapter(vendor: FakeVendor) -> Any:
    return FakeAdapter(httpx.Client(transport=httpx.MockTransport(vendor)))


def test_every_primary_runs_and_a_fallback_waits_for_them(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.mp.setitem(REGISTRY, "fake", FakeProvider)
    world.mp.setenv(FAKE_CRED, fake_cred())
    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token())
    srv, vendor, bse = tx.Tapetide(), FakeVendor(), Bse()
    settings = settings_with(
        world, ProviderSpec(name="bse_xbrl", role="fallback"), ProviderSpec(name="fake"),
        ProviderSpec(name="tapetide", plan=PlanSettings(daily=400, monthly=5000)),
    )  # fmt: skip
    rep = ingest(world, settings, tapetide=lambda: tx.adapter(srv), india=india(bse),
                 adapters={"fake": lambda: _fake_adapter(vendor)})  # fmt: skip
    assert srv.calls == 6 and vendor.calls == 1 and bse.calls == [] and rep.failed == []
    assert sources(world) == ["fake", "tapetide", "tapetide_est"]


def test_a_fallback_runs_only_when_no_primary_produced_statements(world: World) -> None:
    world.mp.setitem(REGISTRY, "fake", FakeProvider)  # no credential set: the fake is unavailable
    bse = Bse()
    settings = settings_with(
        world, ProviderSpec(name="fake"), ProviderSpec(name="bse_xbrl", role="fallback")
    )
    rep = ingest(
        world, settings, india=india(bse), adapters={"fake": lambda: _fake_adapter(FakeVendor())}
    )
    assert any(FAKE_CRED in f for f in rep.failed) and sources(world) == ["bse_xbrl"]
    assert rep.rows > 0 and rep.shareholding == 3


def test_no_enabled_provider_is_one_failed_line(world: World) -> None:
    rep = ingest(world, settings_with(world, ProviderSpec(name="tapetide", enabled=False)))
    assert len(rep.failed) == 1 and "no India fundamentals provider is enabled" in rep.failed[0]
    assert rep.rows == 0 and rep.tapetide_calls is None


def test_a_disabled_tapetide_is_never_built_and_the_fallback_serves(world: World) -> None:
    def boom() -> Any:
        raise AssertionError("a disabled provider must not be built")

    specs = [
        ProviderSpec(name="tapetide", enabled=False),
        ProviderSpec(name="bse_xbrl", role="fallback"),
    ]
    rep = ingest(world, settings_with(world, *specs), tapetide=boom, india=india(Bse()))
    assert sources(world) == ["bse_xbrl"] and rep.tapetide_calls is None and rep.failed == []


def test_upstox_and_tapetide_both_run_and_store_under_their_own_sources(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_fallback() -> Any:
        raise AssertionError("the fallback must not run when a primary produced statements")

    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token())
    monkeypatch.setenv(UPSTOX_CRED, upstox_cred())
    srv, vendor = tx.Tapetide(), UpstoxVendor()
    settings = settings_with(
        world,
        ProviderSpec(name="tapetide", plan=PlanSettings(daily=400, monthly=5000)),
        ProviderSpec(name="upstox"),
        ProviderSpec(name="bse_xbrl", role="fallback"),
    )
    rep = ingest(world, settings, tapetide=lambda: tx.adapter(srv), india=no_fallback,
                 adapters={"upstox": lambda: upstox_adapter(vendor)})  # fmt: skip
    assert srv.calls == 6 and vendor.calls == 5 and rep.failed == []  # primary + primary
    assert {"tapetide", "upstox"} <= set(sources(world))
    assert rep.tapetide_calls == 6  # the report's quota fields stay Tapetide's
    assert any("Upstox gives none" in n for n in rep.notes)


def test_ingest_never_requests_the_upstox_corporate_actions_endpoint(world: World) -> None:
    world.mp.setenv(UPSTOX_CRED, upstox_cred())
    vendor = UpstoxVendor()
    settings = settings_with(world, ProviderSpec(name="upstox"))
    rep = ingest(world, settings, adapters={"upstox": lambda: upstox_adapter(vendor)})
    assert rep.failed == [] and rep.rows > 0 and sources(world) == ["upstox"]
    assert rep.shareholding == 3 and rep.estimated_rows > 0 and rep.tapetide_calls is None
    assert vendor.calls == 5 and "corporate-actions" not in vendor.endpoints


def test_the_default_config_never_builds_upstox(world: World) -> None:
    def boom() -> Any:
        raise AssertionError("a disabled provider must not be built")

    world.mp.setenv("TAPETIDE_TOKEN", tx.token())
    srv = tx.Tapetide()
    rep = ingest(world, world.settings, tapetide=lambda: tx.adapter(srv), adapters={"upstox": boom})
    assert srv.calls == 6 and rep.failed == [] and "upstox" not in sources(world)


def test_enabling_upstox_without_a_token_or_an_isin_is_a_failed_line_and_tapetide_still_runs(
    world: World,
) -> None:
    world.mp.setenv("TAPETIDE_TOKEN", tx.token())  # no Upstox token: the keychain is the fake one
    srv, vendor = tx.Tapetide(), UpstoxVendor()
    settings = settings_with(
        world,
        ProviderSpec(name="tapetide", plan=PlanSettings(daily=400, monthly=5000)),
        ProviderSpec(name="upstox"),
    )
    rep = ingest(world, settings, tapetide=lambda: tx.adapter(srv),
                 adapters={"upstox": lambda: upstox_adapter(vendor)})  # fmt: skip
    assert vendor.calls == 0 and srv.calls == 6
    assert [f for f in rep.failed if UPSTOX_CRED in f]
    assert sources(world) == ["tapetide", "tapetide_est"]
    # and a security without an ISIN: Upstox is not asked, whatever else is configured
    world.mp.setenv(UPSTOX_CRED, upstox_cred())
    world.sql.execute("UPDATE security SET isin = NULL WHERE id = ?", (world.sec.id,))
    bare = SecurityMaster(world.sql).by_symbol("RELIANCE")[0]
    only = settings_with(world, ProviderSpec(name="upstox"))
    rep = ingest_fundamentals(world.duck, world.sql, only, bare, today=TODAY,
                              adapters={"upstox": lambda: upstox_adapter(vendor)})  # fmt: skip
    assert vendor.calls == 0 and len(rep.failed) == 1 and "no valid ISIN" in rep.failed[0]


def renamed(world: World) -> Settings:
    plan = PlanSettings(daily=400, monthly=5000)
    return settings_with(
        world, ProviderSpec(name="tapetide", plan=plan, credential="ref:OTHER_CRED")
    )


def test_the_secret_name_comes_from_the_spec(world: World) -> None:
    world.mp.setenv("OTHER_CRED", tx.token())  # the default TAPETIDE_TOKEN is not set at all
    srv = tx.Tapetide()
    rep = ingest(world, renamed(world), tapetide=lambda: tx.adapter(srv))
    assert srv.calls == 6 and rep.failed == [] and rep.rows > 0


def test_a_missing_secret_is_named_as_the_spec_names_it(world: World) -> None:
    world.mp.setenv("TAPETIDE_TOKEN", tx.token())  # set, but this spec reads another secret
    srv = tx.Tapetide()
    rep = ingest(world, renamed(world), tapetide=lambda: tx.adapter(srv))
    assert srv.calls == 0 and any("OTHER_CRED not set" in f for f in rep.failed)
    assert "nivesh secrets set OTHER_CRED" in " ".join(rep.failed)
    assert not any("TAPETIDE_TOKEN" in f for f in rep.failed)


def test_the_pacing_comes_from_the_plan(world: World) -> None:
    world.mp.setenv("TAPETIDE_TOKEN", tx.token())
    made: list[RateLimiter] = []

    def limiter(per_sec: float) -> RateLimiter:  # a fake clock: the test never sleeps
        made.append(RateLimiter(per_sec, lambda: 0.0, lambda s: None))
        return made[-1]

    world.mp.setattr(fundamentals_tapetide, "RateLimiter", limiter)
    plan = PlanSettings(daily=400, monthly=5000, min_interval_s=2.5)
    settings = settings_with(world, ProviderSpec(name="tapetide", plan=plan))
    srv = tx.Tapetide()
    rep = ingest(world, settings, tapetide=lambda: TapetideFundamentals(tx.client(srv)))
    assert rep.failed == [] and [m.interval for m in made] == [pytest.approx(2.5)]


def test_an_unregistered_enabled_name_stops_the_ingest_with_a_clear_error(world: World) -> None:
    with pytest.raises(ConfigError, match="unknown fundamentals provider 'nosuch'"):
        ingest(world, settings_with(world, ProviderSpec(name="nosuch")))


def test_a_metered_provider_without_a_plan_is_one_failed_line_and_is_never_built(
    world: World,
) -> None:
    class Metered(FakeProvider):
        info = dataclasses.replace(FakeProvider.info, metered=True)

    def boom() -> Any:
        raise AssertionError("a metered provider without a plan must not be built")

    world.mp.setitem(REGISTRY, "fake", Metered)
    rep = ingest(world, settings_with(world, ProviderSpec(name="fake")), adapters={"fake": boom})
    assert len(rep.failed) == 1 and "Fake vendor quota budget not configured" in rep.failed[0]
    assert rep.rows == 0 and rep.tapetide_calls is None


def test_a_metered_provider_with_a_plan_gets_its_own_budget_not_the_tapetide_report(
    world: World,
) -> None:
    seen: list[Any] = []

    class Metered(FakeProvider):
        info = dataclasses.replace(FakeProvider.info, metered=True)

        def collect(self, ctx: CollectContext) -> ProviderResult:
            seen.append(ctx.budget)
            return super().collect(ctx)

    world.mp.setitem(REGISTRY, "fake", Metered)
    world.mp.setenv(FAKE_CRED, fake_cred())
    spec = ProviderSpec(name="fake", plan=PlanSettings(daily=10))
    rep = ingest(
        world,
        settings_with(world, spec),
        adapters={"fake": lambda: _fake_adapter(FakeVendor())},
    )
    assert [(b.vendor, b.priority, b.plan.daily) for b in seen] == [("fake", "universe", 10)]
    assert rep.rows == 7 and rep.failed == []
    assert rep.tapetide_calls is None and rep.tapetide_attempts is None and rep.quota is None


def test_a_fallback_that_finds_nothing_still_reports_what_is_already_stored(world: World) -> None:
    held = ShareholdingRow(period_end=date(2025, 12, 31), filed_at=date(2026, 1, 21),
                           promoter_pct=Decimal("50"), promoter_pledged_pct=None,
                           public_pct=Decimal("50"))  # fmt: skip
    write_fundamentals(world.duck, world.sec.id, [], "tapetide_est", shareholding=[held])
    empty = Bse(filings=[])
    empty.share = []
    rep = ingest(
        world, world.settings, tapetide=lambda: tx.adapter(tx.Tapetide()), india=india(empty)
    )
    assert rep.rows == 0 and rep.shareholding == 1  # no token, BSE has nothing: the count stays


def test_tapetide_stores_no_shareholding_when_it_has_no_statements(world: World) -> None:
    srv = tx.Tapetide()
    for section in tx.SECTIONS:  # every period loses its filing date, so none can be stored
        srv.edit("RELIANCE", section, lambda sec: sec.update(availability=[]))
    h = next(h for h in HARNESSES if h.name == "tapetide")
    res = collect(world, h, srv).res
    assert res.groups == [] and res.shareholding == [] and res.calls == 6
    assert any("no filing date from Tapetide" in x for x in res.skipped)


def test_tapetide_notes_count_the_estimated_and_the_clamped_filing_dates(world: World) -> None:
    h = next(h for h in HARNESSES if h.name == "tapetide")
    res = collect(world, h, h.serve(), today=date(2024, 5, 1)).res
    assert res.estimated > 0 and res.clamped > 0
    total = sum(len(rows) for _, rows in res.groups)
    assert res.notes[:2] == [
        f"{res.estimated} of {total} rows carry an estimated filing date "
        "(history before about Sep 2024 is approximate)",
        f"{res.clamped} filing dates after today were set to today",
    ]


def test_tapetide_without_estimated_dates_has_no_estimate_note(world: World) -> None:
    h = next(h for h in HARNESSES if h.name == "tapetide")
    srv = tx.Tapetide()
    for section in tx.SECTIONS:  # every filing date is a declared one
        srv.edit(
            "RELIANCE",
            section,
            lambda sec: [a.update(basis="reported") for a in sec["availability"]],
        )
    res = collect(world, h, srv).res
    assert res.statements and res.estimated == 0
    assert not any("estimated filing date" in n for n in res.notes)
    assert [src for src, rows in res.groups if rows] == ["tapetide"]


@pytest.mark.parametrize("mapper", ["map_statements", "map_shareholding"])
def test_a_mapper_that_chokes_is_a_failed_line_not_a_crash(world: World, mapper: str) -> None:
    def choke(*args: Any) -> Any:
        raise ValueError("choked")

    world.mp.setattr(fundamentals_tapetide, mapper, choke)
    h = next(h for h in HARNESSES if h.name == "tapetide")
    out = collect(world, h, h.serve())
    what = "statements: unusable Tapetide data" if mapper == "map_statements" else "shareholding"
    assert out.error is None and any(f"{what}" in f and "choked" in f for f in out.failed)
    assert not out.res.shareholding  # nothing was stored for it


def test_the_credential_defaults_to_the_provider_and_the_spec_overrides_it() -> None:
    plan = PlanSettings(daily=10)
    assert provider_class("tapetide")(ProviderSpec(name="tapetide", plan=plan)).credential_name == (
        "TAPETIDE_TOKEN"
    )
    other = ProviderSpec(name="tapetide", plan=plan, credential="ref:OTHER_CRED")
    assert provider_class("tapetide")(other).credential_name == "OTHER_CRED"
    assert provider_class("bse_xbrl")(ProviderSpec(name="bse_xbrl")).credential_name is None


def test_bse_ignores_filings_dated_after_the_as_of_date_and_says_so(world: World) -> None:
    h = next(h for h in HARNESSES if h.name == "bse_xbrl")
    res = collect(world, h, h.serve(), today=h.early).res
    assert not res.statements  # the results filing came a month after
    assert any("BSE filings dated after 2025-12-31 ignored" in x for x in res.skipped)
    assert len(res.shareholding) == 2  # the patterns filed before the date are kept


# --- the contract: every harness, six checks ---


@dataclass
class Outcome:
    provider: FundamentalsProvider
    result: ProviderResult | None
    error: Exception | None
    failed: list[str]

    @property
    def res(self) -> ProviderResult:
        assert self.result is not None, f"collect raised {self.error!r}; failed={self.failed}"
        return self.result

    def texts(self) -> list[str]:
        out = [*self.failed, repr(self.provider), repr(self.error), str(self.error)]
        if self.result is not None:
            out += [*self.result.notes, *self.result.skipped]
        return out


def collect(world: World, h: Harness, double: Any, *, today: date = TODAY) -> Outcome:
    if h.cred is not None:
        world.mp.setenv(*h.cred)
    world.duck.execute("DELETE FROM cache_entry")  # a cache hit would hide the double
    provider = h.provider(double)
    run = Run(world.duck, world.settings)
    budget = tx.memory_budget() if type(provider).info.metered else None
    ctx = CollectContext(world.sec, world.master, today, run.get, run.failed, budget)
    try:
        return Outcome(provider, provider.collect(ctx), None, run.failed)
    except Exception as e:  # noqa: BLE001 - the suite reads what the provider let escape
        return Outcome(provider, None, e, run.failed)


def rows_of(res: ProviderResult) -> dict[Key, Decimal]:
    return {(r.item, r.period_type, r.period_end): r.value for _, rs in res.groups for r in rs}


by_harness = pytest.mark.parametrize("h", HARNESSES, ids=lambda h: h.name)


@by_harness
def test_rows_are_point_in_time_and_stored_under_a_declared_source(
    world: World, h: Harness
) -> None:
    out = collect(world, h, h.serve())
    res, info = out.res, type(out.provider).info
    assert res.statements and res.shareholding
    stored = {src for src, _ in res.groups} | {res.shareholding_source or res.groups[0][0]}
    assert stored <= set(info.sources)
    for _, rows in res.groups:
        assert all(r.period_end <= r.filed_at <= TODAY for r in rows)
    assert all(s.period_end <= s.filed_at <= TODAY for s in res.shareholding)


@by_harness
def test_nothing_filed_after_the_as_of_date_appears_and_results_carry_utc_stamps(
    world: World, h: Harness
) -> None:
    res = collect(world, h, h.serve(), today=h.early).res
    rows = [r for _, rs in res.groups for r in rs]
    assert all(r.filed_at <= h.early for r in rows)
    assert all(s.filed_at <= h.early for s in res.shareholding)
    assert res.shareholding  # the early data set still has something to show
    if h.cred:
        world.mp.setenv(*h.cred)
    fetched = h.adapter(h.serve()).fetch(**h.fetch_params)
    assert fetched.as_of.utcoffset() is not None and fetched.fetched_at.utcoffset() is not None


@by_harness
def test_units_are_scaled_to_absolute_inr_and_bounded(world: World, h: Harness) -> None:
    res = collect(world, h, h.serve()).res
    got = rows_of(res)
    for key, want in h.known.items():
        assert got[key] == want, key
    for variant in h.variants:
        every = [r for _, rs in collect(world, h, h.serve(variant=variant)).res.groups for r in rs]
        assert all(0 <= r.value <= 100 for r in every if r.currency == "pct")
        assert all(abs(r.value) < 100000 for r in every if r.item == "eps")  # per share, unscaled
        counts = [r.value for r in every if r.currency == "shares"]
        assert all(v > 0 and v == v.to_integral_value() for v in counts)
    for s in res.shareholding:
        pcts = (s.promoter_pct, s.public_pct, s.promoter_pledged_pct)
        assert all(p is None or 0 <= p <= 100 for p in pcts)


@by_harness
def test_an_unavailable_item_is_absent_with_a_reason_never_zero(world: World, h: Harness) -> None:
    base = collect(world, h, h.serve()).res
    out = collect(world, h, h.serve(drop=True))
    after = out.result if out.result is not None else ProviderResult()
    assert any(k[0] == h.dropped for k in rows_of(base))  # the item exists in the baseline
    assert not any(k[0] == h.dropped for k in rows_of(after))
    zeros = {k for k, v in rows_of(after).items() if v == 0}
    assert zeros <= {k for k, v in rows_of(base).items() if v == 0}  # absence never became 0
    assert out.failed or after.skipped or after.notes  # and something says why


@by_harness
def test_no_secret_reaches_errors_notes_reprs_or_the_cache(world: World, h: Harness) -> None:
    info = type(h.provider(None)).info
    if h.cred is None:
        assert info.credential is None  # nothing to leak
    else:
        assert info.credential == h.cred[0] and h.spec.credential in (None, f"ref:{h.cred[0]}")
    for kind in (None, *HOSTILE):
        double = h.serve() if kind is None else (lambda req, k=kind: h.reply(k))
        out = collect(world, h, double)
        texts = [*out.texts(), repr(h.adapter(double))]
        texts.append(
            json.dumps(world.duck.execute("select * from cache_entry").fetchall(), default=str)
        )
        if h.cred is not None:
            assert not any(h.cred[1] in t for t in texts), kind
        if kind is not None:
            assert out.result is None or not out.result.statements, kind  # hostile: no rows
            assert out.error is not None or out.failed, kind  # and a reason, never silence


@by_harness
def test_a_provider_built_without_a_transport_cannot_reach_the_network(
    world: World, h: Harness
) -> None:
    out = collect(world, h, None)
    if out.error is not None:  # the test guards stopped it: it tried to leave the process
        assert isinstance(out.error, FixtureMissing | RuntimeError | ConfigError), out.error
    else:
        assert not out.res.statements and out.failed  # or it reported why it produced nothing


# --- the declared capabilities, and the guard that keeps a provider from skipping the suite ---


def produced(res: ProviderResult) -> set[Capability]:
    items = {r.item for _, rows in res.groups for r in rows}
    return {
        *([Capability.statements] if res.statements else []),
        *([Capability.shareholding] if res.shareholding else []),
        *([Capability.pledge] if any(s.promoter_pledged_pct is not None for s in res.shareholding)
           else []),
        *([Capability.bank_metrics] if items & BANK_ITEMS else []),
    }  # fmt: skip


@by_harness
def test_declared_capabilities_match_what_the_harness_produces(world: World, h: Harness) -> None:
    got: set[Capability] = set()
    for variant in h.variants:
        got |= produced(collect(world, h, h.serve(variant=variant)).res)
    if h.probe is not None:  # actions, estimates and news have no slot in `ProviderResult`
        got |= h.probe(h.serve())
    declared = type(h.provider(None)).info.capabilities
    assert declared == got  # so declare one of them only with a probe in the harness


@pytest.mark.parametrize("name", sorted(BUILTIN))
def test_every_registered_provider_has_a_harness(name: str) -> None:
    assert name in {h.name for h in HARNESSES}, f"add a harness for {name} to tests/provider_fx.py"
    assert provider_class(name).info.name == name
    assert [h.spec.name for h in HARNESSES if h.name == name] == [name]
