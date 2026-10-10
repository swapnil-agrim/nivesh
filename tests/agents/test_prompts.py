import re
from pathlib import Path

import pytest

from nivesh_agents.prompts import PROMPT_DIR, PromptError, load_prompt, versions
from nivesh_core.pii_scan import scan_text

AGENTS = [
    "fundamental", "technical", "news", "macro", "mf", "bull", "bear", "lens_value",
    "lens_growth", "lens_contrarian", "lens_valuation", "risk", "pm", "thesis_draft",
    "holding_review",
]  # fmt: skip
WRITE_VERBS = re.compile(
    r"\b(place[sd]?|orders?|ordered|modif(y|ied)|cancel|transfer(red|s)?|withdraw|delete[sd]?)\b"
)


def make(root: Path, agent: str, n: int, *, head_version: int | None = None) -> None:
    d = root / agent
    d.mkdir(parents=True, exist_ok=True)
    v = head_version or n
    (d / f"v{n}.md").write_text(
        f"---\nagent: {agent}\nversion: {v}\nschema: AnalystView\ntools: [mcp__a__b]\n---\n"
        f"[agent:{agent}]\nbody {n}\n"
    )


def test_every_agent_has_a_v1_prompt() -> None:
    for a in AGENTS:
        assert 1 in versions(a), a
        assert (PROMPT_DIR / a / "v1.md").is_file()


def test_front_matter_has_agent_version_schema_tools_and_agrees_with_the_filename() -> None:
    for a in AGENTS:
        p = load_prompt(a, pins={a: 1})
        assert (p.agent, p.version) == (a, 1) and p.schema
        assert isinstance(p.tools, tuple)
        assert p.text.splitlines()[0] == f"[agent:{a}]"


def test_newest_version_is_resolved_by_default(tmp_path: Path) -> None:
    for n in (1, 2, 10):
        make(tmp_path, "news", n)
    assert load_prompt("news", root=tmp_path).version == 10


def test_pin_overrides_newest(tmp_path: Path) -> None:
    make(tmp_path, "news", 1)
    make(tmp_path, "news", 2)
    assert load_prompt("news", pins={"news": 1}, root=tmp_path).version == 1
    assert load_prompt("news", pins={"pm": 1}, root=tmp_path).version == 2


def test_bad_filename_is_ignored_and_pin_to_missing_version_raises(tmp_path: Path) -> None:
    make(tmp_path, "news", 1)
    for junk in ("v0.md", "v01.md", "vx.md", "v2.txt", "notes.md"):
        (tmp_path / "news" / junk).write_text("---\n---\n")
    assert versions("news", tmp_path) == [1]
    with pytest.raises(PromptError, match="version 3"):
        load_prompt("news", pins={"news": 3}, root=tmp_path)
    with pytest.raises(PromptError):
        load_prompt("nobody", root=tmp_path)
    with pytest.raises(PromptError):
        load_prompt("../etc", root=tmp_path)


def test_front_matter_mismatch_or_missing_is_an_error(tmp_path: Path) -> None:
    make(tmp_path, "news", 1, head_version=2)
    with pytest.raises(PromptError, match="front matter"):
        load_prompt("news", root=tmp_path)
    (tmp_path / "pm").mkdir()
    (tmp_path / "pm" / "v1.md").write_text("no front matter\n")
    with pytest.raises(PromptError, match="front matter"):
        load_prompt("pm", root=tmp_path)


def test_digest_changes_with_text_and_is_stable_otherwise(tmp_path: Path) -> None:
    make(tmp_path, "news", 1)
    a = load_prompt("news", root=tmp_path).digest
    assert a == load_prompt("news", root=tmp_path).digest and len(a) == 64
    with (tmp_path / "news" / "v1.md").open("a") as f:
        f.write("more\n")
    assert load_prompt("news", root=tmp_path).digest != a


def test_prompt_tools_equal_the_spec_allow_list() -> None:
    specs = pytest.importorskip("nivesh_agents.specs", reason="specs arrive in step P3")
    SPECS = specs.SPECS

    for name, spec in SPECS.items():
        assert load_prompt(name).tools == spec.tools, name


def test_prompts_contain_the_required_guardrail_sentences() -> None:
    needles = [
        r"`as_of` date", r"training memory", r"insufficient_data", r"leverage",
        r"<untrusted-data>", r"only data returned by your tools or given in the input",
        r"exactly one JSON object",
    ]  # fmt: skip
    for a in AGENTS:
        text = load_prompt(a).text
        for n in needles:
            assert re.search(n, text), (a, n)


def test_verdict_prompt_requires_invalidation_and_review_date() -> None:
    t = load_prompt("pm").text
    assert "invalidation" in t and "review_date" in t and "INSUFFICIENT_DATA" in t


def test_prompts_have_no_lowercase_write_verbs() -> None:
    for p in PROMPT_DIR.glob("*/v*.md"):
        assert not WRITE_VERBS.search(p.read_text()), p


def test_prompts_have_no_credential_looking_literals() -> None:
    for p in PROMPT_DIR.glob("*/v*.md"):
        assert scan_text(p.read_text()) == [], p


def test_holding_review_prompt_says_code_decides_triggers_and_actions_can_only_be_lowered() -> None:
    t = load_prompt("holding_review").text
    for needle in ("met, not_met or unknown", "evidence", "Code computes", "only lower",
                   "HOLD, ADD, TRIM, EXIT or REVIEW", "override_reason", "triggers"):  # fmt: skip
        assert needle in t, needle


def test_thesis_draft_prompt_requires_two_to_four_measurable_criteria_and_60_words() -> None:
    t = load_prompt("thesis_draft").text
    for needle in ("60 words", "2 to 4", "metric", "comparator", "threshold", "point_index",
                   "no tools"):  # fmt: skip
        assert needle in t, needle
    assert not re.search(r"(?i)\borders?\b", t)
    assert not re.search(r"(?i)\borders?\b", load_prompt("holding_review").text)
