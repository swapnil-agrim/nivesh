"""Model output is untrusted data. This module is the boundary: it extracts JSON, validates it
against the schema, and checks that every cited tool-use id is one the agent really made.

Nothing here evaluates, executes or path-joins model text. Error text that goes back to the model
or into the trace is redacted and truncated.
"""

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from nivesh_core.redact import redact_text

ERROR_LIMIT = 2000
REPAIR_RETRIES = 1  # exactly one repair call; a constant, not configuration


@dataclass(frozen=True)
class Valid:
    model: BaseModel


@dataclass(frozen=True)
class Invalid:
    errors: list[str]


def extract_json(structured: Any, text: str | None) -> str | None:
    """The SDK's structured output if it is an object, else the last top-level JSON object in
    the text (prose around it is ignored). None when there is no object at all."""
    if isinstance(structured, dict):
        return json.dumps(structured)
    if not text:
        return None
    dec, idx, last = json.JSONDecoder(), 0, None
    while (start := text.find("{", idx)) != -1:
        try:
            obj, end = dec.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            last = text[start:end]
        idx = end
    return last


def clean_errors(errors: list[str]) -> list[str]:
    """Redact and cap the total length at ERROR_LIMIT characters."""
    out, used = [], 0
    for e in errors:
        e = redact_text(e)
        if used + len(e) > ERROR_LIMIT:
            out.append(e[: max(ERROR_LIMIT - used, 0)])
            break
        out.append(e)
        used += len(e)
    return out


def parse(model_cls: type[BaseModel], raw: str | None) -> Valid | Invalid:
    if raw is None:
        return Invalid(clean_errors(["output is not a JSON object"]))
    try:
        return Valid(model_cls.model_validate_json(raw))
    except ValidationError as e:
        return Invalid(
            clean_errors(
                [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
            )
        )


def cited_ids(model: BaseModel) -> set[str]:
    """Every `tool_call_id` anywhere in the validated output."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "tool_call_id" and isinstance(v, str):
                    found.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(model.model_dump(mode="json"))
    return found


def check_evidence(model: BaseModel, valid_ids: frozenset[str]) -> list[str]:
    return [
        f"evidence tool_call_id {i!r} is not a tool call made for this task"
        for i in sorted(cited_ids(model) - valid_ids)
    ]


def repair_prompt(errors: list[str], valid_ids: frozenset[str], first_output: str | None) -> str:
    parts = ["Your previous answer was rejected. Fix every problem listed and answer again."]
    parts += [f"- {e}" for e in errors]
    ids = ", ".join(sorted(valid_ids)) or "(none: you made no tool calls)"
    parts.append(f"Valid tool_call_id values: {ids}")
    if first_output is not None:
        parts.append("Your previous answer was:\n" + redact_text(first_output)[:ERROR_LIMIT])
    parts.append("Return JSON only: exactly one object, no prose.")
    return "\n".join(parts)
