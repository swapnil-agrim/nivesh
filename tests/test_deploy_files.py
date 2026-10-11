"""Static checks on the deploy files (lock-in; nothing here runs docker)."""

import re
from pathlib import Path

import yaml

from nivesh_core.paths import ensure_data_dir
from nivesh_core.pii_scan import scan_text

ROOT = Path(__file__).resolve().parents[1]
SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


def dockerfile() -> list[str]:
    return [ln.strip() for ln in (ROOT / "Dockerfile").read_text().splitlines()]


def test_dockerfile_pinned_nonroot_age_entrypoint() -> None:
    lines = dockerfile()
    (base,) = [ln for ln in lines if ln.startswith("FROM ")]
    assert ":" in base and "latest" not in base
    text = "\n".join(lines)
    assert "age" in text and "apt-get install" in text
    users = [ln for ln in lines if ln.startswith("USER ")]
    assert users and users[-1] != "USER root" and users[-1] != "USER 0"
    assert 'ENTRYPOINT ["nivesh"]' in lines
    assert "uv sync --frozen --no-dev" in text


def test_dockerfile_bakes_no_secrets() -> None:
    for ln in dockerfile():
        if ln.startswith(("ENV ", "ARG ")):
            names = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", ln) or [ln.split()[1]]
            assert not any(SECRETISH.search(n) for n in names), ln
        assert not re.search(r"COPY\s+.*\.env", ln)


def test_compose_service() -> None:
    svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["nivesh"]
    assert "./data:/data" in svc["volumes"]
    assert "ports" not in svc
    assert svc["read_only"] is True and "/tmp" in svc["tmpfs"]
    for entry in svc.get("environment", []):
        assert "=" not in str(entry), "secrets must be pass-through names, not values"
    assert "env_file" not in svc or all("=" not in str(f) for f in svc["env_file"])
    assert "scheduler" in (ROOT / "docker-compose.yml").read_text().lower()  # deferral is stated


def test_container_config_points_data_dir_at_volume() -> None:
    assert "data_dir: /data" in (ROOT / "Dockerfile").read_text()
    assert "NIVESH_CONFIG_DIR=/cfg" in (ROOT / "Dockerfile").read_text()


def test_dockerignore_excludes_sensitive_and_heavy_dirs() -> None:
    lines = set((ROOT / ".dockerignore").read_text().split())
    assert {".git", "data", ".env", ".sdlc", "tests", ".venv"} <= lines


def test_vm_guide_headings_and_no_secret_literals() -> None:
    text = (ROOT / "docs" / "deploy" / "vm.md").read_text()
    for heading in (
        "## VM", "## Static IP", "## Firewall", "## Secrets", "## Data volume ownership",
        "## Register the IP with HDFC", "## Daily login via SSH tunnel", "## Egress check",
        "## Backup cron",
    ):  # fmt: skip
        assert heading in text, heading
    assert "Mumbai" in text and "not verified by tests" in text and "10001" in text
    assert scan_text(text) == [] and not re.search(r"sk-[A-Za-z0-9]{8,}", text)


def test_existing_empty_dir_ends_0700(tmp_path: Path) -> None:
    # TD-7: a pre-created empty mount point is tightened to owner-only
    tmp_path.chmod(0o755)
    ensure_data_dir(tmp_path)
    assert (tmp_path.stat().st_mode & 0o777) == 0o700


EXAMPLES = ROOT / "docs" / "deploy" / "examples"


def test_schedule_examples_are_text_only_and_call_the_tick_every_five_minutes() -> None:
    files = {p.name: p.read_text() for p in EXAMPLES.iterdir()}
    assert set(files) == {
        "nivesh-schedule.cron", "nivesh-schedule.service", "nivesh-schedule.timer",
        "com.nivesh.schedule.plist",
    }  # fmt: skip
    for name, text in files.items():
        assert "nothing is installed" in text, name
        assert scan_text(text) == [] and not re.search(r"sk-[A-Za-z0-9]{8,}", text), name
        assert not SECRETISH.search(re.sub(r"</?key>", "", text)), name  # plist tags
        assert "/opt/nivesh" in text or name.endswith(".timer"), name
    assert "nivesh schedule run" in files["nivesh-schedule.cron"]
    assert files["nivesh-schedule.cron"].count("*/5 * * * *") == 1
    assert "ExecStart=/opt/nivesh/.venv/bin/nivesh schedule run" in files["nivesh-schedule.service"]
    assert "OnUnitActiveSec=5min" in files["nivesh-schedule.timer"]
    plist = files["com.nivesh.schedule.plist"]
    assert "<integer>300</integer>" in plist and "<string>schedule</string>" in plist


def test_schedule_guide_names_the_tick_and_the_owner_actions() -> None:
    text = (ROOT / "docs" / "deploy" / "schedule.md").read_text()
    for needle in (
        "nivesh schedule run", "every five minutes", "Nothing is installed", "idempotent".title(),
        "nse_holidays", "nivesh schedule invalidation", "missed window", "catchup_hours",
        "18:30 IST", "19:30 IST", "No ingest job",
    ):  # fmt: skip
        assert needle in text, needle
    assert scan_text(text) == [] and not SECRETISH.search(text)
