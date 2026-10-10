import random
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core.analysis_config import AnalysisSettings, ScoringSettings
from nivesh_engine import scoring
from nivesh_engine.redflags import FlagResult
from nivesh_engine.scoring import ScoreCard, SecurityMeta, ranking, raw_inputs, score_universe
from tests.analysis_fx import UNIVERSE_ASOF, seed_universe

D = Decimal
Q = D("0.0001")
US = SecurityMeta("US", "Tech")
LOOSE = AnalysisSettings(scoring=ScoringSettings(min_input_pct=D(1), min_peers_for_sector=4))

# one stand-in sub-input per factor, in the same direction as the real ones
QUALITY, GROWTH, VALUE = "return_on_capital", "eps_cagr", "fcf_yield_pct"
MOMENTUM, RISK = "price_vs_sma200_pct", "vol_60d"
NON_PRICE_HIGH = ("return_on_capital", "cfo_to_pat", "net_margin_pct", "fcf_yield_pct",
                  "revenue_cagr", "eps_cagr", "net_income_cagr")  # fmt: skip
NON_PRICE_LOW = ("debt_equity", "revenue_growth_stdev_pp", "valuation_percentile",
                 "pe_vs_peer_median_pct", "ev_ebitda")  # fmt: skip
PRICE_HIGH = ("price_vs_sma200_pct", "rs_benchmark_change_6m_pct")
PRICE_LOW = ("vol_60d", "max_drawdown_pct", "atr_pct")


def flag(name: str, status: str, severity: str | None = None) -> FlagResult:
    return FlagResult(name, status, severity, [], "", None)  # type: ignore[arg-type]


def run(
    raw: dict[int, dict[str, Decimal | None]],
    *,
    cfg: AnalysisSettings = LOOSE,
    horizon: str = "long_term",
    meta: dict[int, SecurityMeta] | None = None,
    flags: dict[int, list[FlagResult] | None] | None = None,
) -> dict[int, ScoreCard]:
    return score_universe(
        raw,
        meta or {sid: US for sid in raw},
        flags or {},
        horizon=horizon,  # type: ignore[arg-type]
        cfg=cfg,
    )


def cohort_of_four() -> dict[int, dict[str, Decimal | None]]:
    """Security 1 is 4th (87.5) in quality, 3rd (62.5) in value, 2nd (37.5) in growth, 4th in
    momentum and the lowest volatility (inverted: 87.5)."""
    return {
        1: {QUALITY: D(40), VALUE: D(3), GROWTH: D(2), MOMENTUM: D(40), RISK: D(1)},
        2: {QUALITY: D(30), VALUE: D(4), GROWTH: D(1), MOMENTUM: D(30), RISK: D(2)},
        3: {QUALITY: D(20), VALUE: D(2), GROWTH: D(3), MOMENTUM: D(20), RISK: D(3)},
        4: {QUALITY: D(10), VALUE: D(1), GROWTH: D(4), MOMENTUM: D(10), RISK: D(4)},
    }


# ---- percentiles and weights ------------------------------------------------------------------
def test_factor_percentile_within_market_and_sector_cohort_known_answer() -> None:
    raw = {i: {QUALITY: D(10 * i)} for i in range(1, 6)}
    cards = run(raw)
    # a sector cohort of 5 (the minimum): mid-rank percentile 100 * (below + equal / 2) / 5
    assert [cards[i].factors["quality"] for i in range(1, 6)] == [
        D("10"), D("30"), D("50"), D("70"), D("90"),
    ]  # fmt: skip
    lower = run({i: {"debt_equity": D(i)} for i in range(1, 6)})
    assert lower[1].factors["quality"] == D("90")  # lower is better: inverted
    assert lower[5].factors["quality"] == D("10")


def test_small_sector_cohort_falls_back_to_market_with_reason_per_min_peers_for_sector() -> None:
    raw = {i: {QUALITY: D(10 * i)} for i in range(1, 6)}
    raw[6], raw[7] = {QUALITY: D(15)}, {QUALITY: D(45)}
    fin = SecurityMeta("US", "Fin")
    meta = {**{i: US for i in range(1, 6)}, 6: fin, 7: fin}
    cards = run(raw, cfg=AnalysisSettings(scoring=ScoringSettings(min_input_pct=D(1))), meta=meta)
    # the two "Fin" names (below 5 peers) are ranked against all 7 US names
    assert cards[7].factors["quality"].quantize(Q) == D("78.5714")  # (5 + 1/2) / 7
    assert any("min_peers_for_sector" in r for r in cards[7].reasons)
    assert not any("min_peers_for_sector" in r for r in cards[3].reasons)  # sector of 5 is enough
    assert cards[3].factors["quality"] == D("50")


def test_cohorts_never_mix_markets() -> None:
    raw = {1: {QUALITY: D(1)}, 2: {QUALITY: D(1000)}}
    meta = {1: SecurityMeta("IN", "Tech"), 2: SecurityMeta("US", "Tech")}
    cards = run(raw, meta=meta)
    assert cards[1].factors["quality"] == D(50) == cards[2].factors["quality"]  # a cohort of one


def test_cohort_of_one_scores_fifty_not_one_hundred() -> None:
    cards = run({1: {QUALITY: D(10**6), GROWTH: D(10**6), VALUE: D(10**6)}})
    assert cards[1].factors["quality"] == D(50)
    assert cards[1].composite == D(50)


def test_long_term_and_positional_composites_known_answer() -> None:
    long = run(cohort_of_four(), horizon="long_term")[1]
    pos = run(cohort_of_four(), horizon="positional")[1]
    # support = mean(87.5, 62.5, 37.5) = 62.5; momentum credit and price risk are capped by it
    assert long.factors == {
        "quality": D("87.5"), "value": D("62.5"), "growth": D("37.5"),
        "momentum": D("62.5"), "risk": D("62.5"),
    }  # fmt: skip
    assert long.support == D("62.5") and long.momentum_credit == D("62.5")
    assert long.composite == D("63.75")  # (30*87.5 + 25*62.5 + 25*37.5 + 10*62.5 + 10*62.5) / 100
    assert pos.composite == D("61.25")  # (15, 10, 20, 45, 10) weights
    assert (long.band, pos.band) == ("upper", "upper")
    assert long.horizon == "long_term" and pos.horizon == "positional"


def test_unknown_horizon_is_rejected() -> None:
    with pytest.raises(ValueError, match="horizon"):
        run(cohort_of_four(), horizon="swing")


def test_output_carries_weights_version_and_digest() -> None:
    cfg = AnalysisSettings(scoring=ScoringSettings(weights_version="mine-v2", min_input_pct=D(1)))
    card = run(cohort_of_four(), cfg=cfg)[1]
    assert card.weights_version == "mine-v2"
    assert card.weights_digest == cfg.scoring.weights_digest()
    assert len(card.weights_digest) == 16


# ---- insufficient data -------------------------------------------------------------------------
NAMES = [*NON_PRICE_HIGH, *NON_PRICE_LOW, *PRICE_HIGH, *PRICE_LOW, "revisions_direction",
         "setup_quality"]  # fmt: skip


def test_insufficient_data_below_70_pct_of_required_inputs() -> None:
    assert len(NAMES) == 19  # plus the red-flag health input: 20 in all
    default = AnalysisSettings()
    card = run({1: {n: D(5) for n in NAMES[:13]}}, cfg=default)[1]
    assert (card.inputs_available, card.inputs_total) == (13, 20)
    assert card.band == "insufficient_data" and card.composite is None
    assert any("13 of 20" in r for r in card.reasons)


def test_exactly_70_pct_scores_normally() -> None:
    card = run({1: {n: D(5) for n in NAMES[:14]}}, cfg=AnalysisSettings())[1]
    assert (card.inputs_available, card.inputs_total) == (14, 20)
    assert card.composite is not None and card.band != "insufficient_data"


def test_revisions_unavailable_uses_remaining_inputs_and_reports_inputs_available() -> None:
    full = {n: D(5) for n in NAMES}
    thin = {n: v for n, v in full.items() if n != "revisions_direction"}
    a = run({1: full}, cfg=AnalysisSettings())[1]
    b = run({1: thin}, cfg=AnalysisSettings())[1]
    assert (a.inputs_available, b.inputs_available) == (19, 18)
    assert b.composite is not None and b.factors["growth"] is not None


# ---- red flags ---------------------------------------------------------------------------------
def top_pair() -> dict[int, dict[str, Decimal | None]]:
    """Security 1 is above security 2 on every input, whatever its direction."""
    hi = {n: D(2) for n in (*NON_PRICE_HIGH, *PRICE_HIGH)}
    hi |= {n: D(1) for n in (*NON_PRICE_LOW, *PRICE_LOW)}
    lo = {n: D(1) for n in (*NON_PRICE_HIGH, *PRICE_HIGH)}
    lo |= {n: D(2) for n in (*NON_PRICE_LOW, *PRICE_LOW)}
    return {1: hi, 2: lo}


PAIR_CFG = AnalysisSettings(
    scoring=ScoringSettings(min_input_pct=D(1), min_peers_for_sector=2, top_band_min=D(70))
)


def test_hard_red_flag_sets_cap_hold_and_band_never_above_hold() -> None:
    clean = run(top_pair(), cfg=PAIR_CFG)[1]
    assert clean.composite is not None and clean.composite >= D(70) and clean.band == "top"
    assert clean.cap is None
    hard = run(top_pair(), cfg=PAIR_CFG, flags={1: [flag("cfo_to_pat", "fired", "hard")]})[1]
    assert hard.cap == "HOLD" and hard.band == "hold"
    assert any("hard" in r for r in hard.reasons)
    low = run(
        {1: {n: D(1) for n in NAMES}, 2: {n: D(2) for n in NAMES}},
        cfg=PAIR_CFG,
        flags={1: [flag("cfo_to_pat", "fired", "hard")]},
    )[1]
    assert low.cap == "HOLD" and low.band in ("hold", "weak")  # a clamp never raises a band


def test_soft_flag_lowers_risk_factor_but_sets_no_cap() -> None:
    base = run(top_pair(), cfg=PAIR_CFG, flags={1: [flag("dilution", "clear")]})[1]
    soft = run(top_pair(), cfg=PAIR_CFG, flags={1: [flag("dilution", "fired", "soft")]})[1]
    assert soft.cap is None
    assert base.factors["risk"] > soft.factors["risk"]  # type: ignore[operator]
    assert base.composite > soft.composite  # type: ignore[operator]


def test_not_evaluable_flags_neither_cap_nor_reassure() -> None:
    none = run(top_pair(), cfg=PAIR_CFG)[1]
    unknown = run(
        top_pair(),
        cfg=PAIR_CFG,
        flags={1: [flag("pledge", "not_evaluable"), flag("auditor", "not_evaluable")]},
    )[1]
    assert unknown.cap is None
    assert unknown.factors["risk"] == none.factors["risk"]
    assert unknown.inputs_available == none.inputs_available  # a clean bill needs real evidence


# ---- the BR-12 guard ---------------------------------------------------------------------------
def test_valuation_percentile_over_90_penalises_momentum() -> None:
    def credit(pct: str) -> Decimal | None:
        raw = {1: {QUALITY: D(1), GROWTH: D(1), VALUE: D(1), MOMENTUM: D(1),
                   "valuation_percentile": D(pct)}}  # fmt: skip
        return run(raw)[1].momentum_credit

    assert credit("90") == D(50)  # exactly the threshold: no penalty
    assert credit("91") == D(25)  # 25 points off
    assert credit("100") == D(25)


def test_momentum_credit_is_capped_by_fundamental_support() -> None:
    raw = cohort_of_four()
    raw[1] |= {QUALITY: D(1), GROWTH: D(0), VALUE: D(0)}  # strict worst fundamentals
    card = run(raw)[1]
    assert card.support == D("12.5")
    assert card.factors["momentum"] == D("12.5") and card.momentum_credit == D("12.5")


def test_price_derived_inputs_are_classified_and_capped_the_same_way() -> None:
    assert scoring.PRICE_DERIVED == {*PRICE_HIGH, *PRICE_LOW, "setup_quality"}
    assert {s.name for s in scoring.SUB_INPUTS if s.price_derived} == scoring.PRICE_DERIVED
    for name in scoring.PRICE_DERIVED - {"setup_quality"}:
        top = name in PRICE_HIGH
        raw: dict[int, dict[str, Decimal | None]] = {
            1: {QUALITY: D(1), GROWTH: D(1), VALUE: D(1), name: D(9) if top else D(1)},
            2: {QUALITY: D(9), GROWTH: D(9), VALUE: D(9), name: D(1) if top else D(9)},
        }
        card = run(raw, cfg=PAIR_CFG)[1]
        assert card.support == D(25)
        factor = "momentum" if top else "risk"
        assert card.factors[factor] == D(25), name  # the best price input, held to the support


def test_setup_quality_is_a_price_input_held_to_the_support() -> None:
    raw = {1: {QUALITY: D(1), GROWTH: D(1), VALUE: D(1), "setup_quality": D(90)},
           2: {QUALITY: D(9), GROWTH: D(9), VALUE: D(9), "setup_quality": D(10)}}  # fmt: skip
    assert run(raw, cfg=PAIR_CFG)[1].factors["momentum"] == D(25)


def test_no_non_price_factor_means_no_momentum_credit() -> None:
    card = run({1: {MOMENTUM: D(10**6), RISK: D(1)}})[1]
    assert card.support is None
    assert card.momentum_credit == D(0) and card.band != "top"


def top_at(top: int) -> AnalysisSettings:
    return AnalysisSettings(
        scoring=ScoringSettings(
            min_input_pct=D(1),
            top_band_min=D(top),
            upper_band_min=D(top - 10),
            hold_band_min=D(top - 20),
        )
    )


def price_only_vector(rng: random.Random) -> dict[int, dict[str, Decimal | None]]:
    """Security 1 has the best possible price inputs; every non-price input is missing or sits
    below all five peers (so its fundamentals are at the bottom of the cohort)."""
    raw: dict[int, dict[str, Decimal | None]] = {1: {}}
    for n in PRICE_HIGH:
        raw[1][n] = D(1000 + rng.randrange(1000))
    for n in PRICE_LOW:
        raw[1][n] = D(rng.randrange(1))  # 0: the lowest possible
    raw[1]["setup_quality"] = D(100)
    for n in (*NON_PRICE_HIGH, *NON_PRICE_LOW, "revisions_direction"):
        if rng.randrange(3):  # two in three present, at the bottom of the cohort
            bad_is_low = n in NON_PRICE_HIGH
            if n == "revisions_direction":
                raw[1][n] = D(rng.randrange(0, 30))
            else:
                raw[1][n] = D(rng.randrange(0, 500)) if bad_is_low else D(rng.randrange(1500, 2000))
    for peer in range(2, 7):
        raw[peer] = {n: D(1000 + rng.randrange(500)) for n in (*NON_PRICE_HIGH, *PRICE_HIGH)}
        raw[peer] |= {n: D(rng.randrange(500)) for n in (*NON_PRICE_LOW, *PRICE_LOW)}
        raw[peer]["revisions_direction"] = D(100)
        raw[peer]["setup_quality"] = D(rng.randrange(100))
    return raw


def test_br12_property_trailing_price_only_never_reaches_top_band_seeded_sweep() -> None:
    rng = random.Random(20261010)  # noqa: S311 - seeded sweep, not crypto
    cfg = top_at(40)
    # even with the top band set at 40 (hold) the guard keeps the price-only name out of it
    tried = 0
    for trial in range(3000):
        horizon = "positional" if trial % 2 else "long_term"
        card = run(price_only_vector(rng), cfg=cfg, horizon=horizon)[1]
        tried += 1
        assert card.band != "top", (trial, card)
        if card.support is not None:
            assert card.support < cfg.scoring.support_floor
    assert tried == 3000
    # explicit adversarial vectors: every price input at its best, non-price inputs missing
    best = (
        {n: D(10**9) for n in PRICE_HIGH} | {n: D(0) for n in PRICE_LOW} | {"setup_quality": D(100)}
    )
    for horizon in ("long_term", "positional"):
        for n_peers in range(0, 6):
            peers = {p: {n: D(1) for n in PRICE_HIGH} for p in range(2, 2 + n_peers)}
            raw = {1: dict(best)} | peers
            card = run(raw, cfg=cfg, horizon=horizon)[1]
            assert card.band != "top" and card.support is None
            assert card.composite is None or card.composite <= cfg.scoring.support_floor


def test_br12_raising_momentum_alone_never_changes_a_non_top_band_to_top() -> None:
    rng = random.Random(20261011)  # noqa: S311 - seeded sweep, not crypto
    cfg = top_at(30)
    for trial in range(1500):
        raw = price_only_vector(rng)
        before = run(raw, cfg=cfg)[1]
        assert before.band != "top"
        bumped = {sid: dict(v) for sid, v in raw.items()}
        for n in PRICE_HIGH:
            bumped[1][n] = D(10**9)
        bumped[1]["setup_quality"] = D(100)
        after = run(bumped, cfg=cfg)[1]
        assert after.band != "top", trial


def test_top_band_needs_support_at_the_floor() -> None:
    cfg = AnalysisSettings(
        scoring=ScoringSettings(
            min_input_pct=D(1),
            min_peers_for_sector=2,
            top_band_min=D(20),
            upper_band_min=D(10),
            hold_band_min=D(5),
            support_floor=D(80),
        )  # fmt: skip
    )
    card = run(top_pair(), cfg=cfg)[1]
    assert card.support == D("75") and card.composite is not None and card.composite >= D(20)
    assert card.band == "upper"  # the composite clears the top bar, the support does not


def test_scoring_source_does_not_reference_trailing_return_fields() -> None:
    text = Path(scoring.__file__).read_text()
    for needle in ("roc_3m", "roc_6m", "roc_12m", "roc_", "return_1y", "trailing_return"):
        assert needle not in text, needle


# ---- ranking, determinism, extraction ---------------------------------------------------------
def test_ranking_ties_break_by_score_desc_then_security_id() -> None:
    raw = {i: {QUALITY: D(10)} for i in (7, 3, 5)} | {9: {QUALITY: D(99)}}
    cards = run(raw)
    assert ranking(cards) == [9, 3, 5, 7]
    thin = run({11: {QUALITY: D(1)}}, cfg=AnalysisSettings())
    assert thin[11].composite is None
    assert ranking({**thin, **cards}) == [9, 3, 5, 7, 11]  # no composite: last


def test_scoring_is_deterministic() -> None:
    a = run(cohort_of_four(), horizon="positional")
    b = run(cohort_of_four(), horizon="positional")
    assert a == b and repr(a) == repr(b)
    for card in a.values():
        assert all(v is None or isinstance(v, Decimal) for v in card.factors.values())


def test_raw_inputs_from_the_registry_cover_every_sub_input_and_score_deterministically() -> None:
    universe = seed_universe(8)
    cfg = AnalysisSettings()
    raw, meta, flags = raw_inputs(universe, as_of=UNIVERSE_ASOF, cfg=cfg)
    names = {s.name for s in scoring.SUB_INPUTS} - {"flag_health"}
    assert set(raw) == set(universe) == set(meta) == set(flags)
    assert all(set(v) == names for v in raw.values())
    assert any(v["return_on_capital"] is not None for v in raw.values())
    assert meta[1] == SecurityMeta("US", "Tech")
    one = score_universe(raw, meta, flags, horizon="long_term", cfg=cfg)
    two = score_universe(raw, meta, flags, horizon="long_term", cfg=cfg)
    assert one == two and len(one) == 8
    bands = ("top", "upper", "hold", "weak", "insufficient_data")
    assert all(c.band in bands for c in one.values())
    assert any(c.composite is not None for c in one.values())


def test_raw_inputs_ignore_bars_after_the_as_of_date() -> None:
    universe = seed_universe(3)
    early = date(2024, 6, 30)
    a, _, _ = raw_inputs(universe, as_of=early, cfg=AnalysisSettings())
    b, _, _ = raw_inputs(universe, as_of=UNIVERSE_ASOF, cfg=AnalysisSettings())
    assert a[1]["max_drawdown_pct"] != b[1]["max_drawdown_pct"]
