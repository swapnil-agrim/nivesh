"""No personal data in anything the committee writes."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from nivesh_agents import runtime
from nivesh_agents.committee import run_committee
from nivesh_agents.prompts import PROMPT_DIR
from nivesh_agents.schemas import SCHEMA_DIR
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.config import Settings
from nivesh_core.pii_scan import scan_paths, scan_text
from nivesh_core.trace import Tracer
from tests import pii_values as pv
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import reply

NAME = re.compile(
    r"^(snapshot\.json|trace\.jsonl|outputs|\d{3}_(fundamental|technical|news|macro|mf|debate|"
    r"lenses|risk|verdict)_(\d+|all)\.json)$"
)


async def run_it(rd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def news(call: Any) -> Any:
        body = json.loads(call.prompt)["security"]["security_id"]
        hostile = f"contact {pv.email()} or call {pv.phone()} about {pv.pan()}"
        v = fx.analyst("news", security_id=body, key_points=[fx.point(1)])
        return reply(v, tools=fx.calls("mcp__news__get_news", 1, content=hostile))

    fake = fx.happy(news=news)
    monkeypatch.setattr(runtime, "query", fake)
    await run_committee(
        fx.committee_inputs(fx.sec_input(1), fx.sec_input(2), fx.fund_input(5)), tier="deep",
        cfg=AgentsSettings(), settings=Settings(), tracer=Tracer(rd, 1), run_dir=rd,
    )  # fmt: skip


async def test_committee_run_over_synthetic_holdings_leaves_no_pii_in_run_dir_or_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    await run_it(rd, monkeypatch)
    files = [p for p in rd.rglob("*") if p.is_file()]
    assert len(files) > 10 and (rd / "trace.jsonl").is_file()
    assert scan_paths([rd]) == []
    trace = (rd / "trace.jsonl").read_text()
    assert pv.email() not in trace and pv.pan() not in trace and pv.phone() not in trace


def test_agent_fixtures_and_prompts_have_no_key_shaped_literals() -> None:
    root = Path(__file__).resolve().parents[1]
    files = [
        *PROMPT_DIR.glob("*/v*.md"), *SCHEMA_DIR.glob("*.json"),
        root / "agents" / "committee_fx.py", root / "agents" / "fake_sdk.py",
        root / "eval" / "golden_fa.yaml",
    ]  # fmt: skip
    assert len(files) >= 23
    for f in files:
        assert scan_text(f.read_text()) == [], f


async def test_output_files_use_only_security_ids_and_symbols_never_holder_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    await run_it(rd, monkeypatch)
    for p in rd.rglob("*"):
        assert NAME.match(p.name), p.name
    snap = (rd / "snapshot.json").read_text()
    assert (
        "holder_ref" not in snap and "folio" not in snap.lower() and "account" not in snap.lower()
    )
    assert '"symbol": "S001"' in snap
