"""Telegram and Slack transports: wire shape, retry, and what never leaks."""

from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from nivesh_adapters.delivery import build_transports, send_message
from nivesh_core.delivery_config import DeliverySettings
from tests.delivery_fx import (
    CFG,
    DUMMIES,
    DUMMY_BOT,
    DUMMY_CHAT,
    DUMMY_HOOK,
    Calls,
    FakeSmtp,
    body_json,
    message,
    set_refs,
    trace_text,
    tracer,
)


def only(ch: str, **over: object) -> DeliverySettings:
    return CFG.model_copy(update={"channels": [ch], **over})


def run(
    calls: Calls,
    cfg: DeliverySettings,
    tmp_path: Path | None = None,
    sleeps: list[float] | None = None,
):  # type: ignore[no-untyped-def]
    sl = sleeps if sleeps is not None else []
    t = build_transports(cfg, calls.client(), FakeSmtp().factory)
    tr = tracer(tmp_path) if tmp_path else None
    return send_message(message(), t, cfg, tr, sleep=sl.append)


@pytest.fixture(autouse=True)
def refs(monkeypatch: pytest.MonkeyPatch) -> None:
    set_refs(monkeypatch)


def test_telegram_sends_message_then_document() -> None:
    calls = Calls()
    out = run(calls, only("telegram"))
    assert [o.status for o in out] == ["sent"]
    a, b = calls.requests
    assert a.url.path.endswith("/sendMessage") and b.url.path.endswith("/sendDocument")
    assert DUMMY_BOT in str(a.url) and DUMMY_CHAT in a.content.decode()
    assert (
        b"report.html" in b.content
        and "Example Energy research note" in parse_qs(a.content.decode())["text"][0]
    )


def test_slack_posts_summary_and_table_with_run_path_note() -> None:
    calls = Calls()
    out = run(calls, only("slack"))
    assert out[0].status == "sent" and str(calls.requests[0].url) == DUMMY_HOOK
    text = body_json(calls.requests[0])["text"]
    assert "Example Energy | HOLD | 3%" in text and "runs/2026-10-12/7/report.html" in text


def test_5xx_and_timeout_retry_with_backoff_up_to_max_attempts() -> None:
    calls = Calls([503, httpx.ReadTimeout("slow"), 200])
    sleeps: list[float] = []
    out = run(calls, only("slack", max_attempts=3), sleeps=sleeps)
    assert (out[0].status, out[0].attempts) == ("sent", 3) and sleeps == [1.0, 2.0]
    calls = Calls([500, 500, 500, 500])
    sleeps = []
    out = run(calls, only("slack", max_attempts=2), sleeps=sleeps)
    assert (out[0].status, out[0].attempts) == ("failed", 2) and sleeps == [1.0]
    assert len(calls.requests) == 2
    out = run(Calls([429, 200]), only("slack"))
    assert out[0].status == "sent"


def test_4xx_is_not_retried() -> None:
    calls = Calls([404])
    sleeps: list[float] = []
    out = run(calls, only("slack"), sleeps=sleeps)
    assert (out[0].status, out[0].attempts, out[0].detail) == ("failed", 1, "http 404")
    assert sleeps == [] and len(calls.requests) == 1


def test_each_failure_logged_to_trace_as_error_without_url_or_payload(tmp_path: Path) -> None:
    run(Calls([500, httpx.ConnectError("boom " + DUMMY_HOOK), 500]), only("slack"), tmp_path)
    log = trace_text(tmp_path)
    assert log.count('"type": "error"') == 3
    assert "attempt 2" in log and "ConnectError" in log
    assert not [d for d in DUMMIES if d in log] and "Example Energy" not in log


def test_final_failure_returns_result_object_does_not_raise_or_change_run_status() -> None:
    out = run(Calls([500] * 5), only("telegram"))
    assert out[0].status == "failed" and out[0].channel == "telegram"


def test_resolved_reference_values_never_traced_printed_or_in_the_error_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run(Calls([500, 500, 500]), only("telegram"), tmp_path)
    shown = repr(out) + trace_text(tmp_path) + capsys.readouterr().out
    assert not [d for d in DUMMIES if d in shown]


def test_missing_reference_error_names_the_ref_not_a_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("S_HOOK")
    out = run(Calls(), only("slack"), tmp_path)
    assert out[0].status == "failed" and "ref:S_HOOK" in out[0].detail
    assert "ref:S_HOOK" in trace_text(tmp_path)


def test_unresolved_refs_at_load_time_is_fine_resolution_happens_at_send() -> None:
    cfg = only("slack", slack_hook=None)
    out = run(Calls(), cfg)
    assert (out[0].status, out[0].attempts) == ("unconfigured", 1)


def test_record_mode_env_does_not_persist_delivery_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NIVESH_RECORD", "1")
    monkeypatch.delenv("NIVESH_REPLAY", raising=False)
    monkeypatch.chdir(tmp_path)
    calls = Calls()
    run(calls, only("telegram"))
    assert len(calls.requests) == 2  # sent through the injected client
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_telegram_retry_resends_only_the_document() -> None:
    calls = Calls([200, 500, 200])
    out = run(calls, only("telegram"))
    assert (out[0].status, out[0].attempts) == ("sent", 2)
    assert [r.url.path.rsplit("/", 1)[1] for r in calls.requests] == [
        "sendMessage", "sendDocument", "sendDocument",
    ]  # fmt: skip


def test_malformed_resolved_values_leak_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad_hook = "ht" + "tp://h.example.test:" + "bad-port" + "9z"  # a non-numeric port: InvalidURL
    bad_bot = "ab" + "cd" + "\x01" + "12" + "34"
    monkeypatch.setenv("S_HOOK", bad_hook)
    monkeypatch.setenv("T_BOT", bad_bot)
    caplog.set_level("DEBUG")
    for ch in ("slack", "telegram"):
        out = run(Calls(), only(ch), tmp_path)
        assert out[0].status == "failed" and out[0].detail
        shown = repr(out) + trace_text(tmp_path) + caplog.text
        assert "bad-port" not in shown and "abcd" not in shown and "1234" not in shown


def test_http_client_factory_keeps_request_urls_out_of_info_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from nivesh_cli.deliver import http_client

    with http_client(5):
        caplog.set_level("INFO")
        c = httpx.Client(transport=httpx.MockTransport(Calls()))
        c.post("https://example.test/" + "bot" + DUMMY_BOT)
    assert DUMMY_BOT not in caplog.text
