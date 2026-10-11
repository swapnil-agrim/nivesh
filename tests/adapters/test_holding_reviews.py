"""The holding review run as a service: faked SDK, faked clock, no network."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import nivesh_adapters.review_service as svc
from nivesh_adapters.review_service import run_holding_reviews
from nivesh_adapters.run_support import BudgetRefused
from nivesh_agents import runtime
from nivesh_core.config import Settings, load_settings
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.profile import Profile, load_profile
from nivesh_core.thesis_store import save_thesis
from nivesh_core.timeutil import to_iso
from tests.agents.fake_sdk import Call, script
from tests.cli.test_engine_cli import ASOF, seed_cli_store
from tests.cli.test_review_cli import reviewer
from tests.core.test_thesis import thesis

Env = tuple[list[str], Path]
CLOCK = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)


class Held:
    def __init__(
        self, settings: Settings, profile: Profile, ids: dict[str, int], fake: Any
    ) -> None:
        self.settings, self.profile, self.ids, self.fake = settings, profile, ids, fake


@pytest.fixture
def held(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Held:
    seed_cli_store(cli_env[1])
    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    try:
        ids = {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
        created = ASOF - timedelta(days=10)
        save_thesis(
            sql, thesis(security_id=ids["RELI"], created_at=created,
                        review_date=created + timedelta(days=90)),
        )  # fmt: skip
    finally:
        sql.close()
    fake = script(holding_review=reviewer)
    monkeypatch.setattr(runtime, "query", fake)
    cfg = Path(cli_env[0][1])
    return Held(load_settings(cfg / "nivesh.yaml"), load_profile(cfg / "profile.yaml"), ids, fake)


def runs(env: Env) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(env[1] / "nivesh.sqlite")
    try:
        return c.execute("SELECT command, status, tier, started_at, run_dir FROM run").fetchall()
    finally:
        c.close()


def test_reviews_are_returned_and_ledger_rows_written(cli_env: Env, held: Held) -> None:
    out = run_holding_reviews(held.settings, held.profile, ASOF, clock=lambda: CLOCK)
    assert [i.target.symbol for i in out.items] == ["RELI"] and len(out.reviews) == 1
    assert out.missing == ("US1",) and out.warning is None and out.run_id == 1
    assert runs(cli_env) == [
        ("review holdings", "ok", "quick", to_iso(CLOCK), "runs/2026-01-02/1")
    ]  # fmt: skip
    c = sqlite3.connect(cli_env[1] / "nivesh.sqlite")
    assert c.execute("SELECT source, run_id FROM ledger_entry").fetchall() == [
        ("holding_review", 1)
    ]  # fmt: skip


def test_a_holding_without_a_thesis_is_listed_and_costs_nothing(cli_env: Env, held: Held) -> None:
    out = run_holding_reviews(held.settings, held.profile, ASOF, only="US1")
    assert out.items == () and out.reviews == () and out.run_id == 0
    assert out.missing == ("US1",) and held.fake.seen == [] and runs(cli_env) == []
    with pytest.raises(NiveshError, match="no security matches"):
        run_holding_reviews(held.settings, held.profile, ASOF, only="NOSUCH")


def test_the_stores_are_closed_before_the_agent_is_asked(
    cli_env: Env, held: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[bool] = []

    def hook(call: Call) -> Any:
        open_duck(cli_env[1] / "nivesh.duckdb").close()  # a writer can get in
        opened.append(True)
        return reviewer(call)

    monkeypatch.setattr(runtime, "query", script(holding_review=hook))
    run_holding_reviews(held.settings, held.profile, ASOF)
    assert opened == [True]


def test_the_cost_gate_refuses_before_any_run_row(cli_env: Env, held: Held) -> None:
    spent = sqlite3.connect(cli_env[1] / "nivesh.sqlite", isolation_level=None)
    spent.execute(
        "INSERT INTO run (command, started_at, status, cost_inr) VALUES ('x', ?, 'ok', 99999)",
        (to_iso(CLOCK),),
    )
    spent.close()
    profile = held.profile.model_copy(update={"monthly_cost_cap": 1000})
    warned: list[str] = []
    out = run_holding_reviews(
        held.settings, profile, ASOF, clock=lambda: CLOCK, warn=warned.append
    )  # a quick run past the cap is only warned about when someone is there
    assert len(out.reviews) == 1 and "cap reached" in warned[0]
    n = len(runs(cli_env))
    seen = len(held.fake.seen)
    with pytest.raises(BudgetRefused, match="stop at the cap"):
        run_holding_reviews(held.settings, profile, ASOF, clock=lambda: CLOCK, strict=True)
    assert len(runs(cli_env)) == n and len(held.fake.seen) == seen  # no row, no agent call


def test_a_ledger_failure_is_a_warning_and_the_reviews_still_come_back(
    cli_env: Env, held: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("disk")

    monkeypatch.setattr(svc, "record_calls", boom)
    out = run_holding_reviews(held.settings, held.profile, ASOF)
    assert out.warning == "the ledger rows were not saved (OperationalError)"
    assert len(out.reviews) == 1 and runs(cli_env)[0][1] == "ok"
