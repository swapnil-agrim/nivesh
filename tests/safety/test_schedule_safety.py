"""Safety checks for the scheduler and the alerts (E11): no way to install, run a program, open
a connection, write an order or reach a layer above; nothing leaves except through the injected
notifier."""

import ast
import re
from pathlib import Path

import pytest

import nivesh_cli.schedule as cs
from nivesh_adapters.scheduler import JobCtx, run_due
from nivesh_core.config import load_settings
from nivesh_core.profile import load_profile
from nivesh_core.schedule_config import JobSpec, ScheduleSettings
from nivesh_mcp.base import _desc_write_words, is_write_name
from tests.adapters.test_scheduler import CALS, EIGHT, Env

ROOT = Path(__file__).resolve().parents[2]
FILES = [
    "nivesh_engine/schedule.py", "nivesh_engine/alert_rules.py", "nivesh_adapters/scheduler.py",
    "nivesh_adapters/schedule_jobs.py", "nivesh_adapters/alerts_service.py",
    "nivesh_core/schedule_config.py", "nivesh_core/alerts.py", "nivesh_cli/schedule.py",
]  # fmt: skip
BANNED_IMPORTS = {"subprocess", "socket", "requests", "httpx", "urllib", "aiohttp", "nivesh_mcp"}
LAYERS_BELOW_THE_CLI = ("nivesh_adapters", "nivesh_engine", "nivesh_core", "nivesh_agents")


def present() -> list[Path]:
    return [ROOT / f for f in FILES if (ROOT / f).exists()]


def imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_the_scanned_files_are_the_ones_that_exist() -> None:
    assert len(present()) >= 6  # the full list is pinned once every module has landed


def test_no_install_program_or_connection_code() -> None:
    for path in present():
        text = path.read_text()
        roots = {m.split(".")[0] for m in imports(ast.parse(text))}
        assert not roots & BANNED_IMPORTS, (path.name, roots & BANNED_IMPORTS)
        assert not re.search(r"\bos\.(system|popen|exec\w*|spawn\w*)\b", text), path.name
        assert not re.search(r"crontab|launchctl|systemctl|Popen", text), path.name


def test_scheduler_code_never_opens_the_deploy_examples() -> None:
    for pkg in (*LAYERS_BELOW_THE_CLI, "nivesh_cli"):
        for path in (ROOT / pkg).rglob("*.py"):
            assert "docs/deploy" not in path.read_text(), path


def test_no_write_words_in_names_or_docstrings() -> None:
    for path in present():
        tree = ast.parse(path.read_text())
        assert _desc_write_words(ast.get_docstring(tree) or "") == [], path.name
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert not is_write_name(node.name), (path.name, node.name)
                assert _desc_write_words(ast.get_docstring(node) or "") == [], (
                    path.name, node.name,
                )  # fmt: skip


def test_no_layer_below_the_cli_imports_the_cli_or_typer() -> None:
    for pkg in LAYERS_BELOW_THE_CLI:
        for path in (ROOT / pkg).rglob("*.py"):
            roots = {m.split(".")[0] for m in imports(ast.parse(path.read_text()))}
            assert not roots & {"typer", "nivesh_cli"}, (path, roots & {"typer", "nivesh_cli"})


def test_the_engine_has_no_time_zone_database() -> None:
    text = (ROOT / "nivesh_engine" / "schedule.py").read_text()
    assert "zoneinfo" not in text and "tzdata" not in text


def test_the_scheduler_does_not_touch_the_read_only_tool_servers() -> None:
    from nivesh_mcp.registry import SERVERS

    assert sorted(SERVERS) == sorted(
        "demo filings fundamentals holdings macro market news engine".split()
    )
    assert not [n for n in SERVERS if re.search("sched|alert|notify", n)]


def test_a_failure_notice_with_no_channel_sends_nothing(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Path(cli_env[0][1])
    settings, profile = load_settings(cfg / "nivesh.yaml"), load_profile(cfg / "profile.yaml")
    assert settings.delivery.channels == []

    def no_client(_: int) -> None:
        raise AssertionError("a client was built although no channel is configured")

    monkeypatch.setattr(cs, "http_client", no_client)

    def failing(_: JobCtx) -> None:
        raise RuntimeError("boom")

    sched = ScheduleSettings(jobs=[JobSpec(name="t1", market="IN", at="08:00")])
    reports = run_due(
        settings, profile, sched, {"t1": failing},  # type: ignore[dict-item]
        EIGHT, cs.real_notifier(settings), lambda _s: None, calendars=CALS,
    )  # fmt: skip
    assert [(r.status, r.notified) for r in reports] == [("failed", False)]
    assert "no channel configured" in reports[0].reason
