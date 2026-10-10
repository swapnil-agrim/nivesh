"""Versioned prompt loader (PID 14.6): `prompts/<agent>/vN.md` with a front matter block naming
`agent`, `version`, `schema` and `tools`. The newest version wins unless `prompt_pins` names one.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nivesh_core.errors import NiveshError

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
_FILE = re.compile(r"^v([1-9][0-9]*)\.md$")
_AGENT = re.compile(r"^[a-z][a-z_]*$")


class PromptError(NiveshError):
    pass


@dataclass(frozen=True)
class Prompt:
    agent: str
    version: int
    schema: str
    tools: tuple[str, ...]
    text: str  # body after the front matter; its first line is the `[agent:<name>]` marker
    digest: str  # SHA-256 of the whole file


def versions(agent: str, root: Path = PROMPT_DIR) -> list[int]:
    if not _AGENT.match(agent):
        raise PromptError(f"invalid agent name {agent!r}")
    d = root / agent
    found = (
        [int(m.group(1)) for p in d.glob("*.md") if (m := _FILE.match(p.name))]
        if d.is_dir()
        else []
    )
    return sorted(found)


def load_prompt(
    agent: str, *, pins: dict[str, int] | None = None, root: Path = PROMPT_DIR
) -> Prompt:
    have = versions(agent, root)
    if not have:
        raise PromptError(f"no prompt for agent {agent!r}")
    want = (pins or {}).get(agent, have[-1])
    if want not in have:
        raise PromptError(f"agent {agent!r} has no prompt version {want}")
    raw = (root / agent / f"v{want}.md").read_text()
    if not raw.startswith("---\n") or raw.count("\n---\n") < 1:
        raise PromptError(f"{agent} v{want}: missing front matter")
    head, body = raw[4:].split("\n---\n", 1)
    meta: Any = yaml.safe_load(head)
    if (
        not isinstance(meta, dict)
        or meta.get("agent") != agent
        or meta.get("version") != want
        or not isinstance(meta.get("schema"), str)
        or not isinstance(meta.get("tools"), list)
    ):
        raise PromptError(f"{agent} v{want}: front matter must agree with the file name")
    return Prompt(
        agent=agent,
        version=want,
        schema=meta["schema"],
        tools=tuple(str(t) for t in meta["tools"]),
        text=body.strip() + "\n",
        digest=hashlib.sha256(raw.encode()).hexdigest(),
    )
