from datetime import UTC, datetime, timedelta

from nivesh_engine.dedupe import Item, canonical_url, dedupe, normalise_title, title_similarity

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def item(title: str, url: str = "https://a.example/x", hours: float = 0, source: str = "A") -> Item:
    return Item(title=title, url=url, published=T0 + timedelta(hours=hours), source=source)


def test_canonical_url_strips_utm_fragment_trailing_slash_and_lowercases_host() -> None:
    assert (
        canonical_url("HTTPS://News.Example.COM/Story/1/?utm_source=x&id=7&utm_medium=y#top")
        == "https://news.example.com/Story/1?id=7"
    )
    assert canonical_url("https://a.example/") == "https://a.example/"
    assert canonical_url("") == ""
    assert canonical_url("https://a.example/p?b=2&a=1") == "https://a.example/p?a=1&b=2"


def test_same_canonical_url_deduped() -> None:
    a = item("Alpha posts profit", "https://a.example/s?utm_source=feed")
    b = item("Totally different words", "https://a.example/s/", hours=300, source="B")
    out = dedupe([a, b])
    assert len(out.kept) == 1 and out.kept[0].item == a and out.kept[0].also == ["B"]


def test_similar_titles_within_48h_deduped_above_threshold_0_9() -> None:
    a = item("Alpha Industries reports record quarterly profit", "https://a.example/1")
    b = item("Alpha Industries reports record quarterly profit!", "https://b.example/2", 5, "B")
    out = dedupe([b, a])
    assert [k.item for k in out.kept] == [a]  # earliest published kept, whatever the input order


def test_similar_titles_far_apart_in_time_kept() -> None:
    a = item("Alpha Industries reports record quarterly profit", "https://a.example/1")
    b = item("Alpha Industries reports record quarterly profit", "https://b.example/2", 49)
    assert len(dedupe([a, b]).kept) == 2


def test_title_suffix_source_name_stripped_before_compare() -> None:
    assert normalise_title("Alpha wins big order - Example Times", "Example Times") == (
        "alpha wins big order"
    )
    assert normalise_title("Alpha wins big order | Example Times", "example times") == (
        "alpha wins big order"
    )
    a = item("Alpha wins big order", "https://a.example/1", 0, "Example Times")
    b = item("Alpha wins big order - Wire Daily", "https://b.example/2", 1, "Wire Daily")
    assert len(dedupe([a, b]).kept) == 1


def test_distinct_titles_kept() -> None:
    a = item("Alpha posts quarterly profit", "https://a.example/1")
    b = item("Beta announces acquisition of Gamma", "https://b.example/2", 1)
    assert len(dedupe([a, b]).kept) == 2
    assert title_similarity("abc", "abc") == 1.0 and title_similarity("", "") == 0.0


def test_dedupe_against_existing_stored_items() -> None:
    old = item("Alpha posts quarterly profit", "https://a.example/1")
    new = item("Alpha posts quarterly profit", "https://b.example/2", 2, "B")
    fresh = item("Beta wins order", "https://c.example/3", 3)
    out = dedupe([new, fresh], existing=[old])
    assert [k.item for k in out.kept] == [fresh] and out.dropped == 1


def test_dedupe_keeps_earliest_published_and_records_other_sources() -> None:
    items = [
        item("Alpha posts quarterly profit", "https://a.example/1", 2, "C"),
        item("Alpha posts quarterly profit", "https://b.example/2", 0, "A"),
        item("Alpha posts quarterly profit", "https://c.example/3", 1, "B"),
        item("Alpha posts quarterly profit", "https://d.example/4", 3, "A"),
    ]
    out = dedupe(items)
    assert len(out.kept) == 1 and out.kept[0].item.source == "A"
    assert out.kept[0].also == ["B", "C"]


def test_announcements_of_different_securities_are_not_merged() -> None:
    def ann(title: str, sid: int | None, src: str) -> Item:
        return Item(title, "", T0, src, security_id=sid, kind="announcement")

    a = ann("TCS - Board Meeting Intimation", 1, "BSE")
    b = ann("TCI - Board Meeting Intimation", 2, "NSE")
    assert len(dedupe([a, b]).kept) == 2
    # unresolved security on both sides: no safe basis to merge by title
    assert len(dedupe([ann("TCS - Board Meeting Intimation", None, "BSE"),
                       ann("TCI - Board Meeting Intimation", None, "NSE")]).kept) == 2  # fmt: skip
    # same security, same story: merged
    same = dedupe([ann("TCS - Board Meeting Intimation", 1, "BSE"),
                   ann("TCS - Board Meeting Intimation", 1, "NSE")])  # fmt: skip
    assert len(same.kept) == 1 and same.kept[0].also == ["NSE"]
