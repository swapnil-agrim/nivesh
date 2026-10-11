import json
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.report import FILES, Num, save_report
from nivesh_core.paths import run_dir
from nivesh_core.pii_scan import scan_paths
from nivesh_core.replay import replay_run
from nivesh_core.trace import Tracer, read_trace
from tests.report_fx import base_report

FACTS = {"close": Decimal("101.50"), "net_debt": "1.20 crore"}


def saved(tmp_path: Path) -> tuple[Path, Tracer]:
    d = run_dir(tmp_path, 7)
    return d, Tracer(d, 7)


def test_save_report_writes_report_md_html_and_report_input_json_0600(tmp_path: Path) -> None:
    d, tr = saved(tmp_path)
    paths = save_report(d, base_report(), FACTS, tr)
    assert [p.name for p in paths] == list(FILES)
    for p in paths:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads((d / "report_input.json").read_text())["close"] == "101.50"
    assert (d / "report.md").read_text().startswith("# Example Energy research note")
    assert (d / "report.html").read_text().startswith("<!DOCTYPE html>")


def test_save_report_refuses_to_overwrite(tmp_path: Path) -> None:
    d, _ = saved(tmp_path)
    save_report(d, base_report(), {})
    with pytest.raises(FileExistsError):
        save_report(d, base_report(), {})


def test_trace_gets_report_saved_records_with_digest_not_content(tmp_path: Path) -> None:
    d, tr = saved(tmp_path)
    save_report(d, base_report(), FACTS, tr)
    recs = [r for r in read_trace(d / "trace.jsonl") if r["type"] == "report_saved"]
    assert [r["name"] for r in recs] == list(FILES)
    text = (d / "trace.jsonl").read_text()
    assert "Steady cash flow" not in text and all("-" in r["digest"] for r in recs)


def test_saved_files_pass_scan_paths_clean(tmp_path: Path) -> None:
    d, tr = saved(tmp_path)
    big = Num.inr(Decimal("123456789012"))  # 12-digit raw amount, stored as 12,345.68 crore
    report = base_report(nums=(big,), summary=(f"Market cap {big.text}",))
    save_report(d, report, {"cap_crore": str(big.value), "close": Decimal("101.50")}, tr)
    assert scan_paths([d]) == [] and "12,345.68 crore" in (d / "report.md").read_text()


def test_report_input_json_has_no_engine_call_record_in_trace(tmp_path: Path) -> None:
    d, tr = saved(tmp_path)
    save_report(d, base_report(), FACTS, tr)
    assert all(r["type"] != "engine_call" for r in read_trace(d / "trace.jsonl"))


def test_replay_of_a_run_with_saved_report_still_ok(tmp_path: Path) -> None:
    d, tr = saved(tmp_path)
    save_report(d, base_report(), FACTS, tr)
    res = replay_run(d)
    assert res.ok and res.steps == 0
