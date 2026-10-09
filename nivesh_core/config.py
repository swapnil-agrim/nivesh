import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from nivesh_core.errors import ConfigError
from nivesh_core.secrets import REF_RE
from nivesh_core.yamlio import read_mapping, validate

_SECRETISH = re.compile(r"(_key|_token|_secret|password)$", re.IGNORECASE)
_TTL_RE = re.compile(r"^(\d+)([dh])$")


def _ttl(v: Any) -> timedelta:
    if isinstance(v, timedelta):
        return v
    m = _TTL_RE.match(str(v))
    if not m:
        raise ValueError("TTL must look like '7d' or '12h'")
    n = int(m.group(1))
    return timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n)


class Ttls(BaseModel):
    """Cache TTLs by data type; defaults per NFR-7."""

    model_config = ConfigDict(extra="forbid")
    price: timedelta = timedelta(days=1)
    fundamentals: timedelta = timedelta(days=7)
    nav: timedelta = timedelta(days=1)
    mf_holdings: timedelta = timedelta(days=31)

    _parse = field_validator("price", "fundamentals", "nav", "mf_holdings", mode="before")(_ttl)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["dev", "prod"] = "dev"
    data_dir: str = "data"
    anthropic_api_key: str | None = None  # a "ref:NAME" reference, never a value
    ttls: Ttls = Ttls()


def _find_literal_secrets(node: Any, trail: str = "") -> list[str]:
    bad: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{trail}.{k}" if trail else str(k)
            if _SECRETISH.search(str(k)) and not (isinstance(v, str) and REF_RE.match(v)):
                bad.append(path)
            else:
                bad += _find_literal_secrets(v, path)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            bad += _find_literal_secrets(v, f"{trail}[{i}]")
    return bad


def load_settings(path: Path) -> Settings:
    data = read_mapping(path)
    bad = _find_literal_secrets(data)
    if bad:  # never echo the offending value
        raise ConfigError(
            f"{path}: secret-like field(s) must be a reference such as 'ref:ANTHROPIC_API_KEY', "
            f"not a value: {', '.join(bad)}"
        )
    return validate(Settings, data, path)
