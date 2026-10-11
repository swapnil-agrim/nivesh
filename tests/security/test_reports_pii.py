"""Reports, saved run files and delivered payloads carry no PII or key-shaped value (NFR-3)."""

import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

import nivesh_cli.deliver as cdeliver
import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core.pii_scan import scan_paths, scan_text
from tests.agents import committee_fx as fx
from tests.cli.test_ideas_cli import edit
from tests.cli.test_ideas_report_cli import clean_pm
from tests.delivery_fx import DUMMY_HOOK, Calls, FakeSmtp, body_json
from tests.ideas_fx import ASOF, seed_ideas_store, with_benchmarks
from tests.run_paths import run_path

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    monkeypatch.setattr(runtime, "query", fx.happy(pm=clean_pm))
    return cli_env


def call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def test_full_run_leaves_no_pii_in_run_dir_report_md_html_input_json_or_trace(env: Env) -> None:
    r = call(env, "research", "AAA")
    b = call(env, "brief", "both")
    assert r.exit_code == 0 and b.exit_code == 0, (r.output, b.output)
    for run_id in (1, 2):
        d = run_path(env[1], run_id)
        files = [p for p in d.rglob("*") if p.is_file()]
        names = {p.name for p in files}
        assert {"report.md", "report.html", "report_input.json", "report.json"} <= names
        assert scan_paths(files) == [], d
    assert scan_text(r.output) == [] and scan_text(b.output) == []


def test_delivered_payloads_pass_scan_text(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    edit(env, "nivesh.yaml", "channels: []", "channels: [slack, telegram, email]")
    for field, ref in (("slack_hook", "S_HOOK"), ("telegram_bot", "T_BOT"),
                       ("telegram_chat", "T_CHAT"), ("smtp_host", "M_HOST"),
                       ("smtp_user", "M_USER"), ("smtp_auth", "M_AUTH"),
                       ("mail_to", "M_TO")):  # fmt: skip
        edit(env, "nivesh.yaml", f"# {field}: ref:{field.upper()}", f"{field}: ref:{ref}")
    from tests.delivery_fx import set_refs

    set_refs(monkeypatch)
    calls, smtp = Calls(), FakeSmtp()
    monkeypatch.setattr(cdeliver, "http_client", lambda timeout: calls.client())
    monkeypatch.setattr(cdeliver, "smtp_factory", lambda: smtp.factory)
    assert call(env, "research", "AAA", "--deliver").exit_code == 0
    assert len(calls.requests) == 3 and len(smtp.sent) == 1
    bodies = [parse_qs(calls.requests[0].content.decode())["text"][0]]  # telegram text
    bodies += [calls.requests[1].content.decode(errors="replace")]  # telegram page
    bodies += [body_json(calls.requests[2])["text"]]  # slack
    mail = smtp.sent[0]
    bodies += [mail.get_body(preferencelist=("plain",)).get_content()]  # type: ignore[union-attr]
    bodies += [a.get_content() for a in mail.iter_attachments()] + [mail["Subject"]]
    assert all(scan_text(str(x).replace(DUMMY_HOOK, "")) == [] for x in bodies)


KEYLIKE = re.compile(r"[A-Za-z0-9]{20,}")
FIELDS = "telegram_bot|telegram_chat|slack_hook|smtp_host|smtp_user|smtp_auth|mail_to"


def test_new_fixtures_commands_and_docs_have_no_key_shaped_literals() -> None:
    adr = ROOT / "docs" / "adr" / "0012-reports-commands-delivery.md"
    paths = [
        *(ROOT / ".claude" / "commands").glob("*.md"),
        *(ROOT / "tests" / "fixtures" / "reports").rglob("*"),
        ROOT / "tests" / "report_fx.py", ROOT / "tests" / "delivery_fx.py",
        ROOT / "config" / "nivesh.yaml", adr,
    ]  # fmt: skip
    for p in (q for q in paths if q.is_file()):
        text = p.read_text()
        assert scan_text(text) == [], p.name
        long_runs = [m for m in KEYLIKE.findall(text) if not m.isalpha() and not m.isdigit()]
        assert long_runs == [] or all("_" in m or "-" in m for m in long_runs), (p.name, long_runs)
        assert not re.search(r"(?i)\b(secret|password|credential)\b\s*[:=]", text), p.name


def test_config_example_has_only_ref_names() -> None:
    block = (ROOT / "config" / "nivesh.yaml").read_text().split("\ndelivery:", 1)[1]
    for line in block.splitlines():
        m = re.match(
            r"\s*#?\s*(telegram_bot|telegram_chat|slack_hook|smtp_\w+|mail_to):\s*(\S+)", line
        )
        if m:
            assert re.fullmatch(r"ref:[A-Z][A-Z0-9_]*", m.group(2)), line


def test_delivery_leaves_no_file_or_row_behind_when_nothing_is_configured(env: Env) -> None:
    assert call(env, "brief", "india", "--deliver").exit_code == 0
    d = run_path(env[1], 1)
    assert sorted(p.name for p in d.iterdir()) == [
        "report.html", "report.json", "report.md", "report_input.json", "trace.jsonl",
    ]  # fmt: skip
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        assert [r[0] for r in c.execute("SELECT command FROM run")] == ["brief"]
    finally:
        c.close()
