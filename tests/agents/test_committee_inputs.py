"""`prepare_inputs` with score cards supplied by the caller (no re-scoring) and exact ids."""

import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters import analysis_service as svc
from nivesh_agents import committee
from nivesh_agents.committee import prepare_inputs
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from tests.analysis_fx import make_profile
from tests.ideas_fx import ASOF, seed_ideas_store, settings_for

D = Decimal


def stores(data: Path):  # type: ignore[no-untyped-def]
    return open_sqlite(data / "nivesh.sqlite"), open_duck(data / "nivesh.duckdb", read_only=True)


def test_prepare_inputs_default_signature_and_behaviour_unchanged(tmp_path: Path) -> None:
    params = inspect.signature(prepare_inputs).parameters
    assert list(params)[:7] == [
        "duck", "sql", "settings", "profile", "queries", "day", "starter_weight_pct"
    ]  # fmt: skip
    assert (
        params["cards"].default is None and params["cards"].kind is inspect.Parameter.KEYWORD_ONLY
    )
    seed_ideas_store(tmp_path / "d")
    sql, duck = stores(tmp_path / "d")
    try:
        s = settings_for(tmp_path / "d")
        got = prepare_inputs(duck, sql, s, make_profile(), ["AAA"], ASOF, starter_weight_pct=D(2))
        want = svc.score_one(duck, sql, s, "AAA", "long_term", ASOF).card
    finally:
        sql.close()
        duck.close()
    assert got.securities[0].card == want


def test_prepare_inputs_uses_supplied_cards_and_does_not_rescore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ideas_store(tmp_path / "d")
    sql, duck = stores(tmp_path / "d")
    try:
        s = settings_for(tmp_path / "d")
        rep = svc.score_report(duck, sql, s, [], "positional", ASOF)
        monkeypatch.setattr(committee.svc, "score_one", lambda *a, **k: pytest.fail("scored again"))
        ids = sorted(rep.cards)[:3]
        got = prepare_inputs(
            duck, sql, s, make_profile(), [f"id:{i}" for i in ids], ASOF,
            starter_weight_pct=D(2), cards=rep.cards,
        )  # fmt: skip
    finally:
        sql.close()
        duck.close()
    assert [x.card for x in got.securities] == [rep.cards[i] for i in ids]
    assert all(x.card is not None and x.card.horizon == "positional" for x in got.securities)


def test_supplied_cards_for_wrong_security_ignored_with_data_gap(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    sql, duck = stores(tmp_path / "d")
    try:
        s = settings_for(tmp_path / "d")
        rep = svc.score_report(duck, sql, s, [], "long_term", ASOF)
        only_other = {i: c for i, c in rep.cards.items() if i != 1}
        got = prepare_inputs(
            duck, sql, s, make_profile(), ["id:1"], ASOF, starter_weight_pct=D(2),
            cards=only_other,
        )  # fmt: skip
    finally:
        sql.close()
        duck.close()
    card = got.securities[0].card
    assert card is not None and card.security_id == 1
    assert (card.inputs_available, card.composite) == (0, None)


def test_a_symbol_in_both_markets_is_ambiguous_but_an_id_is_exact(tmp_path: Path) -> None:
    ids = seed_ideas_store(
        tmp_path / "d", in_symbols=("DUP", "BBB"), us_symbols=("DUP", "UBB"), load=False
    )
    sql, duck = stores(tmp_path / "d")
    rows = sql.execute(
        "SELECT id, market FROM security WHERE symbol = 'DUP' ORDER BY id"
    ).fetchall()
    assert [m for _, m in rows] == ["IN", "US"] and len({i for i, _ in rows}) == 2
    try:
        s = settings_for(tmp_path / "d")
        with pytest.raises(NiveshError, match="ambiguous"):
            prepare_inputs(duck, sql, s, make_profile(), ["DUP"], ASOF, starter_weight_pct=D(2))
        rep = svc.score_report(duck, sql, s, [], "long_term", ASOF)
        us_id = next(i for i, m in rows if m == "US")
        got = prepare_inputs(
            duck, sql, s, make_profile(), [f"id:{us_id}"], ASOF, starter_weight_pct=D(2),
            cards=rep.cards,
        )  # fmt: skip
    finally:
        sql.close()
        duck.close()
    t = got.securities[0].target
    assert (t.security_id, t.market, t.symbol) == (us_id, "US", "DUP")
    assert ids["DUP"] in {i for i, _ in rows}


def test_id_reference_for_an_unknown_row_says_so(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    sql, duck = stores(tmp_path / "d")
    try:
        with pytest.raises(NiveshError, match="no security matches"):
            svc.resolve_security(sql, "id:99999")
    finally:
        sql.close()
        duck.close()
