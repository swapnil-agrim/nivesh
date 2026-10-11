import ipaddress
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nivesh_core.agents_config import AgentsSettings
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.delivery_config import DeliverySettings
from nivesh_core.errors import ConfigError
from nivesh_core.report_config import ReportSettings
from nivesh_core.review_config import ReviewSettings
from nivesh_core.secrets import REF_RE
from nivesh_core.universe_config import IdeasSettings, UniverseSettings
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
    news: timedelta = timedelta(hours=6)
    macro: timedelta = timedelta(days=1)
    filings: timedelta = timedelta(days=7)
    estimates: timedelta = timedelta(days=1)
    master: timedelta = timedelta(days=7)

    _parse = field_validator(
        "price", "fundamentals", "nav", "mf_holdings", "news", "macro", "filings", "estimates",
        "master", mode="before",
    )(_ttl)  # fmt: skip


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


MacroRole = Literal[
    "policy_us", "policy_in", "y10_us", "y10_in", "cpi_us", "cpi_in", "usdinr", "vix_us", "crude",
    "fii_net", "dii_net",
]  # fmt: skip


class MacroSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["fred", "nse_flows"]
    id: str


class FeedSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    kind: Literal["rss", "bse_announcements", "nse_announcements"] = "rss"

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("must start with https://")
        return v


class MarketSettings(BaseModel):
    """E4 market data. Wire details and series ids are per spec, unverified against live sources."""

    model_config = ConfigDict(extra="forbid")
    edgar_max_per_sec: float = Field(default=8, ge=1, le=10)  # SEC fair-access limit is 10
    fred_api_key: str = "ref:FRED_API_KEY"
    fmp_api_key: str = "ref:FMP_API_KEY"
    us_secondary: Literal["stooq", "none"] = "stooq"
    cross_check_tolerance: Decimal = Decimal("0.01")
    macro_series: dict[MacroRole, MacroSpec] = {}
    feeds: list[FeedSpec] = []
    nse_holidays: dict[int, list[date]] = {}

    @field_validator("cross_check_tolerance", mode="before")
    @classmethod
    def _tol(cls, v: Any) -> Decimal:
        d = Decimal(str(v))  # YAML float 0.01 must become exactly 0.01
        if not (0 < d <= Decimal("0.5")):
            raise ValueError("must be > 0 and <= 0.5")
        return d

    @field_validator("fred_api_key", "fmp_api_key")
    @classmethod
    def _ref(cls, v: str) -> str:
        if not REF_RE.match(v):
            raise ValueError("must be a reference such as 'ref:NAME'")
        return v

    @field_validator("nse_holidays")
    @classmethod
    def _years(cls, v: dict[int, list[date]]) -> dict[int, list[date]]:
        for year, days in v.items():
            if any(d.year != year for d in days):
                raise ValueError(f"holiday dates under {year} must all fall in {year}")
        return v


class UsBrokerSettings(BaseModel):
    """Read-only US broker connector (Alpaca positions/account). The base URL is per spec,
    unverified against the live API; credentials are references, never values."""

    model_config = ConfigDict(extra="forbid")
    base_url: str = "https://paper-api.alpaca.markets"
    alpaca_key: str = "ref:ALPACA_KEY"
    alpaca_secret: str = "ref:ALPACA_SECRET"  # noqa: S105 - a reference, not a value

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("must start with https://")
        return v.rstrip("/")

    @field_validator("alpaca_key", "alpaca_secret")
    @classmethod
    def _ref(cls, v: str) -> str:
        if not REF_RE.match(v):
            raise ValueError("must be a reference such as 'ref:NAME'")
        return v


class IndiaTax(BaseModel):
    """Owner-set India equity/ETF tax inputs; unset means gain and days only. No tax advice."""

    model_config = ConfigDict(extra="forbid")
    long_term_days: int | None = Field(default=None, gt=0)
    short_rate_pct: Decimal | None = Field(default=None, ge=0, le=100)
    long_rate_pct: Decimal | None = Field(default=None, ge=0, le=100)
    ltcg_exemption_inr: Decimal | None = Field(default=None, ge=0)


class TaxSettings(BaseModel):
    """Owner-set parameters. Nivesh only compares days against them; it gives no tax advice."""

    model_config = ConfigDict(extra="forbid")
    us_long_term_days: int | None = Field(default=None, gt=0)
    india: IndiaTax = IndiaTax()


class MfSources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nav_primary: Literal["mfapi", "amfi"] = "mfapi"
    nav_fallback: Literal["mfapi", "amfi", "none"] = "amfi"


class MfConsistency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_days: list[int] = [1095, 1826]  # 3y and 5y rolling windows
    min_beat_pct: Decimal = Decimal("50")
    min_median_excess_pct: Decimal = Decimal("0")

    @field_validator("window_days")
    @classmethod
    def _windows(cls, v: list[int]) -> list[int]:
        if not v or any(w <= 0 for w in v):
            raise ValueError("must be a non-empty list of positive day counts")
        return v


class MfThresholds(BaseModel):
    """Fund-doctor thresholds. Owner-set; no rule hard-codes a number."""

    model_config = ConfigDict(extra="forbid")
    ter_excess_pct: Decimal = Decimal("0")  # minimum regular-vs-direct TER gap to act on
    overlap_pct: Decimal = Decimal("60")
    downside_capture_max: Decimal = Decimal("100")  # percent of the benchmark's down-day move
    max_drawdown_pct: Decimal = Decimal("35")
    min_tenure_years: Decimal = Decimal("2")
    valuation_stretch_ratio: Decimal = Decimal("1.25")  # current multiple / own median


class ExitLoad(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percent: Decimal = Field(ge=0, le=100)
    days: int = Field(gt=0)


class MfTax(BaseModel):
    """Owner-set tax parameters; unset means gain and days only. No tax advice."""

    model_config = ConfigDict(extra="forbid")
    long_term_days: int | None = Field(default=None, gt=0)
    short_rate_pct: Decimal | None = Field(default=None, ge=0, le=100)
    long_rate_pct: Decimal | None = Field(default=None, ge=0, le=100)


class MfScreenWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consistency: Decimal = Field(default=Decimal("1"), ge=0)
    downside: Decimal = Field(default=Decimal("1"), ge=0)
    cost: Decimal = Field(default=Decimal("1"), ge=0)
    valuation: Decimal = Field(default=Decimal("1"), ge=0)


class MfScreen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: MfScreenWeights = MfScreenWeights()
    universe: list[str] = []  # extra AMFI codes to screen beyond the stored schemes


class MfSettings(BaseModel):
    """E5 mutual funds. All numbers are owner-set parameters. Source wire formats are per spec,
    unverified against live sources. The holdings source credential is a reference only."""

    model_config = ConfigDict(extra="forbid")
    sources: MfSources = MfSources()
    holdings_source: Literal["mfdata", "amc", "fixture"] = "mfdata"
    holdings_api_ref: str = "ref:MFDATA_API_KEY"
    nav_gap_days: int = Field(default=5, gt=0)
    nav_tolerance: Decimal = Decimal("0.005")
    min_alignment_pct: Decimal = Field(default=Decimal("80"), gt=0, le=100)
    min_valuation_coverage_pct: Decimal = Field(default=Decimal("60"), gt=0, le=100)
    mar_pct: Decimal = Decimal("0")  # Sortino minimum acceptable return, annual percent
    max_nav_age_days: int = Field(default=10, gt=0)  # older latest NAV: value and gain unavailable
    consistency: MfConsistency = MfConsistency()
    thresholds: MfThresholds = MfThresholds()
    benchmarks: dict[str, str] = {}  # fund_meta.benchmark text -> index security symbol
    exit_load: dict[str, ExitLoad] = {}
    tax: MfTax = MfTax()
    screen: MfScreen = MfScreen()

    @field_validator("nav_tolerance", mode="before")
    @classmethod
    def _tol(cls, v: Any) -> Decimal:
        d = Decimal(str(v))
        if not (0 < d <= Decimal("0.5")):
            raise ValueError("must be > 0 and <= 0.5")
        return d

    @field_validator("holdings_api_ref")
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
    market: MarketSettings = MarketSettings()
    tax: TaxSettings = TaxSettings()
    us_broker: UsBrokerSettings = UsBrokerSettings()
    mf: MfSettings = MfSettings()
    analysis: AnalysisSettings = AnalysisSettings()
    agents: AgentsSettings = AgentsSettings()
    review: ReviewSettings = ReviewSettings()
    universe: UniverseSettings = UniverseSettings()
    ideas: IdeasSettings = IdeasSettings()
    report: ReportSettings = ReportSettings()
    delivery: DeliverySettings = DeliverySettings()

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
