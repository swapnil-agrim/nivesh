import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from nivesh_adapters.base import Adapter
from nivesh_adapters.cache import cached_fetch
from nivesh_adapters.quality import DataQualityError
from nivesh_core.config import Ttls
from nivesh_core.db import MIGRATIONS, migrate

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


class Fake(Adapter):
    name = "fake"
    source = "fake-feed"

    def __init__(self) -> None:
        self.calls = 0
        self.fail: Exception | None = None
        self.payload: Any = {"price": 1}

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.payload, T0

    def validate(self, data: Any) -> None:
        if data.get("price", 0) < 0:
            raise DataQualityError(self.name, "price", data["price"], "negative")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = duckdb.connect(str(tmp_path / "c.duckdb"))
    migrate.apply(c, MIGRATIONS / "duck")
    yield c
    c.close()


def get(db: Any, ad: Fake, clock: Clock, dtype: str = "price", **kw: Any) -> Any:
    params = kw.pop("params", {"s": "X"})
    return cached_fetch(ad, params, dtype, conn=db, ttls=Ttls(), now=clock, **kw)  # type: ignore[arg-type]


def test_within_ttl_no_network(db: Any) -> None:
    ad, clock = Fake(), Clock()
    get(db, ad, clock)
    clock.t += timedelta(hours=23)
    r = get(db, ad, clock)
    assert ad.calls == 1 and r.data == {"price": 1} and r.stale is False


def test_after_ttl_refetches(db: Any) -> None:
    ad, clock = Fake(), Clock()
    get(db, ad, clock)
    clock.t += timedelta(days=1, seconds=1)
    ad.payload = {"price": 2}
    assert get(db, ad, clock).data == {"price": 2} and ad.calls == 2


def test_refetch_failure_returns_stale_flagged(db: Any) -> None:
    ad, clock = Fake(), Clock()
    get(db, ad, clock)
    clock.t += timedelta(days=2)
    ad.fail = RuntimeError("down")
    r = get(db, ad, clock)
    assert r.stale is True and r.data == {"price": 1} and r.source == "fake-feed"


def test_failure_without_entry_reraises(db: Any) -> None:
    ad = Fake()
    ad.fail = RuntimeError("down")
    with pytest.raises(RuntimeError):
        get(db, ad, Clock())


def test_refresh_arg_bypasses_read_but_writes(db: Any) -> None:
    ad, clock = Fake(), Clock()
    get(db, ad, clock)
    ad.payload = {"price": 9}
    assert get(db, ad, clock, refresh=True).data == {"price": 9} and ad.calls == 2
    assert get(db, ad, clock).data == {"price": 9} and ad.calls == 2


def test_env_refresh_bypasses(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ad, clock = Fake(), Clock()
    get(db, ad, clock)
    monkeypatch.setenv("NIVESH_REFRESH", "1")
    get(db, ad, clock)
    assert ad.calls == 2


def test_param_order_is_irrelevant_and_params_matter(db: Any) -> None:
    ad, clock = Fake(), Clock()
    get(db, ad, clock, params={"a": 1, "b": 2})
    get(db, ad, clock, params={"b": 2, "a": 1})
    assert ad.calls == 1
    get(db, ad, clock, params={"a": 1, "b": 3})
    assert ad.calls == 2


@pytest.mark.parametrize(
    ("dtype", "days"), [("price", 1), ("fundamentals", 7), ("nav", 1), ("mf_holdings", 31)]
)
def test_ttl_by_data_type(db: Any, dtype: str, days: int) -> None:
    ad, clock = Fake(), Clock()
    get(db, ad, clock, dtype)
    clock.t += timedelta(days=days) - timedelta(seconds=1)
    get(db, ad, clock, dtype)
    assert ad.calls == 1
    clock.t += timedelta(seconds=2)
    get(db, ad, clock, dtype)
    assert ad.calls == 2


def test_unknown_data_type(db: Any) -> None:
    with pytest.raises(ValueError, match="data type"):
        get(db, Fake(), Clock(), "bogus")


def test_quality_failure_is_not_cached_nor_masked_by_stale(db: Any) -> None:
    ad, clock = Fake(), Clock()
    ad.payload = {"price": -1}
    with pytest.raises(DataQualityError):
        get(db, ad, clock)
    assert db.execute("select count(*) from cache_entry").fetchone() == (0,)
    ad.payload = {"price": 1}
    get(db, ad, clock)
    clock.t += timedelta(days=2)
    ad.payload = {"price": -1}
    with pytest.raises(DataQualityError):  # bad fresh data must not silently fall back to stale
        get(db, ad, clock)


def test_pii_redacted_before_caching(db: Any) -> None:
    ad, clock = Fake(), Clock()
    ad.payload = {"price": 1, "pan": "ABCDE1234F", "note": "acct 123456789012"}
    fresh = get(db, ad, clock)
    assert fresh.data["pan"] == "ABCDE1234F"  # caller of a live fetch sees the real data
    (payload,) = db.execute("select payload from cache_entry").fetchone()
    assert "ABCDE1234F" not in payload and "123456789012" not in payload


def _market_payload() -> dict[str, Any]:
    long_value = "1" + "2" * 11  # 12-digit value carried as a string
    cik = "0" + "0012" + "3456" + "7"
    return {
        "value": long_value,
        "url": "https://example.test/Archives/" + cik + "/" + "1" * 18 + "/doc.htm",
        "cik": cik,
        "nested": {"key": "kept"},
    }


def test_market_payload_survives_cache_hit_twice(db: duckdb.DuckDBPyConnection) -> None:
    f, clock = Fake(), Clock()
    f.payload = _market_payload()
    first = cached_fetch(f, {"a": 1}, "price", conn=db, ttls=Ttls(), redact=False, now=clock)
    second = cached_fetch(f, {"a": 1}, "price", conn=db, ttls=Ttls(), redact=False, now=clock)
    third = cached_fetch(f, {"a": 1}, "price", conn=db, ttls=Ttls(), redact=False, now=clock)
    stored = json.loads(db.execute("SELECT payload FROM cache_entry").fetchone()[0])  # type: ignore[index]
    assert f.calls == 1
    assert first.data == second.data == third.data == stored == f.payload


def test_default_redact_true_still_masks_digit_runs_and_key_fields(
    db: duckdb.DuckDBPyConnection,
) -> None:
    f, clock = Fake(), Clock()
    f.payload = _market_payload()
    cached_fetch(f, {"a": 1}, "price", conn=db, ttls=Ttls(), now=clock)
    stored = json.loads(db.execute("SELECT payload FROM cache_entry").fetchone()[0])  # type: ignore[index]
    assert stored["value"] == "[REDACTED]" and stored["nested"]["key"] == "[REDACTED]"
