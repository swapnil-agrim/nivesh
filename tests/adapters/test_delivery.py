"""Message build and the personal-data gate."""

from pathlib import Path

import pytest

from nivesh_adapters.delivery import (
    Outcome,
    blocked_reason,
    build_message,
    build_transports,
    send_message,
    slack_escape,
)
from nivesh_adapters.report import Block, to_html
from nivesh_core.delivery_config import DeliverySettings
from tests.delivery_fx import CFG, Calls, FakeSmtp, message, set_refs, trace_text, tracer
from tests.report_fx import base_report


def test_message_is_summary_plus_decision_table_plus_footer() -> None:
    m = message()
    for needle in ("1. Verdict is HOLD", "Name | Verdict | Weight", "Example Energy | HOLD | 3%"):
        assert needle in m.text, needle
    assert m.text.rstrip().endswith("Data as of 2026-01-01, 2026-01-02.")
    assert "Not investment advice." in m.text
    assert "Steady cash flow" not in m.text  # body blocks stay in the page


def test_html_attached_or_local_path_noted() -> None:
    r = base_report()
    assert "Full report: runs/2026-10-12/7/report.html" in message().text
    assert build_message(r, to_html(r)).text.count("Full report attached.") == 1
    assert message().html.startswith("<!DOCTYPE html>")


def test_subject_has_title_and_date_no_numbers_beyond_header() -> None:
    assert message().subject == "Example Energy research note (2026-10-12)"


def test_draft_banner_leads_the_text() -> None:
    m = message(banner="DRAFT - UNVERIFIED NUMBERS", unmatched=("x",))
    assert m.text.startswith("DRAFT - UNVERIFIED NUMBERS")


def test_pii_in_body_blocks_that_channel_and_logs_kinds_and_positions_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_refs(monkeypatch)
    leak = "reach me " + "someone" + "@" + "example" + ".test"
    m = message(summary=(leak,))
    calls = Calls()
    t = build_transports(CFG, calls.client(), FakeSmtp().factory)
    out = send_message(m, t, CFG, tracer(tmp_path))
    assert [o.status for o in out] == ["blocked", "blocked", "blocked"]
    assert calls.requests == []
    log = trace_text(tmp_path)
    assert "email@" in log and "someone" not in log and "example.test" not in log


def test_pii_in_html_attachment_blocks_send(monkeypatch: pytest.MonkeyPatch) -> None:
    set_refs(monkeypatch)
    m = message()
    leak = "x" + "@" + "example" + ".test"
    m = type(m)(m.subject, m.text, m.html + leak)
    smtp, calls = FakeSmtp(), Calls()
    out = {
        o.channel: o
        for o in send_message(m, build_transports(CFG, calls.client(), smtp.factory), CFG)
    }
    assert out["telegram"].status == "blocked" and out["email"].status == "blocked"
    assert out["slack"].status == "sent"  # chat text only: the page is not sent there
    assert smtp.sent == [] and len(calls.requests) == 1


def test_blocked_channel_does_not_block_the_other_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    set_refs(monkeypatch)
    m = message()
    leak = "123456" + "789012"
    m = type(m)(m.subject, m.text, m.html + leak)
    calls = Calls()
    out = send_message(m, build_transports(CFG, calls.client(), FakeSmtp().factory), CFG)
    assert [(o.channel, o.status) for o in out] == [
        ("telegram", "blocked"), ("slack", "sent"), ("email", "blocked"),
    ]  # fmt: skip


def test_empty_channels_sends_nothing_and_opens_no_socket() -> None:
    calls, smtp = Calls(), FakeSmtp()
    cfg = DeliverySettings()
    assert send_message(message(), build_transports(cfg, calls.client(), smtp.factory), cfg) == []
    assert calls.requests == [] and smtp.hosts == []


def test_markup_in_model_text_is_escaped_for_telegram_and_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_refs(monkeypatch)
    m = message(summary=("<!channel> see <https://x.test|click> & go",))
    calls = Calls()
    send_message(m, build_transports(CFG, calls.client(), FakeSmtp().factory), CFG)
    by = {r.url.host: r for r in calls.requests}
    slack = by["hooks.example.test"].content.decode()
    assert "<!channel>" not in slack and "&lt;!channel&gt;" in slack and "&amp; go" in slack
    tg = by["api.telegram.org"]
    assert b"parse_mode" not in tg.content  # plain text: nothing for Telegram to interpret
    assert slack_escape("a<b>&") == "a&lt;b&gt;&amp;"


def test_digit_run_in_report_never_reaches_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    set_refs(monkeypatch)
    m = message(summary=("balance " + "1234" + "56789" + "012",))
    assert blocked_reason(m, page=False).startswith("text:digits@")
    calls = Calls()
    out = send_message(m, build_transports(CFG, calls.client(), FakeSmtp().factory), CFG)
    assert all(isinstance(o, Outcome) and o.status == "blocked" for o in out)
    assert calls.requests == []


def test_blocks_text_is_not_part_of_the_chat_message() -> None:
    m = message(blocks=(Block("t", "Thesis", "Body sentence only in page."),))
    assert "Body sentence" not in m.text and "Body sentence" in m.html
