from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core import egress
from nivesh_core.egress import check_egress

ROOT = Path(__file__).resolve().parents[2]
IP = "203.0.113.7"


def client(handler, calls: list[int]):  # type: ignore[no-untyped-def]
    def counted(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(req)

    return httpx.Client(transport=httpx.MockTransport(counted))


def test_match() -> None:
    calls: list[int] = []
    r = check_egress(
        IP, "https://x.test", client(lambda q: httpx.Response(200, text=IP + "\n"), calls)
    )
    assert (r.status, r.ip) == ("match", IP) and calls == [1]


def test_mismatch_names_both_ips() -> None:
    r = check_egress(
        IP, "https://x.test", client(lambda q: httpx.Response(200, text="198.51.100.9"), [])
    )
    assert r.status == "mismatch" and IP in r.message and "198.51.100.9" in r.message


def test_unset_registered_ip_skips_without_request() -> None:
    calls: list[int] = []
    r = check_egress(None, "https://x.test", client(lambda q: httpx.Response(200, text=IP), calls))
    assert r.status == "skipped" and calls == []


@pytest.mark.parametrize("body", ["<html>nope</html>", "", "999.1.1.1"])
def test_garbage_body_is_error_single_request(body: str) -> None:
    calls: list[int] = []
    r = check_egress(IP, "https://x.test", client(lambda q: httpx.Response(200, text=body), calls))
    assert r.status == "error" and calls == [1]


def test_transport_error_reported_not_retried() -> None:
    calls: list[int] = []

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    r = check_egress(IP, "https://x.test", client(boom, calls))
    assert r.status == "error" and calls == [1]


def cfg(tmp_path: Path, ip: str | None) -> list[str]:
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    text = (ROOT / "config" / "nivesh.yaml").read_text()
    if ip:
        text = text.replace("# registered_ip: 203.0.113.7", f"registered_ip: {ip}")
    (d / "nivesh.yaml").write_text(text.replace("data_dir: data", "data_dir: dd"))
    (d / "profile.yaml").write_text((ROOT / "config" / "profile.yaml").read_text())
    return ["--config-dir", str(d)]


def patch_get(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(
        egress, "_default_client", lambda: client(lambda q: httpx.Response(200, text=text), [])
    )


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    patch_get(monkeypatch, IP)
    assert CliRunner().invoke(app, [*cfg(tmp_path, IP), "egress-check"]).exit_code == 0
    assert CliRunner().invoke(app, [*cfg(tmp_path, None), "egress-check"]).exit_code == 0
    patch_get(monkeypatch, "198.51.100.9")
    assert CliRunner().invoke(app, [*cfg(tmp_path, IP), "egress-check"]).exit_code == 1


def test_run_warns_on_mismatch_but_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.agents.test_runtime import fake_query, result

    monkeypatch.chdir(tmp_path)
    patch_get(monkeypatch, "198.51.100.9")
    monkeypatch.setattr(runtime, "query", fake_query(result("pong")))
    r = CliRunner().invoke(app, [*cfg(tmp_path, IP), "run", "ping"])
    assert r.exit_code == 0 and "egress" in r.output and "pong" in r.output


def test_run_without_registered_ip_makes_no_egress_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.agents.test_runtime import fake_query, result

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(egress, "_default_client", lambda: pytest.fail("no egress call expected"))
    monkeypatch.setattr(runtime, "query", fake_query(result("pong")))
    assert CliRunner().invoke(app, [*cfg(tmp_path, None), "run", "ping"]).exit_code == 0
