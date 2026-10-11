import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.report import (
    FOOTER_HEAD,
    Num,
    Report,
    from_json,
    html_text,
    to_html,
    to_json,
    to_markdown,
)
from tests.report_fx import RUN_AT, base_report

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "reports"
CR4 = (
    "Personal research generated with AI for the owner’s own decisions. "
    "Not investment advice. Data as of 2026-01-01, 2026-01-02."
)  # PID CR-4, pinned as a literal; <dates> is the sorted as-of set


def test_report_has_every_pid_part_in_both_formats() -> None:
    r = base_report()
    md, page = to_markdown(r), to_html(r)
    for text in (md, html_text(page)):
        assert "Example Energy research note" in text
        assert "Run time: 2026-10-12 01:30 IST (run 7)" in text
        assert "Data as of: 2026-01-01, 2026-01-02" in text
        assert "Verdict is HOLD" in text and "(none given)" in text
        assert "Investment thesis" in text and "Data gaps" in text and "Sources" in text
        assert "No news feed loaded" in text and "engine fa_compute" in text
        assert CR4 in text
    assert "| Name | Verdict | Weight |" in md
    assert "<table>" in page and "<th>Verdict</th>" in page
    assert re.search(r"^5\. \(none given\)$", md, re.M)


def test_summary_must_be_five_lines_or_pads_with_none_given_never_longer() -> None:
    assert len(base_report().summary_lines) == 5
    assert base_report(summary=()).summary_lines == ("(none given)",) * 5
    with pytest.raises(ValueError, match="at most 5"):
        base_report(summary=tuple(f"line {i}" for i in range(6)))


def test_footer_is_the_cr4_text_verbatim() -> None:
    assert base_report().footer == CR4
    assert CR4.startswith(FOOTER_HEAD)
    assert base_report(as_of=()).footer.endswith("Data as of n/a.")


def test_html_is_self_contained_no_script_no_link_no_remote_url() -> None:
    page = to_html(base_report())
    assert page.startswith("<!DOCTYPE html>") and page.count("<style>") == 1
    for banned in ("<script", "<link", "<img", "<iframe", "href=", "src=", "http://", "https://"):
        assert banned not in page, banned


def test_html_escapes_script_and_javascript_payloads() -> None:
    evil = "<script>alert(1)</script> javascript:alert(2) <a href='x'>"
    r = base_report(
        title=evil, summary=(evil,), gaps=(evil,), sources=(evil,),
        blocks=(__import__("nivesh_adapters.report", fromlist=["Block"]).Block("b", evil, evil),),
    )  # fmt: skip
    page = to_html(r)
    assert "<script" not in page and "<a href" not in page and "&lt;script&gt;" in page
    assert "<a href" not in to_html(base_report(banner="X", unmatched=(evil,)))


def test_control_characters_are_stripped() -> None:
    r = base_report(summary=("a\x00b\x1b[31mred\x07",), title="T\x00itle")
    for text in (to_markdown(r), to_html(r)):
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text)
    assert "Title" in to_markdown(r) and "ab[31mred" in to_markdown(r)


def test_run_time_rendered_in_ist_from_a_utc_value() -> None:
    late = base_report(run_at=datetime(2026, 1, 1, 18, 30, tzinfo=UTC))
    assert "2026-01-02 00:00 IST" in to_markdown(late)  # date rolls over in IST


def test_markdown_and_html_carry_the_same_numbers() -> None:
    r = base_report()
    nums = re.compile(r"\d[\d,]*(?:\.\d+)?")
    md = re.sub(r"(?m)^\d\. ", "", to_markdown(r))  # the HTML list has no numeric markers
    assert sorted(nums.findall(md)) == sorted(nums.findall(html_text(to_html(r))))


def test_num_registers_value_text_and_decimals() -> None:
    n = Num.show(Decimal("12.345"), 2, "%")
    assert (n.value, n.text, n.decimals, n.unit) == (Decimal("12.35"), "12.35%", 2, "%")
    c = Num.inr(Decimal("12000000"))
    assert (c.value, c.text, c.unit) == (Decimal("1.20"), "₹1.20 crore", "crore")
    assert Num.inr(Decimal("-250000")).value == Decimal("-2.50")
    assert (
        Num.inr(Decimal("1234.5")).unit == ""
        and Num.show(Decimal("5"), 1, "lakh").text == "5.0 lakh"
    )


def test_json_roundtrip_is_exact() -> None:
    r = base_report(banner="DRAFT - UNVERIFIED NUMBERS", unmatched=("7.5 in x",))
    text = to_json(r)
    assert from_json(text) == r and to_json(from_json(text)) == text
    assert '"value": "1.20"' in text and not re.search(r"\d{9}", text)


def test_golden_markdown_and_html_match_fixture() -> None:
    r = base_report()
    assert to_markdown(r) == (FIX / "base.md").read_text()
    assert to_html(r) == (FIX / "base.html").read_text()
    assert isinstance(r, Report) and RUN_AT.tzinfo is not None and date(2026, 1, 1) in r.as_of
