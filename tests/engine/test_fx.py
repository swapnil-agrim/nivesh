from datetime import date
from decimal import Decimal
from pathlib import Path

import nivesh_engine.fx as fx
from nivesh_engine.fx import STALE_DAYS, FxResult, convert_holdings, rate_on_or_before
from tests.holdings_fx import holding
from tests.us_fx import D, usd_holding, usdinr_obs

SRC = "fred:DEXINUS"


def test_rate_on_or_before_uses_latest_observation_not_after() -> None:
    obs = usdinr_obs(("2026-01-02", "83.0"), ("2026-01-05", "84.0"), ("2026-01-06", "99.0"))
    r = rate_on_or_before(obs, date(2026, 1, 5), SRC)
    assert r.available and r.rate == D("84.0") and r.rate_date == date(2026, 1, 5) and not r.stale


def test_weekend_valuation_uses_friday_rate_and_reports_its_date() -> None:
    obs = usdinr_obs(("2026-01-09", "83.5"))  # a Friday
    r = rate_on_or_before(obs, date(2026, 1, 11), SRC)
    assert r.rate == D("83.5") and r.rate_date == date(2026, 1, 9) and not r.stale


def test_missing_series_is_unavailable_with_reason_never_zero() -> None:
    r = rate_on_or_before([], date(2026, 1, 5), SRC)
    assert not r.available and r.rate is None and r.rate_date is None
    assert r.reason == "no USDINR observation on or before 2026-01-05"


def test_observation_only_after_date_is_unavailable() -> None:
    r = rate_on_or_before(usdinr_obs(("2026-02-01", "83")), date(2026, 1, 5), SRC)
    assert not r.available and r.rate is None


def test_non_positive_observation_ignored() -> None:
    obs = usdinr_obs(("2026-01-02", "83"), ("2026-01-05", "0"), ("2026-01-04", "-1"))
    r = rate_on_or_before(obs, date(2026, 1, 5), SRC)
    assert r.rate == D(83) and r.rate_date == date(2026, 1, 2)


def test_stale_after_threshold_still_available_and_flagged() -> None:
    on = date(2026, 1, 20)
    fresh = rate_on_or_before(usdinr_obs(("2026-01-10", "83")), on, SRC)
    assert fresh.available and not fresh.stale  # exactly STALE_DAYS old
    assert STALE_DAYS == 10
    old = rate_on_or_before(usdinr_obs(("2026-01-09", "83")), on, SRC)
    assert old.available and old.stale


def test_max_age_cap_makes_old_rate_unavailable() -> None:
    obs = usdinr_obs(("2026-01-01", "83"))
    ok = rate_on_or_before(obs, date(2026, 1, 8), SRC, max_age_days=7)
    assert ok.available
    r = rate_on_or_before(obs, date(2026, 1, 9), SRC, max_age_days=7)
    assert not r.available and r.rate is None and r.reason is not None and "7" in r.reason


def test_source_label_names_fred_and_states_rbi_not_wired() -> None:
    r = rate_on_or_before(usdinr_obs(("2026-01-05", "83")), date(2026, 1, 5), "fred:DEXINUS")
    assert r.source == "fred:DEXINUS (RBI reference rate not wired)"


def test_convert_holdings_sets_value_inr_for_usd_only() -> None:
    fxr = rate_on_or_before(usdinr_obs(("2026-01-05", "83")), date(2026, 1, 5), SRC)
    inr, usd = holding(), usd_holding(quantity=D(2), price=D(10))
    out = convert_holdings([inr, usd], fxr)
    assert out[0] is inr and out[1].value_inr == D(1660)


def test_convert_holdings_with_unavailable_rate_leaves_none_and_never_zero() -> None:
    fxr = rate_on_or_before([], date(2026, 1, 5), SRC)
    (out,) = convert_holdings([usd_holding()], fxr)
    assert out.value_inr is None


def test_inr_rows_untouched() -> None:
    h = holding()
    assert convert_holdings(
        [h], FxResult(rate=None, rate_date=None, source="s", stale=False, reason="x")
    ) == [h]


def test_decimal_exact_no_float() -> None:
    fxr = rate_on_or_before(usdinr_obs(("2026-01-05", "83.1234")), date(2026, 1, 5), SRC)
    (out,) = convert_holdings([usd_holding(quantity=D("1234.56"), price=D(1))], fxr)
    assert out.value_inr == Decimal("1234.56") * Decimal("83.1234")


def test_config_usd_inr_is_never_consulted() -> None:
    text = Path(fx.__file__).read_text()
    assert "Settings" not in text and "usd_inr" not in text and "float(" not in text
