import ipaddress
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class Price(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(extra="forbid")
    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)


class BackupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str | None = None  # directory for encrypted archives
    recipient: str | None = None  # age public key (not a secret)
    retention_days: int = Field(default=30, gt=0)


class InvestRightSettings(BaseModel):
    """HDFC InvestRight API app. The base URL is per spec, unverified against the live API."""

    model_config = ConfigDict(extra="forbid")
    base_url: str = "https://developer.hdfcsec.com"
    redirect_port: int = Field(default=8765, ge=1024, le=65535)
    app_key: str = "ref:INVESTRIGHT_API_KEY"
    api_secret: str = "ref:INVESTRIGHT_API_SECRET"  # noqa: S105 - a reference, not a value
    user_agent: str = "nivesh/0.1"
    demat_ref: str | None = None  # holder_ref (from `nivesh ingest`) of the InvestRight demat

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("must start with https://")
        return v.rstrip("/")

    @field_validator("app_key", "api_secret")
    @classmethod
    def _ref(cls, v: str) -> str:
        if not REF_RE.match(v):
            raise ValueError("must be a reference such as 'ref:NAME'")
        return v


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["dev", "prod"] = "dev"
    data_dir: str = "data"
    anthropic_api_key: str | None = None  # a "ref:NAME" reference, never a value
    ttls: Ttls = Ttls()
    prices: dict[str, Price] = {}
    usd_inr: float = Field(default=90.0, gt=0)
    registered_ip: str | None = None
    egress_url: str = "https://api.ipify.org"
    backup: BackupSettings = BackupSettings()
    investright: InvestRightSettings = InvestRightSettings()

    @field_validator("registered_ip")
    @classmethod
    def _ip(cls, v: str | None) -> str | None:
        if v is not None:
            ipaddress.ip_address(v)
        return v


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
