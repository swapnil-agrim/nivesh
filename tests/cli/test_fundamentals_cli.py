"""`nivesh fundamentals compare`: stored rows of several providers side by side, read-only, and the
source-aware reads behind `nivesh fa` (the first configured provider wins each figure)."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
import nivesh_cli.market as cm
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.market_store import write_fundamentals
from nivesh_core.pii_scan import scan_text
from nivesh_core.security_master import build_master
from nivesh_engine.statements import StatementRow
from tests.market_fx import mrow

D = Decimal
runner = CliRunner()
Env = tuple[list[str], Path]
ASOF = date(2025, 12, 31)
FY = date(2025, 3, 31)
TAPE, UPS = "2025-05-09", "2025-06-02"  # the two providers file on different days


def stmt(item: str, value: str, filed: str, ptype: str = "A", end: date = FY) -> StatementRow:
    return StatementRow(
        period_end=end, period_type=ptype,  # type: ignore[arg-type]
        item=item, value=D(value), currency="INR", filed_at=date.fromisoformat(filed),
    )  # fmt: skip


def held(
    promoter: str, filed: str, *, public: str | None = None, pledged: str | None = None,
    end: date = FY,
) -> ShareholdingRow:  # fmt: skip
    return ShareholdingRow(
        period_end=end, promoter_pct=D(promoter), public_pct=D(public) if public else None,
        promoter_pledged_pct=D(pledged) if pledged else None, filed_at=date.fromisoformat(filed),
    )  # fmt: skip


def seed(data: Path) -> dict[str, int]:
    """RELIANCE with a tapetide and an upstox set (net income 18.5 percent apart), TCS with
    tapetide only, TATAMOTORS with nothing."""
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    build_master(sql, [
        mrow("RELIANCE", name="Reliance Industries Limited", isin="INE002A01018", sector="Energy"),
        mrow("TCS", name="Tata Consultancy Services Limited", isin="INE467B01029"),
        mrow("TATAMOTORS", name="Tata Motors Limited", isin="INE155A01022"),
    ], [])  # fmt: skip
    ids = {r[0]: r[1] for r in sql.execute("select symbol, id from security")}
    sql.close()
    duck = open_duck(data / "nivesh.duckdb")
    try:
        tape = [stmt("revenue", "1055780", TAPE), stmt("net_income", "95754", TAPE),
                stmt("total_equity", "500000", TAPE), stmt("cfo", "150000", TAPE),
                stmt("eps", "70", TAPE)]  # fmt: skip
        ups = [stmt("revenue", "1055780", UPS), stmt("net_income", "78039.51", UPS),
               stmt("total_equity", "502000", UPS),
               stmt("total_assets", "9000000", UPS)]  # fmt: skip
        q3 = date(2024, 12, 31)
        tape_held = [held("50.4", TAPE, public="49.9", pledged="0"), held("50.0", TAPE, end=q3)]
        ups_held = [held("50.0", UPS), held("49.0", UPS, end=q3)]
        write_fundamentals(duck, ids["RELIANCE"], tape, "tapetide", shareholding=tape_held)
        write_fundamentals(duck, ids["RELIANCE"], ups, "upstox", shareholding=ups_held)
        est = [stmt("capex", "1234", "2025-05-30")]  # tapetide's estimated filing date: its own
        write_fundamentals(duck, ids["RELIANCE"], est, "tapetide_est")
        write_fundamentals(duck, ids["TCS"], [stmt("revenue", "10", TAPE)], "tapetide")
    finally:
        duck.close()
    return ids


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)

    def never(*_: Any, **__: Any) -> None:
        raise AssertionError("fundamentals compare must not build a vendor adapter")

    monkeypatch.setattr(cm, "_tapetide", never)
    monkeypatch.setattr(cm, "_upstox", never)
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def as_json(env: Env, *args: str) -> dict[str, Any]:
    r = call(env, "fundamentals", "compare", *args, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)["result"]  # type: ignore[no-any-return]


def sid_of(env: Env, symbol: str) -> int:
    sql = open_sqlite(env[1] / "nivesh.sqlite")
    try:
        return int(sql.execute("select id from security where symbol = ?", (symbol,)).fetchone()[0])
    finally:
        sql.close()


def ledger_rows(data: Path) -> int:
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        return int(sql.execute("select count(*) from vendor_call").fetchone()[0])
    finally:
        sql.close()


def order_providers(env: Env, *names: str) -> None:
    """Rewrite the `fundamentals:` block of the copied config with these providers, in order."""
    path = Path(env[0][1]) / "nivesh.yaml"
    block = "fundamentals:\n  providers:\n" + "".join(f"    - {{name: {n}}}\n" for n in names)
    text = re.sub(r"(?m)^fundamentals:\n(?:  .*\n)+", block, path.read_text())
    path.write_text(text)


# ---- the report ---------------------------------------------------------------------------------
def test_compare_prints_providers_labels_counts_and_the_largest_difference_first(env: Env) -> None:
    r = call(env, "fundamentals", "compare", "RELIANCE")
    assert r.exit_code == 0, r.output
    out = r.output
    assert f"fundamentals compare RELIANCE as of {ASOF.isoformat()}" in out
    assert out.index("tapetide") < out.index("upstox")  # priority order
    assert "  tapetide: Tapetide (" in out and "  upstox (disabled): Upstox (" in out
    for label in ("revenue:", "profit:", "filing dates:", "statements, shareholding, pledge"):
        assert label in out, label
    assert "tolerance: statements 1%, shareholding 0.5 pp" in out
    assert (
        "statements: 3 compared against the first provider that holds each, 2 agree, 1 differ"
        in out
    )
    assert "A 2025-03-31 net_income: tapetide 95,754 vs upstox 78,039.51 (-18.5%)" in out
    assert "only in tapetide: A 2025-03-31 capex, A 2025-03-31 cfo, A 2025-03-31 eps" in out
    assert "only in upstox: A 2025-03-31 total_assets" in out
    assert "shareholding: 2 compared" in out and "2024-12-31 promoter_pct" in out
    assert scan_text(out) == []


def test_compare_json_carries_exact_decimals_and_every_label(env: Env) -> None:
    raw = call(env, "fundamentals", "compare", "RELIANCE", "--json").stdout
    assert scan_text(raw) == []
    got = as_json(env, "RELIANCE")
    assert [p["name"] for p in got["providers"]] == ["tapetide", "upstox"]
    tape = got["providers"][0]
    assert tape["sources"] == ["tapetide", "tapetide_est"] and tape["enabled"] is True
    assert {"statements", "shareholding"} <= set(tape["capabilities"])
    assert tape["revenue_basis"] and tape["profit_basis"] and tape["filing_dates"] == "mixed"
    assert got["providers"][1]["enabled"] is False  # stored rows of a disabled provider are read
    assert got["tolerance"] == {"statements_pct": "1", "shareholding_pp": "0.5"}
    assert got["single_provider"] is False
    (d,) = got["statements"]["disagreements"]
    assert (d["figure"], d["base_provider"], d["provider"]) == (
        "A 2025-03-31 net_income", "tapetide", "upstox",
    )  # fmt: skip
    assert (D(d["base"]), D(d["value"]), D(d["delta"]), d["pct"]) == (
        D(95754), D("78039.51"), D("-17714.49"), "-18.50",
    )  # fmt: skip
    assert (got["statements"]["compared"], got["statements"]["agree"]) == (3, 2)
    assert got["statements"]["only_in"] == {
        "tapetide": ["A 2025-03-31 capex", "A 2025-03-31 cfo", "A 2025-03-31 eps"],
        "upstox": ["A 2025-03-31 total_assets"],
    }
    (s,) = got["shareholding"]["disagreements"]  # 50.4 against 50.0 agrees: 0.4 points <= 0.5
    assert (s["figure"], D(s["delta"])) == ("2024-12-31 promoter_pct", D("-1"))
    assert got["shareholding"]["compared"] == 2


def test_one_stored_provider_says_there_is_nothing_to_compare(env: Env) -> None:
    r = call(env, "fundamentals", "compare", "TCS")
    assert r.exit_code == 0, r.output
    assert "single provider with stored rows (tapetide): nothing to compare" in r.output
    assert "differ" not in r.output and "only in" not in r.output
    got = as_json(env, "TCS")
    assert got["single_provider"] is True and [p["name"] for p in got["providers"]] == ["tapetide"]
    assert got["statements"]["disagreements"] == [] and got["statements"]["only_in"] == {}


def test_no_stored_rows_at_all_is_said_plainly(env: Env) -> None:
    r = call(env, "fundamentals", "compare", "TATAMOTORS")
    assert r.exit_code == 0 and "no provider has stored rows for TATAMOTORS" in r.output
    got = as_json(env, "TATAMOTORS")
    assert got["providers"] == [] and got["single_provider"] is True


def test_as_of_hides_what_a_provider_had_not_yet_filed(env: Env) -> None:
    r = call(env, "fundamentals", "compare", "RELIANCE", "--as-of", "2025-05-20")
    assert "single provider with stored rows (tapetide): nothing to compare" in r.output
    assert "upstox" not in r.output.split("tolerance")[0]
    got = as_json(env, "RELIANCE", "--as-of", "2025-06-02")
    assert [p["name"] for p in got["providers"]] == ["tapetide", "upstox"]
    bad = call(env, "fundamentals", "compare", "RELIANCE", "--as-of", "soon")
    assert bad.exit_code == 1 and "error:" in bad.output


def test_unknown_security_is_the_usual_error(env: Env) -> None:
    r = call(env, "fundamentals", "compare", "NOSUCHCO")
    assert r.exit_code == 1 and "error:" in r.output and "NOSUCHCO" in r.output


def test_a_long_list_of_differences_is_capped_and_json_has_them_all(env: Env) -> None:
    duck = open_duck(env[1] / "nivesh.duckdb")
    try:
        many = [stmt(f"line_{i:02d}", "100", TAPE) for i in range(25)]
        tcs = sid_of(env, "TCS")
        write_fundamentals(duck, tcs, many, "tapetide")
        write_fundamentals(duck, tcs, [stmt(r.item, "50", UPS) for r in many], "upstox")
    finally:
        duck.close()
    out = call(env, "fundamentals", "compare", "TCS").output
    assert out.count(" (-50%)") == 20 and "and 5 more, use --json" in out
    assert len(as_json(env, "TCS")["statements"]["disagreements"]) == 25


def test_compare_is_read_only_no_vendor_adapter_and_no_ledger_row(env: Env) -> None:
    before = ledger_rows(env[1])
    assert call(env, "fundamentals", "compare", "RELIANCE").exit_code == 0
    assert call(env, "fundamentals", "compare", "RELIANCE", "--json").exit_code == 0
    assert ledger_rows(env[1]) == before == 0  # `never` (the adapter seams) was not called either


def test_providers_that_hold_different_kinds_of_rows_have_nothing_to_compare_in_each(
    env: Env,
) -> None:
    duck = open_duck(env[1] / "nivesh.duckdb")
    try:  # TCS has tapetide statements; add upstox shareholding only, and a source nobody claims
        tcs = sid_of(env, "TCS")
        write_fundamentals(duck, tcs, [], "upstox", shareholding=[held("50", UPS)])
        write_fundamentals(duck, tcs, [stmt("revenue", "9", TAPE)], "legacy_feed")
    finally:
        duck.close()
    out = call(env, "fundamentals", "compare", "TCS").output
    assert "statements: only tapetide holds any; nothing to compare" in out
    assert "shareholding: only upstox holds any; nothing to compare" in out
    assert "other stored sources (not compared): legacy_feed" in out
    got = as_json(env, "TCS")
    assert got["other_sources"] == ["legacy_feed"] and got["single_provider"] is False


def test_the_group_does_not_clash_with_market_fundamentals(env: Env) -> None:
    top = call(env, "fundamentals", "--help")
    assert top.exit_code == 0 and "compare" in top.output
    market = call(env, "market", "fundamentals", "--help")
    assert market.exit_code == 0 and "compare" not in market.output


# ---- source-aware reads end to end -----------------------------------------------------------
def net_margin(env: Env) -> Decimal:
    r = call(env, "fa", "RELIANCE", "--json")
    assert r.exit_code == 0, r.output
    metric = json.loads(r.stdout)["result"]["metrics"]["net_margin_pct"]
    assert metric["available"] is True, metric
    return D(metric["value"])


def test_fa_uses_the_first_configured_provider_and_flipping_the_order_flips_it(env: Env) -> None:
    first = net_margin(env)  # tapetide: 95754 / 1055780
    assert first.quantize(D("0.01")) == D("9.07")
    order_providers(env, "upstox", "tapetide")
    assert net_margin(env).quantize(D("0.01")) == D("7.39")  # upstox: 78039.51 / 1055780
    order_providers(env, "tapetide", "upstox")
    assert net_margin(env) == first


def test_compare_follows_the_configured_order(env: Env) -> None:
    order_providers(env, "upstox", "tapetide")
    got = as_json(env, "RELIANCE")
    assert [p["name"] for p in got["providers"]] == ["upstox", "tapetide"]
    (d,) = got["statements"]["disagreements"]
    assert (d["base_provider"], d["provider"], d["pct"]) == ("upstox", "tapetide", "22.70")


def test_a_misspelled_provider_name_fails_loudly_instead_of_ignoring_the_order(env: Env) -> None:
    order_providers(env, "tapetid", "upstox")  # nobody registered "tapetid"
    for args in (("fa", "RELIANCE"), ("fundamentals", "compare", "RELIANCE")):
        r = call(env, *args)
        assert r.exit_code == 1, args
        assert "unknown fundamentals provider 'tapetid'" in r.output and "registered:" in r.output
