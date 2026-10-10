from decimal import Decimal

from nivesh_engine.rebalance import Position, RebalancePlan, propose_moves

D = Decimal
TARGETS = {"equity": D(60), "debt": D(30), "gold": D(10)}


def pos(key: str, cls: str, value: int | str, flag: str | None = None,
        tax: str | None = None) -> Position:  # fmt: skip
    return Position(key, key.upper(), cls, D(value), flag, None if tax is None else D(tax))


def plan(positions: list[Position], cash: int = 0, cap: int = 100, band: int = 5) -> RebalancePlan:
    return propose_moves(
        TARGETS, positions, band_pp=D(band), new_cash=D(cash), turnover_limit_pct=D(cap)
    )


def book(eq: int, debt: int, gold: int) -> list[Position]:
    return [pos("e", "equity", eq), pos("d", "debt", debt), pos("g", "gold", gold)]


def adds(p: RebalancePlan) -> dict[str, Decimal]:
    return {m.asset_class: m.amount_inr for m in p.moves if m.kind == "add"}


def reduces(p: RebalancePlan) -> list[tuple[str, Decimal, str]]:
    return [(m.key, m.amount_inr, m.basis) for m in p.moves if m.kind == "reduce"]


def test_within_band_proposes_nothing() -> None:
    p = plan(book(620, 280, 100))
    assert p.moves == () and p.within_band and p.turnover_pct == 0 and p.reason is None


def test_band_default_5pp_boundary_exactly_5_is_within() -> None:
    p = plan(book(650, 250, 100))
    assert p.moves == () and p.within_band
    q = plan(book(651, 249, 100))
    assert reduces(q) == [("e", D(1), "tax unknown")]  # 65.1% is outside: back to the edge


def test_new_cash_goes_first_to_underweight_classes_in_proportion() -> None:
    # T = 925: shortfalls debt 77.5, gold 42.5, equity none -> 75 split 48.44 / 26.56
    p = plan(book(600, 200, 50), cash=75)
    assert adds(p) == {"debt": D("48.44"), "gold": D("26.56")}
    assert [m.basis for m in p.moves] == ["new cash", "new cash"]


def test_cash_alone_restoring_band_needs_no_reductions() -> None:
    p = plan(book(600, 200, 50), cash=75)  # equity 70.6% before, 64.86% after
    assert reduces(p) == [] and p.within_band and p.turnover_pct == 0
    assert {x.asset_class: x.after_pct for x in p.pro_forma}["equity"] == D("64.86")


def exit_trim_book() -> list[Position]:
    return [
        pos("a", "equity", 300, tax="0.05"), pos("b", "equity", 200, flag="TRIM"),
        pos("c", "equity", 100, flag="EXIT"), pos("x", "equity", 300),
        pos("d", "debt", 80), pos("g", "gold", 20),
    ]  # fmt: skip


def test_reductions_use_exit_then_trim_flags_before_other_holdings() -> None:
    p = plan(exit_trim_book())  # equity 90% -> band edge 65%: excess 250
    assert reduces(p) == [("c", D(100), "review EXIT"), ("b", D(150), "review TRIM")]


def test_unflagged_reductions_rank_by_lowest_tax_per_inr_unknown_last_and_noted() -> None:
    p = plan([
        pos("a", "equity", 100, tax="0.10"), pos("b", "equity", 100, tax="0.02"),
        pos("u", "equity", 750), pos("d", "debt", 40), pos("g", "gold", 10),
    ])  # fmt: skip
    assert reduces(p) == [
        ("b", D(100), "lowest tax per INR"), ("a", D(100), "lowest tax per INR"),
        ("u", D(100), "tax unknown"),
    ]  # fmt: skip
    assert any("tax per INR unknown" in n and "U" in n for n in p.notes)


def test_classes_move_only_to_the_band_edge() -> None:
    p = plan(exit_trim_book())
    lines = {x.asset_class: x for x in p.pro_forma}
    assert lines["equity"].after_pct == D("65.00") and lines["equity"].within_band
    assert adds(p) == {"debt": D("183.34"), "gold": D("66.66")}  # proceeds by shortfall
    assert all(m.basis == "proceeds" for m in p.moves if m.kind == "add")
    assert p.within_band


def test_turnover_pct_is_reductions_over_pre_proposal_value() -> None:
    assert plan(exit_trim_book()).turnover_pct == D("25.00")
    assert plan(book(600, 200, 50), cash=75).turnover_pct == 0


def test_turnover_cap_stops_with_a_partial_move_and_notes_what_stays_out_of_band() -> None:
    p = plan(exit_trim_book(), cap=20)
    assert reduces(p) == [("c", D(100), "review EXIT"), ("b", D(100), "review TRIM")]
    assert p.turnover_pct == D("20.00") and not p.within_band
    cap = [n for n in p.notes if n.startswith("turnover cap 20% reached; still out of band:")]
    assert len(cap) == 1 and "equity" in cap[0]
    assert "per-proposal cap; annual turnover not tracked yet" in p.notes


def test_pro_forma_sums_to_100_and_matches_moves() -> None:
    cases = ((exit_trim_book(), 0, 1000), (book(600, 200, 50), 75, 925))
    for positions, cash, grand in cases:
        p = plan(positions, cash=cash)
        assert sum(x.after_inr for x in p.pro_forma) == grand
        assert abs(sum((x.after_pct for x in p.pro_forma), D(0)) - 100) <= D("0.03")
        for x in p.pro_forma:
            start = sum(q.value_inr for q in positions if q.asset_class == x.asset_class)
            delta = sum(
                m.amount_inr if m.kind == "add" else -m.amount_inr
                for m in p.moves if m.asset_class == x.asset_class
            )  # fmt: skip
            assert x.after_inr == start + delta and x.target_pct == TARGETS[x.asset_class]


def test_zero_portfolio_value_or_no_targets_gives_reason_not_exception() -> None:
    assert plan([]).reason == "no valued holdings"
    z = propose_moves({}, book(1, 1, 1), band_pp=D(5), turnover_limit_pct=D(30))
    assert z.reason == "no target allocation in the profile" and z.moves == ()
    assert plan(book(1, 1, 1), cash=-1).reason == "new cash must not be negative"


def test_cash_beyond_every_shortfall_is_spread_by_target() -> None:
    p = plan(book(600, 300, 100), cash=1000)  # at target already: cash keeps the mix
    assert adds(p) == {"equity": D(600), "debt": D(300), "gold": D(100)}


def test_moves_are_proposals_with_add_or_reduce_kinds_only() -> None:
    p = plan(exit_trim_book(), cash=50)
    assert {m.kind for m in p.moves} <= {"add", "reduce"}
    assert all(
        m.amount_inr > 0 and m.amount_inr == m.amount_inr.quantize(D("0.01")) for m in p.moves
    )


def test_zero_value_position_ranked_first_does_not_stop_the_reductions() -> None:
    p = plan([pos("z", "equity", 0, flag="EXIT"), pos("e", "equity", 700),
              pos("d", "debt", 200), pos("g", "gold", 100)])  # fmt: skip
    assert reduces(p) == [("e", D(50), "tax unknown")]


def test_class_without_a_target_is_left_alone_with_a_note() -> None:
    p = plan([*book(600, 300, 100), pos("u", "unclassified", 100)])
    assert all(m.key != "u" for m in p.moves)
    assert any("unclassified" in n and "no target" in n for n in p.notes)
    assert {x.asset_class: x.target_pct for x in p.pro_forma}["unclassified"] == 0
