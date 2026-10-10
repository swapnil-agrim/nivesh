from datetime import date
from decimal import Decimal

from nivesh_engine.statements import (
    Fact,
    StatementRow,
    facts_from_companyfacts,
    latest_as_of,
    quarterise,
)
from tests.market_fx import companyfacts

D = Decimal


def rows_for(item: str, ptype: str, years: int = 2) -> dict[date, StatementRow]:
    rows = quarterise(facts_from_companyfacts(companyfacts(years=years))).rows
    return {r.period_end: r for r in rows if r.item == item and r.period_type == ptype}


def fact(
    concept: str, start: str | None, end: str, val: str, filed: str, form: str = "10-Q"
) -> Fact:
    return Fact(
        concept, "USD", date.fromisoformat(start) if start else None, date.fromisoformat(end),
        D(val), date.fromisoformat(filed), form,
    )  # fmt: skip


def test_annual_flow_from_10k_duration_about_one_year() -> None:
    a = rows_for("revenue", "A")
    assert a[date(2019, 12, 31)].value == D(sum(19000 + 100 * q for q in (1, 2, 3, 4)))
    assert a[date(2019, 12, 31)].filed_at == date(2020, 2, 15)


def test_ytd_cash_flow_differenced_into_discrete_quarters() -> None:
    q = rows_for("cfo", "Q")
    assert [q[date(2019, m, d)].value for m, d in ((3, 31), (6, 30), (9, 30))] == [
        D(960),
        D(970),
        D(980),
    ]


def test_q4_is_fy_minus_nine_month_ytd_with_10k_filed_at() -> None:
    q4 = rows_for("cfo", "Q")[date(2019, 12, 31)]
    assert q4.value == D(990) and q4.filed_at == date(2020, 2, 15)
    rq4 = rows_for("revenue", "Q")[date(2019, 12, 31)]
    assert rq4.value == D(19400)


def test_instant_items_use_end_date_for_quarter_and_year() -> None:
    assert rows_for("total_equity", "Q")[date(2019, 6, 30)].value == D(5191)
    a = rows_for("total_equity", "A")
    assert list(a) == [date(2019, 12, 31), date(2020, 12, 31)] and a[date(2019, 12, 31)].value == D(
        5193
    )


def test_restatement_keeps_both_filed_at_values() -> None:
    facts = [
        fact("NetIncomeLoss", "2020-01-01", "2020-12-31", "100", "2021-02-15", "10-K"),
        fact("NetIncomeLoss", "2020-01-01", "2020-12-31", "100", "2022-02-15", "10-K"),  # repeat
        fact("NetIncomeLoss", "2020-01-01", "2020-12-31", "90", "2023-02-15", "10-K/A"),
    ]
    rows = quarterise(facts).rows
    assert [(r.value, r.filed_at) for r in rows] == [
        (D(100), date(2021, 2, 15)), (D(90), date(2023, 2, 15)),
    ]  # fmt: skip


def test_latest_as_of_picks_max_filed_at_not_after_date() -> None:
    rows = quarterise(
        [
            fact("NetIncomeLoss", "2020-01-01", "2020-12-31", "100", "2021-02-15", "10-K"),
            fact("NetIncomeLoss", "2020-01-01", "2020-12-31", "90", "2023-02-15", "10-K/A"),
        ]
    ).rows
    assert latest_as_of(rows, date(2022, 1, 1))[0].value == D(100)
    assert latest_as_of(rows)[0].value == D(90)
    assert latest_as_of(rows, date(2021, 1, 1)) == []


def test_concept_alias_priority_revenues_then_contract_revenue() -> None:
    a = rows_for("revenue", "A", years=4)
    assert set(a) == {date(y, 12, 31) for y in (2019, 2020, 2021, 2022)}
    both = quarterise(
        [
            fact(
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "2020-01-01",
                "2020-12-31",
                "7",
                "2021-02-15",
                "10-K",
            ),
            fact("Revenues", "2020-01-01", "2020-12-31", "8", "2021-02-15", "10-K"),
        ]
    ).rows
    assert [r.value for r in both] == [D(8)]


def test_negative_capex_sign_normalised_to_positive_outflow() -> None:
    rows = quarterise(
        [
            fact(
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "2020-01-01",
                "2020-12-31",
                "-40",
                "2021-02-15",
                "10-K",
            )
        ]
    ).rows
    assert rows[0].item == "capex" and rows[0].value == D(40)


def test_six_year_fixture_yields_at_least_five_annual_and_twelve_quarterly_periods() -> None:
    assert len(rows_for("revenue", "A", years=6)) >= 5
    assert len(rows_for("revenue", "Q", years=6)) >= 12
    assert len(rows_for("cfo", "Q", years=6)) >= 12


def test_odd_durations_skipped_and_reported_and_eps_unit_currency() -> None:
    out = quarterise(
        [
            fact("NetIncomeLoss", "2020-01-01", "2020-02-15", "5", "2020-05-01"),
            Fact(
                "EarningsPerShareDiluted",
                "USD/shares",
                date(2020, 1, 1),
                date(2020, 3, 31),
                D("1.25"),
                date(2020, 5, 1),
                "10-Q",
            ),
        ]
    )
    assert out.skipped == ["net_income 2020-01-01..2020-02-15"]
    assert [(r.item, r.currency) for r in out.rows] == [("eps", "USD")]


def test_malformed_and_unmapped_facts_ignored() -> None:
    bad = [{"end": "bad", "val": 1, "filed": "2020-01-01"}, {"val": 1}]
    ok = [{"end": "2020-01-01", "val": 1, "filed": "2020-01-01"}]
    doc = {
        "facts": {
            "us-gaap": {"NetIncomeLoss": {"units": {"USD": bad}}, "Nope": {"units": {"USD": ok}}}
        }
    }
    assert facts_from_companyfacts(doc) == [] and facts_from_companyfacts({}) == []


# revisions (ST-4.9) ----------------------------------------------------------------------------


def snaps(*pairs: tuple[str, str]) -> list[tuple[date, Decimal]]:
    return [(date.fromisoformat(d), Decimal(v)) for d, v in pairs]


def test_revision_direction_up_down_flat_for_30_and_90_days() -> None:
    from nivesh_engine.statements import revision

    s = snaps(("2025-10-01", "10"), ("2025-12-01", "11"), ("2026-01-05", "11"))
    as_of = date(2026, 1, 5)
    assert revision(s, as_of, 30) == "flat"  # 2025-12-01 is the snapshot at/before 30d back
    assert revision(s, as_of, 90) == "up"
    assert revision(snaps(("2025-10-01", "12"), ("2026-01-05", "11")), as_of, 90) == "down"


def test_revision_unavailable_when_history_shorter_than_window() -> None:
    from nivesh_engine.statements import revision

    s = snaps(("2025-12-20", "10"), ("2026-01-05", "11"))
    assert revision(s, date(2026, 1, 5), 30) is None  # never a silent flat
    assert revision([], date(2026, 1, 5), 30) is None


def test_revision_uses_nearest_earlier_snapshot() -> None:
    from nivesh_engine.statements import revision

    s = snaps(("2025-09-01", "5"), ("2025-12-04", "10"), ("2026-01-05", "10"))
    assert revision(s, date(2026, 1, 5), 30) == "flat"  # not the 2025-09-01 value
    future = snaps(("2025-12-04", "10"), ("2026-02-01", "99"))
    assert revision(future, date(2026, 1, 5), 30) == "flat"  # later snapshots are not used
