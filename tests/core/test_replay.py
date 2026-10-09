import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_core.paths import run_dir
from nivesh_core.replay import REPLAYABLE, replay_run
from nivesh_core.trace import Tracer

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def add(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REPLAYABLE, "add", lambda a, b: a + b)


def trace(d: Path, calls: list[tuple[str, list[float], float]]) -> None:
    t = Tracer(d, 1)
    t.assistant_text("hi")
    for name, args, result in calls:
        t.engine_call(name, args, result)


@pytest.mark.usefixtures("add")
def test_identical_replay(tmp_path: Path) -> None:
    trace(tmp_path, [("add", [1, 2], 3), ("add", [0.1, 0.2], 0.1 + 0.2)])
    r = replay_run(tmp_path)
    assert (r.ok, r.steps, r.failures) == (True, 2, [])


@pytest.mark.usefixtures("add")
def test_tampered_result_names_the_step(tmp_path: Path) -> None:
    trace(tmp_path, [("add", [1, 2], 3), ("add", [1, 2], 4)])
    r = replay_run(tmp_path)
    assert not r.ok and len(r.failures) == 1 and "step 3" in r.failures[0]


def test_unknown_function_is_unreplayable_not_skipped(tmp_path: Path) -> None:
    trace(tmp_path, [("nope", [1], 1)])
    r = replay_run(tmp_path)
    assert not r.ok and "unreplayable" in r.failures[0] and "nope" in r.failures[0]


def test_exact_equality_no_tolerance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REPLAYABLE, "f", lambda: 0.30000000000000004)
    trace(tmp_path, [("f", [], 0.3)])
    # args=[] -> f() ; stored 0.3 differs from computed 0.30000000000000004
    assert not replay_run(tmp_path).ok


def setup_cli(tmp_path: Path) -> list[str]:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "nivesh.yaml").write_text(
        (ROOT / "config" / "nivesh.yaml").read_text().replace("data_dir: data", "data_dir: dd")
    )
    (cfg / "profile.yaml").write_text((ROOT / "config" / "profile.yaml").read_text())
    return ["--config-dir", str(cfg)]


@pytest.mark.usefixtures("add")
def test_cli_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = setup_cli(tmp_path)
    d = run_dir(tmp_path / "dd", 1)
    trace(d, [("add", [1, 2], 3)])
    r = CliRunner().invoke(app, [*cfg, "replay", "1"])
    assert r.exit_code == 0 and "replayed 1 steps, identical" in r.output
    lines = (d / "trace.jsonl").read_text().splitlines()
    rec = json.loads(lines[-1])
    rec["result"] = 99
    (d / "trace.jsonl").write_text("\n".join([*lines[:-1], json.dumps(rec)]) + "\n")
    assert CliRunner().invoke(app, [*cfg, "replay", "1"]).exit_code == 1


def test_cli_replay_no_engine_steps_and_missing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = setup_cli(tmp_path)
    trace(run_dir(tmp_path / "dd", 1), [])
    r = CliRunner().invoke(app, [*cfg, "replay", "1"])
    assert r.exit_code == 0 and "no engine steps to replay" in r.output
    assert CliRunner().invoke(app, [*cfg, "replay", "9"]).exit_code == 1
