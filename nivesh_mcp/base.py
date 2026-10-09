"""Read-only MCP server framework (NFR-1). Built on FastMCP 4.x (`from fastmcp import FastMCP`).

Every tool must be a sync, fully type-hinted, documented function whose name has no write verb
and which returns an `AdapterResult` or a dict carrying `as_of` and `source`.
"""

import functools
import inspect
import re
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from nivesh_adapters.base import AdapterResult
from nivesh_core.errors import NiveshError

WRITE_VERBS = ("place", "modify", "cancel", "transfer", "order", "buy", "sell", "delete")
_SUFFIXES = {
    "",
    "s",
    "es",
    "d",
    "ed",
    "led",
    "ing",
    "ling",
    "er",
    "ers",
    "red",
    "ring",
    "ned",
    "ning",
    "ion",
    "ions",
    "ation",
    "lation",
    "ment",
    "ments",
    "ied",
    "ication",
    "ifying",
    "ying",
}  # inflections: placing, transferred, deletion, cancellation, modified, placement


class RegistrationError(NiveshError):
    pass


def is_write_name(name: str) -> bool:
    """Word-part match: `PlaceOrder`, `sellAll` are writes; `reorder_levels` is not."""
    parts = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower().replace("-", "_").split("_")
    stems = {v: (v[:-1] if v[-1] in "ey" else v) for v in WRITE_VERBS}
    return any(
        p[len(v) :] in _SUFFIXES or p[len(stems[v]) :] in _SUFFIXES
        for p in parts
        for v in WRITE_VERBS
        if p.startswith(stems[v])
    )


def _envelope(result: Any, tool: str) -> dict[str, Any]:
    if isinstance(result, AdapterResult):
        return {
            "data": result.data,
            "source": result.source,
            "as_of": result.as_of.isoformat(),
            "stale": result.stale,
        }
    if isinstance(result, dict) and "as_of" in result and "source" in result:
        return result
    raise RegistrationError(
        f"tool {tool!r} must return an AdapterResult or a dict with 'as_of' and 'source'"
    )


class ReadOnlyServer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.mcp = FastMCP(name)
        self.tool_names: list[str] = []

    def tool(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        name = fn.__name__
        if is_write_name(name):
            raise RegistrationError(f"tool {name!r} contains a write verb; servers are read-only")
        if inspect.iscoroutinefunction(fn):
            raise RegistrationError(f"tool {name!r} must be a sync function")
        if not (fn.__doc__ and fn.__doc__.strip()):
            raise RegistrationError(f"tool {name!r} needs a docstring")
        sig = inspect.signature(fn)
        missing = [
            p.name for p in sig.parameters.values() if p.annotation is inspect.Parameter.empty
        ]
        if missing or sig.return_annotation is inspect.Signature.empty:
            raise RegistrationError(
                f"tool {name!r} needs type hints on params {missing} and return"
            )
        if name in self.tool_names:
            raise RegistrationError(f"duplicate tool {name!r} on server {self.name!r}")

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return _envelope(fn(*args, **kwargs), name)

        wrapper.__signature__ = sig.replace(return_annotation=dict[str, Any])  # type: ignore[attr-defined]
        wrapper.__annotations__ = {**fn.__annotations__, "return": dict[str, Any]}
        self.mcp.tool(wrapper)
        self.tool_names.append(name)
        return fn
