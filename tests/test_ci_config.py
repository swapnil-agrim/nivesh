from pathlib import Path

import yaml

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def _run_text(wf: dict) -> str:
    return "\n".join(
        s.get("run", "") for job in wf["jobs"].values() for s in job["steps"] if "run" in s
    )


def test_ci_triggers_matrix_and_permissions() -> None:
    ci = _load("ci.yml")
    triggers = ci.get(True) or ci["on"]  # PyYAML parses bare `on` as True
    assert "push" in triggers and "pull_request" in triggers
    assert ci["permissions"] == {"contents": "read"}
    job = ci["jobs"]["ci"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest"]


def test_ci_runs_all_gates() -> None:
    ci = _load("ci.yml")
    text = _run_text(ci)
    assert "uv sync --frozen" in text
    assert "make check" in text  # ruff + mypy + pytest/cov share one command
    assert "pip-audit" in text
    assert "gitleaks" in text
    assert "tests/safety" in text
    steps = ci["jobs"]["ci"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    assert not any("gitleaks/gitleaks-action" in str(s.get("uses", "")) for s in steps)


def test_make_check_has_coverage_gate() -> None:
    mk = (WF.parent.parent / "Makefile").read_text()
    for needle in ("--cov=nivesh_engine", "--cov=nivesh_adapters", "--cov-fail-under=85"):
        assert needle in mk
    assert "ruff" in mk and "mypy" in mk and "pytest" in mk


def test_nightly() -> None:
    n = _load("nightly.yml")
    triggers = n.get(True) or n["on"]
    assert triggers["schedule"][0]["cron"]
    text = _run_text(n)
    assert "-m bench" in text and "-m eval" in text and "pip-audit" in text
    assert "report.md" in text
    uses = [s.get("uses", "") for j in n["jobs"].values() for s in j["steps"]]
    assert any(u.startswith("actions/upload-artifact") for u in uses)


def test_ci_secret_scan_and_least_privilege_locked_in() -> None:
    # lock-in (already true): CI fails on committed secrets using full history
    ci = _load("ci.yml")
    assert "gitleaks detect" in _run_text(ci)
    assert ci["permissions"] == {"contents": "read"}


def test_nightly_has_pii_scan_job() -> None:
    n = _load("nightly.yml")
    assert "tests/security" in _run_text({"jobs": {"pii-scan": n["jobs"]["pii-scan"]}})


def test_ci_builds_docker_image_on_linux_only() -> None:
    steps = _load("ci.yml")["jobs"]["ci"]["steps"]
    (step,) = [s for s in steps if "docker build" in s.get("run", "")]
    assert step["if"] == "runner.os == 'Linux'"
