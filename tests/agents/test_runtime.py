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


# --- ST-13.3 / 13.4: tracing, run rows, budget gate ---------------------------------------------
import sqlite3  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from nivesh_core.pii_scan import scan_paths  # noqa: E402
from nivesh_core.timeutil import to_iso  # noqa: E402
from nivesh_core.trace import Tracer, read_trace  # noqa: E402
from tests import pii_values as pv  # noqa: E402


def full_run(text: str = "pong") -> list[Any]:
    return [
        AssistantMessage(
            content=[
                TextBlock(text="calling " + pv.email()),
                ToolUseBlock("tu1", "mcp__demo__ping", {"a": 1}),
            ],
            model="claude-test",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="tu1", content=pv.pan(), is_error=False)]),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
            session_id="s", result=text, total_cost_usd=0.01,
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
    ]  # fmt: skip


async def test_run_command_traces_all_record_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", fake_query(*full_run()))
    tracer = Tracer(tmp_path, 1)
    assert await run_command("ping", [], mode="prod", tracer=tracer) == "pong"
    recs = read_trace(tmp_path / "trace.jsonl")
    assert [r["type"] for r in recs] == [
        "start", "assistant_text", "tool_call", "tool_result", "result",
    ]  # fmt: skip
    assert recs[0]["prompt_version"] == tracer.prompt_ver and len(tracer.prompt_ver or "") == 12
    assert recs[-1]["model"] == "claude-test" and recs[-1]["input_tokens"] == 100
    assert recs[-1]["cost_source"] == "sdk" and recs[-1]["cost_inr"] > 0
    assert scan_paths([tmp_path]) == []


def cfg_dir(tmp_path: Path, *, cap: int = 2000) -> list[str]:
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    (d / "nivesh.yaml").write_text(
        (ROOT / "config" / "nivesh.yaml").read_text().replace("data_dir: data", "data_dir: dd")
    )
    (d / "profile.yaml").write_text(
        (ROOT / "config" / "profile.yaml")
        .read_text()
        .replace("monthly_cost_cap: 2000", f"monthly_cost_cap: {cap}")
    )
    return ["--config-dir", str(d)]


def rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(tmp_path / "dd" / "nivesh.sqlite")
    try:
        return c.execute(
            "select status, tier, model, input_tokens, cost_inr, run_dir from run order by id"
        ).fetchall()
    finally:
        c.close()


def test_cli_run_records_row_and_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "query", fake_query(*full_run()))
    r = CliRunner().invoke(app, [*cfg_dir(tmp_path), "run", "ping"])
    assert r.exit_code == 0, r.output
    ((status, tier, model, tok, cost, rdir),) = rows(tmp_path)
    assert (status, tier, model, tok, rdir) == ("ok", "quick", "claude-test", 100, "runs/1")
    assert cost > 0
    assert (tmp_path / "dd" / "runs" / "1" / "trace.jsonl").is_file()


def test_cli_run_error_row_and_redacted_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "query", fake_query(raises=RuntimeError("bad " + pv.bearer())))
    r = CliRunner().invoke(app, [*cfg_dir(tmp_path), "run", "ping"])
    assert r.exit_code == 1
    assert rows(tmp_path)[0][0] == "error"
    recs = read_trace(tmp_path / "dd" / "runs" / "1" / "trace.jsonl")
    assert recs[-1]["type"] == "error" and "abc.def123" not in recs[-1]["message"]


def seed_spend(tmp_path: Path, inr: float) -> None:
    from nivesh_core.db import init_stores
    from nivesh_core.timeutil import utcnow

    init_stores(tmp_path / "dd")
    c = sqlite3.connect(tmp_path / "dd" / "nivesh.sqlite")
    c.execute(
        "insert into run (command, started_at, status, cost_inr) values ('x', ?, 'ok', ?)",
        (to_iso(utcnow()), inr),
    )
    c.commit()
    c.close()


def test_deep_at_85_percent_downgrades_and_force_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_dir(tmp_path, cap=1000)
    seed_spend(tmp_path, 850)
    monkeypatch.setattr(runtime, "query", fake_query(*full_run()))
    r = CliRunner().invoke(app, [*cfg, "run", "ping", "--tier", "deep"])
    assert r.exit_code == 0 and "downgraded" in r.output  # stderr is mixed into output
    r = CliRunner().invoke(app, [*cfg, "run", "ping", "--tier", "deep", "--force"])
    assert r.exit_code == 0
    assert [x[1] for x in rows(tmp_path)][1:] == ["quick", "deep"]


def test_deep_at_100_percent_refused_before_any_sdk_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_dir(tmp_path, cap=1000)
    seed_spend(tmp_path, 1000)
    q = fake_query(*full_run())
    monkeypatch.setattr(runtime, "query", q)
    for extra in ([], ["--force"]):
        r = CliRunner().invoke(app, [*cfg, "run", "ping", "--tier", "deep", *extra])
        assert r.exit_code == 1 and "refused" in r.output
    assert q.seen == {} and len(rows(tmp_path)) == 1  # no SDK call, no run row
    assert CliRunner().invoke(app, [*cfg, "run", "ping", "--tier", "quick"]).exit_code == 0


def test_bad_tier_rejected(tmp_path: Path) -> None:
    r = CliRunner().invoke(app, [*cfg_dir(tmp_path), "run", "ping", "--tier", "huge"])
    assert r.exit_code == 2


def test_status_prints_mtd_and_cap(tmp_path: Path) -> None:
    cfg = cfg_dir(tmp_path, cap=1000)
    seed_spend(tmp_path, 850)
    r = CliRunner().invoke(app, [*cfg, "status"])
    assert r.exit_code == 0, r.output
    assert "850.00" in r.output and "1000.00" in r.output and "85%" in r.output
    assert "downgraded" in r.output


async def test_failed_run_still_records_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = ResultMessage(
        subtype="error", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
        session_id="s", result="boom", total_cost_usd=0.01,
        usage={"input_tokens": 100, "output_tokens": 20},
    )  # fmt: skip
    monkeypatch.setattr(runtime, "query", fake_query(bad))
    tracer = Tracer(tmp_path, 1)
    with pytest.raises(AgentRunError):
        await run_command("ping", [], mode="prod", tracer=tracer)
    assert tracer.summary["input_tokens"] == 100 and tracer.summary["cost_inr"] > 0
