from datetime import UTC, date, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError

import nivesh_mcp.common as common
from nivesh_core.market_store import NewsRow, write_events, write_news
from nivesh_core.pii_scan import scan_text
from nivesh_mcp.base import is_write_name
from nivesh_mcp.registry import SERVERS
from tests.mcp.mkt_fx import Mkt, call

TOOLS = ["get_news", "get_events_calendar", "get_next_results_date"]
T0 = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)


def item(sid: int | None, hours: int, title: str, **kw: object) -> NewsRow:
    base = dict(kind="news", published_at=T0 + timedelta(hours=hours), source="wire", title=title,
                url="https://news.example.invalid/a/1", summary="Short summary.",
                event_type="results", materiality="high", sentiment="positive",
                classified_by="rules", security_id=sid)  # fmt: skip
    base.update(kw)
    return NewsRow(**base)  # type: ignore[arg-type]


def seed_news(m: Mkt) -> None:
    r, t = m.ids["RELIANCE"], m.ids["TCS"]
    rows = [item(r, 0, "Reliance posts record profit"), item(t, 1, "TCS wins deal"),
            item(r, 2, "Reliance faces probe", event_type="litigation", sentiment="negative"),
            item(None, 3, "Markets rally"),
            item(r, 4, "Board meeting intimation", kind="announcement")]  # fmt: skip
    with m.duck() as c:
        write_news(c, rows)


def test_tools_exact_set() -> None:
    assert SERVERS["news"].tool_names == TOOLS
    assert not [t for t in TOOLS if is_write_name(t)]


async def test_get_news_filters_newest_first_with_classification_fields(mkt: Mkt) -> None:
    seed_news(mkt)
    allr = await call("news", "get_news")
    titles = [n["published_at"] for n in allr["data"]["items"]]
    assert titles == sorted(titles, reverse=True) and len(titles) == 4  # announcements excluded
    one = await call("news", "get_news", security="RELIANCE")
    items = one["data"]["items"]
    assert len(items) == 2 and items[0]["event_type"] == "litigation"
    assert items[0]["materiality"] == "high" and items[0]["source"] == "wire"
    assert items[0]["sentiment"] == "negative" and items[0]["published_at"].endswith("+00:00")
    since = await call("news", "get_news", since="2026-01-05")
    assert len(since["data"]["items"]) == 4
    later = await call("news", "get_news", since="2026-01-06")
    assert later["data"]["items"] == []
    assert one["as_of"] == items[0]["published_at"]


async def test_get_news_text_is_wrapped_untrusted(mkt: Mkt) -> None:
    seed_news(mkt)
    out = await call("news", "get_news", security="TCS")
    n = out["data"]["items"][0]
    assert n["text"].startswith("<untrusted-data id=") and "TCS wins deal" in n["text"]
    assert "Short summary." in n["text"] and n["text"].rstrip().endswith('">')
    assert n["title_ref"] == f"news {n['news_id']}" and "title" not in n


async def test_get_news_limit_capped_and_paginated(mkt: Mkt) -> None:
    with mkt.duck() as c:
        write_news(c, [item(mkt.ids["TCS"], i, f"Story number {i} unique") for i in range(620)])
    got, cursor = 0, 0
    while True:
        out = await call("news", "get_news", limit=100000, cursor=cursor)
        assert len(out["data"]["items"]) <= 500
        got += len(out["data"]["items"])
        if out.get("next_cursor") is None:
            break
        cursor = out["next_cursor"]
    assert got == 620
    with pytest.raises(ToolError, match="since must be an ISO date"):
        await call("news", "get_news", since="yesterday")


async def test_get_events_calendar_window_days_in_ist(
    mkt: Mkt, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = mkt.ids["RELIANCE"]
    events = [(sid, "results", date(2026, 1, 5)), (sid, "results", date(2026, 1, 16)),
              (mkt.ids["TCS"], "results", date(2026, 3, 1))]  # fmt: skip
    with mkt.duck() as c:
        write_events(c, events, "bse", date(2026, 1, 1))
    out = await call("news", "get_events_calendar", window_days=14)
    assert [(e["symbol"], e["event_date"]) for e in out["data"]["events"]] == [
        ("RELIANCE", "2026-01-05"), ("RELIANCE", "2026-01-16")]  # fmt: skip
    assert out["data"]["from"] == "2026-01-05" and out["data"]["to"] == "2026-01-19"
    # 20:00Z on 5 Jan is already 6 Jan in IST: the 5 Jan event has passed
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 1, 5, 20, 0, tzinfo=UTC))
    late = await call("news", "get_events_calendar", window_days=14)
    assert late["data"]["from"] == "2026-01-06"
    assert [e["event_date"] for e in late["data"]["events"]] == ["2026-01-16"]
    one = await call("news", "get_events_calendar", security="TCS", window_days=90)
    assert [e["symbol"] for e in one["data"]["events"]] == ["TCS"]
    capped = await call("news", "get_events_calendar", window_days=100000)
    assert capped["data"]["to"] == "2027-01-07"  # 366 days after 6 Jan IST


async def test_get_next_results_date_future_only_in_ist_and_none_with_reason_when_unknown(
    mkt: Mkt,
) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        write_events(c, [(sid, "results", date(2025, 10, 10)), (sid, "results", date(2026, 1, 16)),
                         (sid, "results", date(2026, 4, 20))], "bse", date(2026, 1, 1))  # fmt: skip
    out = await call("news", "get_next_results_date", security="RELIANCE")
    assert out["data"]["next_results_date"] == "2026-01-16" and out["data"]["days_until"] == 11
    none = await call("news", "get_next_results_date", security="TCS")
    assert none["data"]["next_results_date"] is None and none["data"]["reason"]


async def test_next_results_date_on_ist_date_boundary(
    mkt: Mkt, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = mkt.ids["RELIANCE"]
    with mkt.duck() as c:
        write_events(c, [(sid, "results", date(2026, 1, 5)), (sid, "results", date(2026, 1, 9))],
                     "bse", date(2026, 1, 1))  # fmt: skip
    # 5 Jan 20:00Z == 6 Jan 01:30 IST: 5 Jan is in the past in India
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 1, 5, 20, 0, tzinfo=UTC))
    out = await call("news", "get_next_results_date", security="RELIANCE")
    assert out["data"]["next_results_date"] == "2026-01-09" and out["data"]["days_until"] == 3
    monkeypatch.setattr(common, "now", lambda: datetime(2026, 1, 5, 17, 0, tzinfo=UTC))  # 5 Jan IST
    out = await call("news", "get_next_results_date", security="RELIANCE")
    assert out["data"]["next_results_date"] == "2026-01-05" and out["data"]["days_until"] == 0


async def test_payload_survives_redaction(mkt: Mkt) -> None:
    seed_news(mkt)
    out = await call("news", "get_news")
    assert "[REDACTED]" not in str(out) and scan_text(str(out)) == []
    n = out["data"]["items"][0]
    assert n["url"] == "https://news.example.invalid/a/1"


async def test_get_news_url_and_source_are_validated_and_capped(mkt: Mkt) -> None:
    sid = mkt.ids["TCS"]
    bad = ["javascript:alert(1)", "https://x.example.invalid/" + "a" * 600, "data:text/html,hi"]
    with mkt.duck() as c:
        write_news(c, [item(sid, i, f"t{i}", url=u) for i, u in enumerate(bad)]
                   + [item(sid, 9, "long", source="s" * 300)])  # fmt: skip
    items = (await call("news", "get_news", security="TCS"))["data"]["items"]
    long = next(i for i in items if i["source"].startswith("ss"))
    assert len(long["source"]) <= 103 and long["url"] == "https://news.example.invalid/a/1"
    assert [i["url"] for i in items if i is not long] == [None, None, None]
