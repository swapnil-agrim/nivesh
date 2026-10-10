import hashlib
import json
import re
import stat
from pathlib import Path

import pytest

from nivesh_agents.store import KINDS, RunStore
from nivesh_core.errors import NiveshError
from nivesh_core.pii_scan import scan_paths
from nivesh_core.trace import Tracer, digest_text, read_trace


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path, Tracer(tmp_path, 1))


def test_snapshot_json_has_the_documented_fields_and_config_digest_including_weights_version(
    store: RunStore, tmp_path: Path
) -> None:
    from decimal import Decimal

    from nivesh_agents.committee import CommitteeInputs, snapshot_payload
    from nivesh_core.agents_config import AgentsSettings
    from nivesh_core.config import Settings
    from tests.agents import committee_fx as fx

    inputs = CommitteeInputs(
        (fx.sec_input(1),), fx.AS_OF, {"max_position_pct": "10", "max_sector_pct": "30"}
    )
    settings = Settings()
    payload = snapshot_payload(inputs, "deep", AgentsSettings(), settings, 7)
    store.snapshot(payload)
    got = json.loads((tmp_path / "snapshot.json").read_text())
    assert set(got) == {
        "run_id", "as_of", "tier", "securities", "profile_limits", "engine_outputs", "coverage",
        "prompt_versions", "models", "config_digest",
    }  # fmt: skip
    assert got["run_id"] == 7 and got["tier"] == "deep" and got["as_of"] == "2026-01-02"
    assert got["securities"] == [{"security_id": 1, "symbol": "S001", "kind": "equity"}]
    assert got["profile_limits"] == {"max_position_pct": "10", "max_sector_pct": "30"}
    assert got["prompt_versions"]["pm"] == "v1" and got["models"]["pm"] == "opus"
    cd = got["config_digest"]
    assert cd["weights_version"] == settings.analysis.scoring.weights_version
    assert cd["weights_digest"] == digest_text(settings.analysis.scoring.weights_digest())
    assert (
        re.fullmatch(r"([0-9a-f]{8}-){7}[0-9a-f]{8}", cd["agents"])
        and Decimal(got["engine_outputs"]["1"]["risk_facts"]["tested_weight_pct"]) == 2
    )


def test_outputs_are_numbered_and_named_from_enums_and_integers_only(store: RunStore) -> None:
    store.output("fundamental", 3, {"a": 1})
    store.output("macro", None, {"a": 2})
    store.output("verdict", 3, {"a": 3})
    names = sorted(p.name for p in store.outputs.iterdir())
    assert names == ["001_fundamental_3.json", "002_macro_all.json", "003_verdict_3.json"]
    for bad in ("../x", "fundamental/../../y", "nope"):
        with pytest.raises(NiveshError, match="unknown output kind"):
            store.output(bad, 1, {})
    with pytest.raises(NiveshError, match="integer"):
        store.output("news", "1; rm", {})  # type: ignore[arg-type]
    assert "debate" in KINDS and len(list(store.outputs.iterdir())) == 3


def test_files_are_0600_and_the_dir_0700(store: RunStore, tmp_path: Path) -> None:
    store.snapshot({"a": 1})
    store.output("risk", 1, {"a": 1})
    assert stat.S_IMODE((tmp_path / "snapshot.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.outputs.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in store.outputs.iterdir())


def test_existing_file_is_never_overwritten(store: RunStore, tmp_path: Path) -> None:
    store.snapshot({"a": 1})
    with pytest.raises(FileExistsError):
        store.snapshot({"a": 2})
    assert json.loads((tmp_path / "snapshot.json").read_text()) == {"a": 1}
    other = RunStore(tmp_path, Tracer(tmp_path, 2))
    store.output("news", 1, {"k": 1})
    with pytest.raises(FileExistsError):
        other.output("news", 1, {"k": 2})  # same sequence number, same name: refused


def test_digest_matches_the_file_bytes_and_is_traced(store: RunStore, tmp_path: Path) -> None:
    digest = store.output("technical", 2, {"b": [1, 2]})
    path = next(store.outputs.iterdir())
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    rec = [r for r in read_trace(tmp_path / "trace.jsonl") if r["type"] == "output_saved"][0]
    assert rec["digest"] == digest_text(digest) and rec["name"] == path.name and "b" not in rec


def test_written_files_pass_the_pii_scan(store: RunStore, tmp_path: Path) -> None:
    store.snapshot({"securities": [{"security_id": 1, "symbol": "US1"}]})
    store.output("fundamental", 1, {"key_points": [{"claim": "ROCE is high"}]})
    assert scan_paths([tmp_path / "snapshot.json", store.outputs]) == []
    # key_points survives because outputs are not redacted
    assert "key_points" in next(store.outputs.iterdir()).read_text()


def test_failed_agent_gets_a_failed_marker_file(store: RunStore) -> None:
    store.failed("news", 4, "invalid output after repair")
    got = json.loads(next(store.outputs.iterdir()).read_text())
    assert got == {"reason": "invalid output after repair", "status": "failed"}


def test_missing_run_dir_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NiveshError, match="does not exist"):
        RunStore(tmp_path / "nope", Tracer(tmp_path, 1))
