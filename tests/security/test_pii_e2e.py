"""PII never reaches tool output, recorded fixtures or error text (NFR-3). Run nightly too."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from nivesh_adapters.base import AdapterResult
from nivesh_adapters.recorder import make_client
from nivesh_agents.runtime import safe_error
from nivesh_core.pii_scan import scan_paths, scan_text
from nivesh_core.trace import Tracer
from nivesh_mcp.base import ReadOnlyServer
from tests import pii_values as pv

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def pii_blob() -> str:
    return " | ".join(m() for m in (pv.pan, pv.phone, pv.email, pv.folio, pv.api_key, pv.bearer))


async def test_no_pii_in_tool_output_fixtures_or_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = pii_blob()
    assert scan_text(raw), "control: scanner must see the raw input"

    # (a) tool wrapper
    s = ReadOnlyServer("demo")

    @s.tool
    def leaky() -> AdapterResult:
        """Return PII-laden data."""
        return AdapterResult(data={"a": [{"b": raw}]}, source="t", as_of=NOW, fetched_at=NOW)

    async with Client(s.mcp) as c:
        res = await c.call_tool("leaky", {})
    out = tmp_path / "out"
    out.mkdir()
    (out / "tool.json").write_text(res.content[0].text)  # type: ignore[union-attr]
    assert json.loads((out / "tool.json").read_text())["source"] == "t"

    # (b) recorder
    monkeypatch.setenv("NIVESH_RECORD", "1")
    fx = tmp_path / "fx"
    client = make_client(
        "demo",
        fixture_dir=fx,
        inner=httpx.MockTransport(lambda r: httpx.Response(200, json={"note": raw})),
    )
    client.get("https://example.test/q")

    # (c) error text
    (out / "err.txt").write_text(safe_error(RuntimeError(raw)))

    # (d) trace output
    tr = Tracer(out, 1)
    tr.assistant_text(raw)
    tr.tool_call("t1", "demo", {"q": raw})
    tr.tool_result("t1", {"rows": [raw]})
    tr.engine_call("f", [raw], raw)
    tr.error(raw)

    assert scan_paths([out, fx]) == []


def test_cas_pipeline_leaves_no_pii_anywhere(
    cli_env: tuple[list[str], Path], fake_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring
    from typer.testing import CliRunner

    from nivesh_adapters import cas
    from nivesh_cli.main import app
    from nivesh_core.db import init_stores
    from nivesh_core.db.sqlite import open_sqlite
    from tests.cas_models import demat_data, pii_strings, rta_data

    args, data = cli_env
    raw = demat_data().model_dump_json() + rta_data().model_dump_json()
    assert scan_text(raw), "control: the raw models contain PII"

    keyring.set_password("nivesh", "CAS_PASSWORD", pv.cas_password())
    keyring.set_password("nivesh", "FOLIO_SALT", pv.salt())
    models = {b"demat": demat_data(), b"rta": rta_data(parse_warnings=["warn " + pv.email()])}
    monkeypatch.setattr(cas, "_read", lambda p, pw: models[p.read_bytes()])
    init_stores(data)
    (data / "inbox").mkdir()
    (data / "inbox" / "a.pdf").write_bytes(b"demat")
    (data / "inbox" / "b.pdf").write_bytes(b"rta")

    r = CliRunner().invoke(app, [*args, "ingest"])
    assert r.exit_code == 0, r.output

    conn = open_sqlite(data / "nivesh.sqlite")
    dump = "\n".join(conn.iterdump())
    reports = " ".join(row[0] for row in conn.execute("select report from ingest"))
    conn.close()
    files = "\n".join(
        p.read_text()
        for p in data.rglob("*")
        if p.is_file() and p.suffix in {".json", ".jsonl", ".txt"}
    )
    for label, text in {"db": dump, "report": reports, "cli": r.output, "files": files}.items():
        assert scan_text(text) == [], label
        for pii in pii_strings():
            assert pii not in text, label
    assert scan_paths([data / "inbox"]) == []
