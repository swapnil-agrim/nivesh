"""The read-only `engine` MCP server over real temp stores (nothing fetched, nothing written)."""

import ast
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastmcp.exceptions import ToolError

import nivesh_mcp.common as common
from nivesh_adapters import analysis_service as svc
from nivesh_core.config import Settings
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.redact import is_sensitive_key
from nivesh_core.security_master import SecurityMaster, build_master
from nivesh_mcp import engine
from nivesh_mcp.base import _desc_write_words, is_write_name
from nivesh_mcp.registry import SERVERS
from tests.analysis_fx import make_profile
from tests.cli.test_engine_cli import ASOF, ASOF_TEXT, seed_cli_store
from tests.market_fx import mrow
from tests.mcp.mkt_fx import call
from tests.mf_fx import DG, DP, G, P, seed_doctor
from tests.mf_fx import Env as MfEnv

TOOLS = [
    "ta_compute", "fa_compute", "valuation_range", "red_flags", "risk_metrics", "portfolio_xray",
    "score", "mf_analyse", "mf_overlap", "get_fund_meta",
]  # fmt: skip
NOW = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
ARGS: dict[str, dict[str, Any]] = {
    "ta_compute": {"security": "US1", "as_of": ASOF_TEXT},
    "fa_compute": {"security": "US1", "as_of": ASOF_TEXT},
    "valuation_range": {"security": "US1", "as_of": ASOF_TEXT},
    "red_flags": {"security": "US1", "as_of": ASOF_TEXT},
    "risk_metrics": {"candidate": "US2", "weight_pct": "2", "as_of": ASOF_TEXT},
    "portfolio_xray": {"as_of": ASOF_TEXT},
    "score": {"security": "US1", "as_of": ASOF_TEXT},
    "mf_analyse": {"scheme": "100001"},
    "mf_overlap": {},
    "get_fund_meta": {"scheme": "100001"},
}  # fmt: skip


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One seeded data dir for the whole module (every test only reads it)."""
    data = tmp_path_factory.mktemp("engine") / "data"
    seed_cli_store(data)
    sql, duck = open_sqlite(data / "nivesh.sqlite"), open_duck(data / "nivesh.duckdb")
    try:
        build_master(
            sql,
            [
                mrow(code, "AMFI", isin=code, asset_class="mf", amfi_code=amfi, name=name)
                for code, amfi, name in (
                    (G, "100001", "Example Bluechip Fund Regular Plan Growth"),
                    (P, "100001", "Example Bluechip Fund Regular Plan IDCW"),
                    (DG, "100010", "Example Bluechip Fund Direct Plan Growth"),
                    (DP, "100010", "Example Bluechip Fund Direct Plan IDCW"),
                )
            ],
            [],
        )
        seed_doctor(MfEnv(sql, duck, None, SecurityMaster(sql)))  # type: ignore[arg-type]
    finally:
        sql.close()
        duck.close()
    return data


@pytest.fixture
def eng(seeded: Path, cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private config dir (own profile copy) pointing at the shared seeded data dir."""
    cfg = Path(cli_env[0][1])
    text = (
        (ROOT / "config" / "nivesh.yaml")
        .read_text()
        .replace("data_dir: data", f"data_dir: {seeded}")
    )
    (cfg / "nivesh.yaml").write_text(text)
    monkeypatch.setenv("NIVESH_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(common, "now", lambda: NOW)
    common._ready.clear()
    return seeded


def walk(node: Any) -> Any:
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def test_server_has_exactly_the_ten_tools() -> None:
    assert SERVERS["engine"] is engine.server and list(SERVERS)[-1] == "engine"
    assert engine.server.tool_names == TOOLS


def test_every_tool_is_sync_documented_typed_and_has_no_write_name_or_description_word() -> None:
    for name in TOOLS:
        fn = getattr(engine, name)
        assert not inspect.iscoroutinefunction(fn) and not is_write_name(name)
        doc = fn.__doc__ or ""
        assert doc.strip() and _desc_write_words(doc) == [], name
        sig = inspect.signature(fn)
        assert all(p.annotation is not inspect.Parameter.empty for p in sig.parameters.values())
        assert sig.return_annotation is not inspect.Signature.empty
    tree = ast.parse(Path(engine.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            assert not is_write_name(node.name), node.name
            assert _desc_write_words(ast.get_docstring(node) or "") == [], node.name


async def test_each_tool_returns_the_envelope_with_as_of_and_source_nivesh_engine(
    eng: Path,
) -> None:
    for name in TOOLS:
        out = await call("engine", name, **ARGS[name])
        assert out["source"] == "nivesh-engine" and out["as_of"], name
        assert set(out["data"]) == {"subject", "result"} or name == "get_fund_meta"


async def test_decimals_are_emitted_as_json_numbers_not_digit_strings(eng: Path) -> None:
    out = await call("engine", "ta_compute", **ARGS["ta_compute"])
    ind = out["data"]["result"]["indicators"]
    rsi = ind["values"]["rsi_14"]["value"]
    assert isinstance(rsi, float | int) and not isinstance(rsi, bool)
    risk = await call("engine", "risk_metrics", **ARGS["risk_metrics"])
    assert isinstance(
        risk["data"]["result"]["risk"]["pro_forma"]["candidate_weight_pct"], int | float
    )


def _direct(fn: str, *extra: Any) -> Any:
    data = Path(common.settings().data_dir)
    sql = open_sqlite(data / "nivesh.sqlite")
    duck = open_duck(data / "nivesh.duckdb", read_only=True)
    try:
        return getattr(svc, fn)(duck, sql, common.settings(), *extra)
    finally:
        sql.close()
        duck.close()


async def test_ta_compute_over_a_seeded_store_matches_the_service(eng: Path) -> None:
    out = await call("engine", "ta_compute", **ARGS["ta_compute"])
    want = _direct("ta_report", "US1", ASOF)
    assert out["data"]["subject"] == "US1"
    assert out["data"]["result"] == json.loads(
        json.dumps(svc.plain(want.result, number=common.num))
    )


async def test_fa_compute_as_of_excludes_later_filings(eng: Path) -> None:
    late = await call("engine", "fa_compute", security="US1", as_of=ASOF_TEXT)
    early = await call("engine", "fa_compute", security="US1", as_of="2019-01-01")
    assert late["data"]["result"] != early["data"]["result"]
    assert early["as_of"] == "2019-01-01"


async def test_valuation_range_red_flags_and_score_return_their_fields(eng: Path) -> None:
    val = await call("engine", "valuation_range", **ARGS["valuation_range"])
    assert set(val["data"]["result"]) == {"multiples", "range"}
    flags = await call("engine", "red_flags", **ARGS["red_flags"])
    assert flags["data"]["result"]["flags"]
    sc = await call("engine", "score", **ARGS["score"])
    card = sc["data"]["result"]["card"]
    assert {"band", "cap", "inputs_available", "inputs_total"} <= set(card)
    assert sc["data"]["result"]["universe_size"] >= 4


async def test_risk_metrics_and_portfolio_xray_use_the_profile_limits_from_the_config_dir(
    eng: Path, cli_env: tuple[list[str], Path]
) -> None:
    loose = await call("engine", "risk_metrics", **ARGS["risk_metrics"])
    prof = Path(cli_env[0][1]) / "profile.yaml"
    prof.write_text(prof.read_text().replace("max_position_pct: 10", "max_position_pct: 1"))
    tight = await call("engine", "risk_metrics", **ARGS["risk_metrics"])
    assert (
        loose["data"]["result"]["risk"]["pro_forma"]["positions_over_limit"]
        != (tight["data"]["result"]["risk"]["pro_forma"]["positions_over_limit"])
    )
    x = await call("engine", "portfolio_xray", **ARGS["portfolio_xray"])
    assert "xray" in x["data"]["result"]


async def test_mf_analyse_overlap_and_fund_meta_over_seeded_funds(eng: Path) -> None:
    a = await call("engine", "mf_analyse", scheme="100001")
    r = a["data"]["result"]
    assert r["scheme"] == "100001" and r["analytics"]["points"] > 0
    assert r["doctor"]["verdict"]["action"] == "SWITCH_TO_DIRECT"
    m = await call("engine", "get_fund_meta", scheme="100001")
    assert m["data"]["result"]["meta"]["expense_ratio"] == 1.5
    ov = (await call("engine", "mf_overlap"))["data"]["result"]
    assert ov["pairs"] == [] and ov["look_through"]["total_inr"] > 0  # one held fund: no pair


async def test_unknown_security_is_a_tool_error_not_a_crash(eng: Path) -> None:
    for name in ("ta_compute", "fa_compute", "valuation_range", "red_flags", "score"):
        with pytest.raises(ToolError, match="no security matches"):
            await call("engine", name, security="NOPE-ZZZ")
    with pytest.raises(ToolError, match="unknown AMFI"):
        await call("engine", "get_fund_meta", scheme="424242")


async def test_tool_output_has_no_redaction_trigger_key_names_anywhere(eng: Path) -> None:
    for name in TOOLS:
        if name == "mf_overlap":
            continue
        out = await call("engine", name, **ARGS[name])
        bad = sorted({k for k in walk(out) if is_sensitive_key(str(k))})
        assert bad == [], (name, bad)


async def test_bad_as_of_and_half_a_candidate_pair_are_tool_errors(eng: Path) -> None:
    with pytest.raises(ToolError, match="as_of must be an ISO date"):
        await call("engine", "ta_compute", security="US1", as_of="yesterday")
    with pytest.raises(ToolError, match="go together"):
        await call("engine", "risk_metrics", candidate="US2")
    with pytest.raises(ToolError, match="weight"):
        await call("engine", "risk_metrics", candidate="US2", weight_pct="150")
    with pytest.raises(ToolError, match="horizon"):
        await call("engine", "score", security="US1", horizon="someday")


async def test_market_db_busy_becomes_the_standard_busy_error(eng: Path) -> None:
    writer = duckdb.connect(str(eng / "nivesh.duckdb"))
    try:
        with pytest.raises(ToolError, match="being updated"):
            await call("engine", "ta_compute", **ARGS["ta_compute"])
    finally:
        writer.close()


async def test_read_only_connections_nothing_is_written(eng: Path) -> None:
    def counts() -> list[int]:
        d = duckdb.connect(str(eng / "nivesh.duckdb"), read_only=True)
        s = open_sqlite(eng / "nivesh.sqlite")
        try:
            out = [
                d.execute(f'SELECT count(*) FROM "{t[0]}"').fetchone()[0]  # type: ignore[index]  # noqa: S608
                for t in d.execute("SHOW TABLES").fetchall()
            ]
            names = [r[0] for r in s.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            out += [s.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names]  # noqa: S608
            return out
        finally:
            d.close()
            s.close()

    before = counts()
    for name in TOOLS:
        try:
            await call("engine", name, **ARGS[name])
        except ToolError:
            pass
    assert counts() == before


def test_server_runs_over_stdio_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from nivesh_mcp.__main__ import main

    seen: list[Any] = []
    monkeypatch.setattr(engine.server.mcp, "run", lambda **kw: seen.append(kw))
    assert main(["engine"]) == 0 and seen == [{"transport": "stdio", "show_banner": False}]


def test_profile_and_settings_helpers_are_read_from_the_config_dir(
    eng: Path, cli_env: tuple[list[str], Path]
) -> None:
    assert engine._profile().max_position_pct == 10
    assert isinstance(common.settings(), Settings)
    assert make_profile().max_position_pct == 30
