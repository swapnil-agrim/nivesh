import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from typer.testing import CliRunner

from nivesh_agents import runtime
from nivesh_agents.runtime import AgentRunError, build_options, load_command, run_command
from nivesh_cli.main import app
from nivesh_mcp.registry import SERVERS

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = {"Bash", "Write", "Edit", "WebFetch", "WebSearch", "NotebookEdit"}


def result(text: str = "pong", error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=error,
        num_turns=1, session_id="s", result=text,
    )  # fmt: skip


def fake_query(*messages: Any, raises: Exception | None = None) -> Any:
    seen: dict[str, Any] = {}

    async def q(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        seen.update(prompt=prompt, options=options)
        if raises:
            raise raises
        for m in messages:
            yield m

    q.seen = seen  # type: ignore[attr-defined]
    return q


@pytest.mark.parametrize("mode", ["prod", "dev"])
def test_options_allow_only_registry_mcp_tools(mode: str) -> None:
    o = build_options(mode=mode)
    expected = [f"mcp__{n}__{t}" for n, s in SERVERS.items() for t in s.tool_names]
    assert o.allowed_tools == expected
    assert o.tools == []  # no built-in tools at all
    assert FORBIDDEN <= set(o.disallowed_tools)
    assert o.permission_mode == "dontAsk"  # deny anything not pre-approved
    assert o.strict_mcp_config is True and o.setting_sources == ["project"]
    assert set(o.mcp_servers) == set(SERVERS)  # type: ignore[arg-type]


def test_dev_is_subset_of_claude_settings() -> None:
    allow = set(
        json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]["allow"]
    )
    assert set(build_options(mode="dev").allowed_tools) <= allow


def test_bad_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        build_options(mode="staging")


def test_load_command_substitutes_arguments_and_strips_frontmatter() -> None:
    text = load_command("ping", ["a", "b"])
    assert not text.startswith("---") and "ping" in text
    assert "$ARGUMENTS" not in text


@pytest.mark.parametrize("bad", ["../etc/passwd", "Ping", "a/b", "", "nope-missing"])
def test_load_command_rejects_bad_names(bad: str) -> None:
    with pytest.raises(AgentRunError):
        load_command(bad, [])


async def test_run_command_success(monkeypatch: pytest.MonkeyPatch) -> None:
    q = fake_query(result("pong!"))
    monkeypatch.setattr(runtime, "query", q)
    assert await run_command("ping", [], mode="prod") == "pong!"
    assert "ping" in q.seen["prompt"].lower()


async def test_run_command_error_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "query", fake_query(result("boom", error=True)))
    with pytest.raises(AgentRunError, match="boom"):
        await run_command("ping", [], mode="prod")


async def test_run_command_no_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "query", fake_query())
    with pytest.raises(AgentRunError, match="no result"):
        await run_command("ping", [], mode="prod")


async def test_refresh_passes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    q = fake_query(result())
    monkeypatch.setattr(runtime, "query", q)
    await run_command("ping", [], mode="prod", refresh=True)
    assert q.seen["options"].env["NIVESH_REFRESH"] == "1"
    q2 = fake_query(result())
    monkeypatch.setattr(runtime, "query", q2)
    await run_command("ping", [], mode="prod")
    assert "NIVESH_REFRESH" not in q2.seen["options"].env


def test_redact_error_masks_token() -> None:
    msg = runtime.safe_error(
        RuntimeError(
            "auth failed with " + "Bea" + "rer abc123.def and " + "sk-ant-" + "api03-SECRETSECRET"
        )
    )
    assert "abc123" not in msg and "SECRETSECRET" not in msg and "auth failed" in msg


CFG = ["--config-dir", str(ROOT / "config")]


def test_cli_run_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "query", fake_query(result("pong!")))
    r = CliRunner().invoke(app, [*CFG, "run", "ping"])
    assert r.exit_code == 0, r.output
    assert "pong!" in r.output


def test_cli_run_refresh_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    q = fake_query(result())
    monkeypatch.setattr(runtime, "query", q)
    r = CliRunner().invoke(app, [*CFG, "run", "ping", "--refresh"])
    assert r.exit_code == 0, r.output
    assert q.seen["options"].env["NIVESH_REFRESH"] == "1"


def test_cli_run_sdk_exception_exits_nonzero_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime, "query", fake_query(raises=RuntimeError("bad " + "Bea" + "rer tok.en123"))
    )
    r = CliRunner().invoke(app, [*CFG, "run", "ping"])
    assert r.exit_code != 0
    assert "bad" in r.output and "tok.en123" not in r.output


def test_cli_run_error_result_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "query", fake_query(result("it broke", error=True)))
    r = CliRunner().invoke(app, [*CFG, "run", "ping"])
    assert r.exit_code != 0 and "it broke" in r.output


def test_cli_run_unknown_command_exits_nonzero() -> None:
    r = CliRunner().invoke(app, [*CFG, "run", "nope-missing"])
    assert r.exit_code != 0


@pytest.mark.live
async def test_live_sdk_smoke() -> None:
    assert "pong" in (await run_command("ping", [], mode="prod")).lower()
