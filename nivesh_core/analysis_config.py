"""Owner-set parameters of the analysis engines (ST-6.1 to ST-6.9), one nested `analysis:` block.

Every number the engines use is a parameter here, with a documented default; none is a rule hard
coded in an engine, and none is advice. Models forbid unknown keys, numbers are Decimal (never
binary floating point), and the factor weights carry an owner label (`weights_version`) plus a
computed digest, so a changed weight is visible in every score that used it.
"""

import hashlib
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

D = Decimal


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _positive(values: list[int], what: str) -> list[int]:
    if not values or any(v <= 0 for v in values):
        raise ValueError(f"{what} must be a non-empty list of positive integers")
    return values


class TaSettings(_Group):
    """ST-6.1 indicator windows (sessions) and the market and sector benchmark symbols."""

    sma_windows: list[int] = [20, 50, 200]
    ema_window: int = Field(default=21, gt=0)
    rsi_period: int = Field(default=14, gt=0)
    atr_period: int = Field(default=14, gt=0)
    macd_fast: int = Field(default=12, gt=0)
    macd_slow: int = Field(default=26, gt=0)
    macd_signal: int = Field(default=9, gt=0)
    roc_sessions: list[int] = [63, 126, 252]  # about 3, 6 and 12 months
    vol_windows: list[int] = [20, 60]
    bollinger_window: int = Field(default=20, gt=0)
    bollinger_width_k: Decimal = Field(default=D("2"), gt=0)
    slope_lookback: int = Field(default=20, gt=0)
    year_window: int = Field(default=252, gt=0)  # 52-week high and low
    rs_window: int = Field(default=126, gt=0)  # relative-strength change window
    min_bars_long: int = Field(default=200, gt=0)
    benchmarks: dict[str, str] = {}  # market (IN, US) -> index symbol; empty: no relative strength
    sector_index: dict[str, str] = {}  # sector name -> index symbol

    @field_validator("sma_windows", "roc_sessions", "vol_windows")
    @classmethod
    def _windows(cls, v: list[int]) -> list[int]:
        return _positive(v, "windows")

    @model_validator(mode="after")
    def _macd(self) -> "TaSettings":
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be shorter than macd_slow")
        return self


class LevelsSettings(_Group):
    pivot_window: int = Field(default=5, gt=0)
    cluster_tolerance_pct: Decimal = Field(default=D("1.5"), gt=0, le=100)
    max_levels: int = Field(default=3, gt=0)
    gap_min_pct: Decimal = Field(default=D("1"), gt=0, le=100)
    max_gap_zones: int = Field(default=3, ge=0)


class RegimeSettings(_Group):
    risk_on_breadth_pct: Decimal = Field(default=D("60"), ge=0, le=100)
    risk_off_breadth_pct: Decimal = Field(default=D("40"), ge=0, le=100)
    min_breadth_universe: int = Field(default=20, gt=0)

    @model_validator(mode="after")
    def _breadth_ranked(self) -> "RegimeSettings":
        if self.risk_off_breadth_pct > self.risk_on_breadth_pct:
            raise ValueError("risk_off_breadth_pct must not exceed risk_on_breadth_pct")
        return self


SETUP_TYPES = ("breakout", "pullback", "trend_continuation", "base", "downtrend", "none")


class SetupsSettings(_Group):
    pullback_atr_band: Decimal = Field(default=D("1"), gt=0)
    breakout_volume_mult: Decimal = Field(default=D("1.5"), gt=0)
    rsi_trend_min: Decimal = Field(default=D("50"), ge=0, le=100)
    rsi_trend_max: Decimal = Field(default=D("70"), ge=0, le=100)
    base_lookback: int = Field(default=40, gt=0)
    base_atr_pct_max: Decimal = Field(default=D("3"), gt=0, le=100)
    stop_atr_mult: Decimal = Field(default=D("2"), gt=0)
    precedence: list[str] = list(SETUP_TYPES)

    @field_validator("precedence")
    @classmethod
    def _precedence(cls, v: list[str]) -> list[str]:
        if sorted(v) != sorted(SETUP_TYPES):
            raise ValueError(f"precedence must list each of {', '.join(SETUP_TYPES)} once")
        return v

    @model_validator(mode="after")
    def _rsi(self) -> "SetupsSettings":
        if self.rsi_trend_min >= self.rsi_trend_max:
            raise ValueError("rsi_trend_min must be below rsi_trend_max")
        return self


class FaSettings(_Group):
    financial_sectors: list[str] = ["Banks", "Financial Services", "Finance", "NBFC", "Insurance"]
    default_tax_rate_pct: Decimal = Field(default=D("25"), ge=0, le=100)
    growth_years: int = Field(default=3, gt=0)


class DcfScenario(_Group):
    growth_pct: Decimal = Field(ge=-100, le=1000)
    discount_pct: Decimal = Field(gt=0, le=100)
    terminal_growth_pct: Decimal = Field(ge=-100, le=100)

    @model_validator(mode="after")
    def _spread(self) -> "DcfScenario":
        if self.discount_pct <= self.terminal_growth_pct:
            raise ValueError("discount_pct must exceed terminal_growth_pct")
        return self


class DcfSettings(_Group):
    horizon_years: int = Field(default=10, gt=0)
    base: DcfScenario = DcfScenario(
        growth_pct=D("8"), discount_pct=D("12"), terminal_growth_pct=D("3")
    )
    bull: DcfScenario = DcfScenario(
        growth_pct=D("12"), discount_pct=D("11"), terminal_growth_pct=D("4")
    )
    bear: DcfScenario = DcfScenario(
        growth_pct=D("4"), discount_pct=D("14"), terminal_growth_pct=D("2")
    )


class ValuationSettings(_Group):
    history_years: list[int] = [5, 10]
    min_obs: int = Field(default=24, gt=0)
    min_peers: int = Field(default=3, gt=0)
    dcf: DcfSettings = DcfSettings()
    peer_overrides: dict[str, list[str]] = {}  # symbol -> peer symbols, replaces the industry set

    @field_validator("history_years")
    @classmethod
    def _years(cls, v: list[int]) -> list[int]:
        return _positive(v, "history_years")


FlagSeverity = Literal["hard", "soft"]
DEFAULT_SEVERITY: dict[str, FlagSeverity] = {
    "pledge_high_and_rising": "hard",
    "pledge_high": "soft",
    "pledge_rising": "soft",
    "auditor_change": "soft",
    "cfo_to_pat": "hard",
    "receivable_days": "soft",
    "dilution": "soft",
    "contingent_liabilities": "hard",
}


class FlagsSettings(_Group):
    pledge_pct: Decimal = Field(default=D("20"), ge=0, le=100)
    pledge_rising_quarters: int = Field(default=2, gt=0)
    cfo_to_pat_min: Decimal = Field(default=D("0.5"), ge=0)
    cfo_to_pat_years: int = Field(default=3, gt=0)
    receivable_days_rise_pct: Decimal = Field(default=D("30"), ge=0)
    dilution_pct_per_year: Decimal = Field(default=D("5"), ge=0)
    contingent_pct_of_net_worth: Decimal = Field(default=D("20"), ge=0)
    auditor_lookback_days: int = Field(default=730, gt=0)
    severity: dict[str, FlagSeverity] = dict(DEFAULT_SEVERITY)


class MarketCapBands(_Group):
    """Lower bounds of the large and mid buckets in the market's own currency units."""

    large_min: Decimal = Field(gt=0)
    mid_min: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _descending(self) -> "MarketCapBands":
        if self.large_min <= self.mid_min:
            raise ValueError("large_min must exceed mid_min (thresholds are descending)")
        return self


class XraySettings(_Group):
    # holding asset class -> target-allocation class; anything unmapped is reported "unclassified"
    asset_class_map: dict[str, str] = {"equity": "equity", "etf": "equity", "bond": "debt"}
    # mutual-fund category prefix (case-insensitive, longest wins) -> target-allocation class
    mf_category_map: dict[str, str] = {"Equity": "equity", "Debt": "debt", "Liquid": "debt"}
    market_cap: dict[str, MarketCapBands] = {}  # market (IN, US) -> bands; empty: unclassified
    top_n: list[int] = [5, 10]

    @field_validator("top_n")
    @classmethod
    def _top(cls, v: list[int]) -> list[int]:
        return _positive(v, "top_n")


class RiskSettings(_Group):
    lookback_days: int = Field(default=252, gt=0)
    drawdown_years: list[int] = [1, 3]
    participation_pct: Decimal = Field(default=D("10"), gt=0, le=100)
    adv_days: int = Field(default=20, gt=0)
    min_overlap_days: int = Field(default=60, gt=1)

    @field_validator("drawdown_years")
    @classmethod
    def _years(cls, v: list[int]) -> list[int]:
        return _positive(v, "drawdown_years")


class ScreenSettings(_Group):
    max_universe: int = Field(default=5000, gt=0)
    max_rules: int = Field(default=50, gt=0)
    revision_lookback_days: int = Field(default=90, gt=0)  # the window revisions are measured over


FACTORS = ("quality", "value", "growth", "momentum", "risk")


class FactorWeights(_Group):
    quality: Decimal = Field(ge=0)
    value: Decimal = Field(ge=0)
    growth: Decimal = Field(ge=0)
    momentum: Decimal = Field(ge=0)
    risk: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _sum(self) -> "FactorWeights":
        if sum((getattr(self, f) for f in FACTORS), Decimal(0)) != 100:
            raise ValueError("factor weights must sum to 100")
        return self


class HorizonWeights(_Group):
    long_term: FactorWeights = FactorWeights(
        quality=D("30"), value=D("25"), growth=D("25"), momentum=D("10"), risk=D("10")
    )
    positional: FactorWeights = FactorWeights(
        quality=D("15"), value=D("10"), growth=D("20"), momentum=D("45"), risk=D("10")
    )


class ScoringSettings(_Group):
    weights_version: str = "pid-15.5-v1"
    weights: HorizonWeights = HorizonWeights()
    top_band_min: Decimal = Field(default=D("75"), ge=0, le=100)
    upper_band_min: Decimal = Field(default=D("60"), ge=0, le=100)
    hold_band_min: Decimal = Field(default=D("40"), ge=0, le=100)
    support_floor: Decimal = Field(default=D("50"), ge=0, le=100)
    valuation_penalty_over_pct: Decimal = Field(default=D("90"), ge=0, le=100)
    valuation_penalty_points: Decimal = Field(default=D("25"), ge=0, le=100)
    min_input_pct: Decimal = Field(default=D("70"), gt=0, le=100)
    min_peers_for_sector: int = Field(default=5, gt=0)
    flag_penalty_hard: Decimal = Field(default=D("50"), ge=0, le=100)  # off the flag health input
    flag_penalty_soft: Decimal = Field(default=D("20"), ge=0, le=100)
    setup_quality: dict[str, Decimal] = {
        "breakout": D("90"), "pullback": D("80"), "trend_continuation": D("70"),
        "base": D("50"), "downtrend": D("0"), "none": D("30"),
    }  # fmt: skip

    @field_validator("weights_version")
    @classmethod
    def _label(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("weights_version must be a non-empty label")
        return v.strip()

    @model_validator(mode="after")
    def _bands(self) -> "ScoringSettings":
        if not self.top_band_min > self.upper_band_min > self.hold_band_min:
            raise ValueError("band minimums must descend: top_band_min > upper > hold")
        if any(not (0 <= v <= 100) for v in self.setup_quality.values()):
            raise ValueError("setup_quality values must be between 0 and 100")
        return self

    def weights_digest(self) -> str:
        """Short digest of every factor weight; it changes when any weight changes."""
        text = ";".join(
            f"{h}:{f}={getattr(getattr(self.weights, h), f).normalize():f}"
            for h in ("long_term", "positional")
            for f in FACTORS
        )
        return hashlib.sha256(text.encode()).hexdigest()[:16]


class AnalysisSettings(_Group):
    ta: TaSettings = TaSettings()
    levels: LevelsSettings = LevelsSettings()
    regime: RegimeSettings = RegimeSettings()
    setups: SetupsSettings = SetupsSettings()
    fa: FaSettings = FaSettings()
    valuation: ValuationSettings = ValuationSettings()
    flags: FlagsSettings = FlagsSettings()
    xray: XraySettings = XraySettings()
    risk: RiskSettings = RiskSettings()
    screen: ScreenSettings = ScreenSettings()
    scoring: ScoringSettings = ScoringSettings()
