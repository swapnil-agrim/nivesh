"""The fundamentals provider seam (ADR-0016 D1): what the ingest needs from a vendor, so a new
statements, shareholding or actions source is one module plus one `BUILTIN` line.

A provider turns the adapter of its vendor into rows: `collect(ctx)` fetches through `ctx.get`
(the run's cached fetch, which records a failure and gives None), maps with pure functions and
returns a `ProviderResult`. The ingest decides roles, quota budgets and what is written; a
provider never writes a store. This module must not import `market_ingest` (the ingest imports
it): a provider receives the run's callables instead of the run.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from importlib import import_module
from typing import Any, ClassVar, Literal

from nivesh_core.errors import ConfigError
from nivesh_core.fundamentals_config import FundamentalsSettings, ProviderSpec
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.quota import Budget
from nivesh_core.secrets import REF_RE
from nivesh_core.security_master import SecurityMaster, SecurityRow
from nivesh_engine.statements import StatementRow

# what a mapper can raise on a hostile number or date (InvalidOperation is an ArithmeticError)
MAPPING_ERRORS = (ArithmeticError, ValueError, TypeError)


class Capability(StrEnum):
    statements = "statements"
    shareholding = "shareholding"
    pledge = "pledge"
    actions = "actions"
    estimates = "estimates"
    bank_metrics = "bank_metrics"
    news = "news"


class Ident(StrEnum):
    """What a vendor identifies a company by (the master carries all three)."""

    nse_symbol = "nse_symbol"
    bse_code = "bse_code"
    isin = "isin"


@dataclass(frozen=True)
class ProviderInfo:
    """What a provider declares about itself: shown by `fundamentals compare`, used to rank stored
    sources, and checked against what its harness produces by the contract suite."""

    name: str  # the registry and config name
    label: str  # for people
    sources: tuple[str, ...]  # the `source` values it writes, most trusted first
    capabilities: frozenset[Capability]
    keyed_on: Ident
    credential: str | None  # default secret name; the spec's `ref:NAME` overrides it
    metered: bool  # has a vendor quota, so the ingest requires a plan and a ledger budget
    revenue_basis: str
    profit_basis: str
    filing_dates: Literal["declared", "estimated", "mixed"]


@dataclass
class CollectContext:
    sec: SecurityRow
    master: SecurityMaster
    today: date
    get: Callable[..., Any]  # `Run.get(adapter, params, data_type, label, soft=)`
    failed: list[str]  # the run's shared failure lines: append reasons, never raise for a source
    budget: Budget | None  # the quota gate of a spec with a plan


@dataclass
class ProviderResult:
    groups: list[tuple[str, list[StatementRow]]] = field(default_factory=list)
    supersede: str | None = None  # a source whose rows for the first group's figures go
    shareholding: list[ShareholdingRow] = field(default_factory=list)
    shareholding_source: str | None = None  # defaults to the first group's source
    notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    clamped: int = 0  # filing dates moved back to today
    estimated: int = 0  # rows whose filing date is a guess
    calls: int | None = None  # billed calls of a metered provider

    @property
    def statements(self) -> bool:
        return any(rows for _, rows in self.groups)


class FundamentalsProvider(ABC):
    info: ClassVar[ProviderInfo]

    def __init__(self, spec: ProviderSpec, make: Callable[[], Any] | None = None) -> None:
        self.spec = spec
        self._make = make  # the adapter factory a test or the CLI injects; None: the default

    @property
    def credential_name(self) -> str | None:
        """The secret to read: the spec's `ref:NAME`, else the provider's default."""
        ref = REF_RE.match(self.spec.credential or "")
        return ref.group(1) if ref else self.info.credential

    @abstractmethod
    def collect(self, ctx: CollectContext) -> ProviderResult:
        """Fetch and map one security. Source failures go to `ctx.failed` and give a partial or
        empty result; `NotConfigured` (or `ConfigError`) says the owner must set something up."""


REGISTRY: dict[str, type[FundamentalsProvider]] = {}  # added at run time (a test registers a fake)
BUILTIN: dict[str, str] = {  # name -> "module:Class", imported on first use
    "tapetide": "nivesh_adapters.fundamentals_tapetide:TapetideProvider",
    "bse_xbrl": "nivesh_adapters.fundamentals_in:BseProvider",
    "upstox": "nivesh_adapters.fundamentals_upstox:UpstoxProvider",
}


def provider_class(name: str) -> type[FundamentalsProvider]:
    if name in REGISTRY:
        return REGISTRY[name]
    path = BUILTIN.get(name)
    if path is None:
        known = ", ".join(sorted(REGISTRY.keys() | BUILTIN.keys()))
        raise ConfigError(f"unknown fundamentals provider {name!r}; registered: {known}")
    module, _, cls = path.partition(":")
    found: type[FundamentalsProvider] = getattr(import_module(module), cls)
    return found


def _known(name: str) -> bool:
    return name in REGISTRY or name in BUILTIN


def configured(fundamentals: FundamentalsSettings) -> list[tuple[ProviderSpec, ProviderInfo]]:
    """The enabled providers in config order, fallbacks last. An enabled name nobody registered is
    a `ConfigError` (a disabled one is never looked up)."""
    enabled = [(s, provider_class(s.name).info) for s in fundamentals.providers if s.enabled]
    return sorted(enabled, key=lambda p: p[0].role == "fallback")  # stable: config order inside


def provider_infos(fundamentals: FundamentalsSettings) -> list[ProviderInfo]:
    """The declarations of every provider whose stored rows stay readable, most trusted first:
    the enabled ones in order, then the disabled ones (rows an earlier configuration wrote). A
    disabled name nobody registered contributes nothing."""
    infos = [i for _, i in configured(fundamentals)]
    off = [s.name for s in fundamentals.providers if not s.enabled and _known(s.name)]
    return infos + [provider_class(name).info for name in off]


def provider_sources(fundamentals: FundamentalsSettings) -> tuple[str, ...]:
    """Stored source names, most trusted first (the order of `provider_infos`)."""
    return tuple(
        dict.fromkeys(src for info in provider_infos(fundamentals) for src in info.sources)
    )


def revenue_basis_of(fundamentals: FundamentalsSettings, market: str) -> str | None:
    """How the first enabled statements provider defines revenue, for India (US: not declared)."""
    if market != "IN":
        return None
    infos = [i for _, i in configured(fundamentals) if Capability.statements in i.capabilities]
    return infos[0].revenue_basis if infos else None


def dedupe_tail(lines: list[str], start: int) -> None:
    """Collapse failures added since `start` that carry the same reason (a halted source repeats
    its reason for every remaining call)."""
    seen: set[str] = set()
    kept = []
    for line in lines[start:]:
        reason = line.split(": ", 1)[-1]
        if reason not in seen:
            seen.add(reason)
            kept.append(line)
    lines[start:] = kept
