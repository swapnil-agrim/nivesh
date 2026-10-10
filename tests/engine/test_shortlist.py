from decimal import Decimal

import pytest

from nivesh_core.universe_config import IdeasSettings
from nivesh_engine.shortlist import (
    UNCLASSIFIED,
    IdeaVerdict,
    Scored,
    rank_ideas,
    shortlist,
    watch_distance,
)

D = Decimal
CFG = IdeasSettings()


def sc(i: int, comp: str | None, sector: str | None = "Tech", symbol: str | None = None,
       held: bool = False) -> Scored:  # fmt: skip
    return Scored(i, symbol or f"S{i:02d}", None if comp is None else D(comp), sector, held)


def ids(res) -> list[int]:  # type: ignore[no-untyped-def]
    return [r.security_id for r in res.rows]


# ---- shortlist --------------------------------------------------------------------------------
def test_ranked_by_composite_desc_then_symbol_asc() -> None:
    cands = [sc(1, "50", "A", "ZZ"), sc(2, "80", "B"), sc(3, "50", "C", "AA"), sc(4, "65", "D")]
    res = shortlist(cands, CFG)
    assert ids(res) == [2, 4, 3, 1]  # 80, 65, then the 50s by symbol AA < ZZ
    assert [r.rank for r in res.rows] == [1, 2, 3, 4]


def test_at_most_two_per_sector() -> None:
    cands = [sc(i, str(90 - i), "Tech") for i in range(1, 6)] + [sc(9, "10", "Banks")]
    res = shortlist(cands, CFG)
    assert ids(res) == [1, 2, 9]
    assert {d.security_id: d.reason for d in res.dropped} == {
        3: "sector cap (2 per sector)", 4: "sector cap (2 per sector)",
        5: "sector cap (2 per sector)",
    }  # fmt: skip


def test_missing_sector_goes_to_unclassified_bucket_exempt_from_the_cap_with_a_warning() -> None:
    cands = [sc(i, str(100 - i), None) for i in range(1, 31)]  # 30 names, no sector at all
    res = shortlist(cands, CFG)
    assert len(res.rows) == 8 and all(r.sector == UNCLASSIFIED for r in res.rows)
    assert any("unclassified" in n and "not capped" in n for n in res.notes)
    blank = shortlist([sc(1, "9", "  "), sc(2, "8", "Tech")], CFG)
    assert [r.sector for r in blank.rows] == [UNCLASSIFIED, "Tech"]


def test_size_default_8_and_configurable_and_never_above_max() -> None:
    cands = [sc(i, str(100 - i), f"S{i}") for i in range(1, 30)]
    assert len(shortlist(cands, CFG).rows) == 8
    assert len(shortlist(cands, IdeasSettings(shortlist_size=3)).rows) == 3
    assert len(shortlist(cands, IdeasSettings(shortlist_size=12)).rows) == 12
    big = IdeasSettings.model_construct(shortlist_size=50, shortlist_max=12, sector_cap=2)
    assert len(shortlist(cands, big).rows) == 12  # a bad config cannot fan out


def test_held_security_labelled_add_and_kept_by_default() -> None:
    res = shortlist([sc(1, "90", held=True), sc(2, "80", "B")], CFG)
    assert ids(res) == [1, 2]
    assert [r.label for r in res.rows] == ["ADD candidate", ""]


def test_held_security_excluded_when_configured() -> None:
    res = shortlist([sc(1, "90", held=True), sc(2, "80", "B")], IdeasSettings(held="exclude"))
    assert ids(res) == [2]
    assert res.dropped[0].reason == "already held"


def test_fewer_matches_than_size_returns_fewer_without_padding() -> None:
    res = shortlist([sc(1, "90", "A"), sc(2, "80", "B")], CFG)
    assert len(res.rows) == 2
    assert shortlist([], CFG).rows == ()


def test_composite_none_sorts_last_with_reason() -> None:
    res = shortlist([sc(1, None, "A"), sc(2, "1", "B")], CFG)
    assert ids(res) == [2, 1] and res.rows[1].note == "no composite score"


def test_order_is_deterministic_under_input_permutation() -> None:
    cands = [sc(i, str((i * 37) % 11), f"S{i % 3}") for i in range(1, 25)]
    base = shortlist(cands, CFG)
    assert shortlist(list(reversed(cands)), CFG) == base
    assert shortlist(cands[5:] + cands[:5], CFG) == base


def test_property_no_sector_exceeds_cap_for_generated_tables() -> None:
    x = 12345
    for cap in (1, 2, 3):
        cfg = IdeasSettings(sector_cap=cap, shortlist_size=10)
        for _ in range(25):
            cands = []
            for i in range(1, 40):
                x = (x * 1103515245 + 12345) % 2**31
                cands.append(
                    sc(i, str((x >> 8) % 100), f"X{(x >> 4) % 5}", held=(x >> 12) % 7 == 0)
                )
            res = shortlist(cands, cfg)
            counts: dict[str, int] = {}
            for r in res.rows:
                counts[r.sector] = counts.get(r.sector, 0) + 1
            assert max(counts.values(), default=0) <= cap and len(res.rows) <= 10
            comps = [r.composite for r in res.rows if r.composite is not None]
            assert comps == sorted(comps, reverse=True)


# ---- rank_ideas -------------------------------------------------------------------------------
def iv(i: int, verdict: str, conv: str = "medium", comp: str | None = "50", veto: bool = False,
       symbol: str | None = None) -> IdeaVerdict:  # fmt: skip
    return IdeaVerdict(
        i, symbol or f"S{i:02d}", verdict, conv, None if comp is None else D(comp), veto
    )


def test_only_buy_and_accumulate_qualify_and_vetoed_never_do() -> None:
    vs = [iv(1, "BUY"), iv(2, "ACCUMULATE"), iv(3, "HOLD"), iv(4, "TRIM"), iv(5, "SELL"),
          iv(6, "AVOID"), iv(7, "INSUFFICIENT_DATA"), iv(8, "BUY", veto=True)]  # fmt: skip
    res = rank_ideas(vs, 10)
    assert [i.security_id for i in res.ideas] == [1, 2] and res.qualified == 2


def test_rank_by_conviction_then_composite_then_symbol() -> None:
    vs = [
        iv(1, "BUY", "medium", "90"), iv(2, "BUY", "high", "10"), iv(3, "BUY", "low", "99"),
        iv(4, "ACCUMULATE", "medium", "90", symbol="AAA"), iv(5, "BUY", "medium", None),
    ]  # fmt: skip
    res = rank_ideas(vs, 5)
    assert [i.security_id for i in res.ideas] == [2, 4, 1, 5, 3]


def test_top_n_default_5_and_n_override() -> None:
    vs = [iv(i, "BUY", comp=str(i)) for i in range(1, 9)]
    assert len(rank_ideas(vs, CFG.default_n).ideas) == 5
    assert [i.security_id for i in rank_ideas(vs, 2).ideas] == [8, 7]
    assert rank_ideas(vs, 8).message == ""


def test_fewer_than_n_qualifying_reports_k_of_n_without_padding() -> None:
    res = rank_ideas([iv(1, "BUY"), iv(2, "HOLD"), iv(3, "ACCUMULATE")], 5)
    assert len(res.ideas) == 2 and res.message == "only 2 of 5 ideas qualified; no padding"


def test_zero_qualifying_says_none_qualified() -> None:
    res = rank_ideas([iv(1, "HOLD")], 5)
    assert res.ideas == () and res.message == "none of the 1 shortlisted names qualified"
    assert rank_ideas([], 5).message == "nothing was shortlisted"


def test_never_changes_a_verdict() -> None:
    vs = [iv(1, "BUY"), iv(2, "ACCUMULATE", "high"), iv(3, "HOLD")]
    res = rank_ideas(vs, 5)
    assert {i.security_id: i.verdict for i in res.ideas} == {1: "BUY", 2: "ACCUMULATE"}
    assert [v.verdict for v in vs] == ["BUY", "ACCUMULATE", "HOLD"]


def test_reported_flag_only_for_top_n() -> None:
    vs = [iv(i, "BUY", comp=str(i)) for i in range(1, 8)] + [iv(9, "HOLD")]
    res = rank_ideas(vs, 3)
    assert res.reported_ids == frozenset({7, 6, 5})
    assert res.qualified == 7


def test_n_must_be_positive() -> None:
    with pytest.raises(ValueError, match="n must be"):
        rank_ideas([], 0)


# ---- watch_distance ---------------------------------------------------------------------------
def test_distance_above_zone_is_percent_above_entry_high() -> None:
    d = watch_distance(D(110), D(90), D(100))
    assert d.state == "above" and d.percent == D("10.0000") and d.reason is None


def test_inside_zone_is_zero_and_flagged_inside() -> None:
    for close in (D(90), D(95), D(100)):
        d = watch_distance(close, D(90), D(100))
        assert d.state == "inside" and d.percent == D(0)


def test_below_zone_is_negative() -> None:
    d = watch_distance(D(72), D(80), D(100))
    assert d.state == "below" and d.percent == D("-10.0000")


def test_no_zone_or_no_close_gives_reason_not_exception() -> None:
    assert watch_distance(D(5), None, None).reason == "no entry zone set"
    assert watch_distance(None, D(1), D(2)).reason == "no stored close"
    assert watch_distance(D(5), None, None).state == "unknown"
    assert watch_distance(D(5), D(0), D(0)).reason == "entry zone is not positive"


def test_distance_is_exact_decimal() -> None:
    d = watch_distance(D("103.50"), D("95"), D("100"))
    assert d.percent == D("3.5000") and isinstance(d.percent, Decimal)
    assert watch_distance(D(1), D(1), D(3)).percent == D(0)
    assert watch_distance(D(4), D(2), D(3)).percent == D("33.3333")
