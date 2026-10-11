"""Email transport over an injected mail connection."""

import smtplib
from pathlib import Path

import pytest

from nivesh_adapters.delivery import Outcome, build_transports, send_message
from nivesh_core.delivery_config import DeliverySettings
from nivesh_core.trace import Tracer
from tests.delivery_fx import (
    CFG,
    DUMMY_AUTH,
    DUMMY_FROM,
    DUMMY_HOST,
    DUMMY_TO,
    Calls,
    FakeSmtp,
    message,
    set_refs,
)

CFG1 = CFG.model_copy(update={"channels": ["email"]})


@pytest.fixture(autouse=True)
def refs(monkeypatch: pytest.MonkeyPatch) -> None:
    set_refs(monkeypatch)


def go(
    smtp: FakeSmtp, cfg: DeliverySettings = CFG1, sleeps: list[float] | None = None
) -> list[Outcome]:
    sl = sleeps if sleeps is not None else []
    return send_message(
        message(), build_transports(cfg, Calls().client(), smtp.factory), cfg, sleep=sl.append
    )


def test_email_has_text_body_and_html_attachment() -> None:
    smtp = FakeSmtp()
    assert go(smtp)[0].status == "sent"
    mail = smtp.sent[0]
    assert mail["Subject"] == "Example Energy research note (2026-10-12)"
    assert mail["To"] == DUMMY_TO and mail["From"] == DUMMY_FROM
    body = mail.get_body(preferencelist=("plain",))
    assert body is not None and "Example Energy | HOLD | 3%" in body.get_content()
    atts = list(mail.iter_attachments())
    assert [a.get_filename() for a in atts] == ["report.html"]
    assert atts[0].get_content_type() == "text/html"


def test_login_uses_resolved_refs_and_ssl_factory_injected() -> None:
    smtp = FakeSmtp()
    go(smtp)
    assert smtp.hosts == [(DUMMY_HOST, 10)] and smtp.logins == [(DUMMY_FROM, DUMMY_AUTH)]


def test_smtp_error_retries_then_gives_up_with_logged_error() -> None:
    smtp = FakeSmtp(lambda: smtplib.SMTPServerDisconnected("gone " + DUMMY_AUTH))
    sleeps: list[float] = []
    out = go(smtp, sleeps=sleeps)
    assert (out[0].status, out[0].attempts, out[0].detail) == (
        "failed", 3, "SMTPServerDisconnected",
    )  # fmt: skip
    assert sleeps == [1.0, 2.0] and DUMMY_AUTH not in repr(out)


def test_refused_login_is_not_retried() -> None:
    smtp = FakeSmtp(lambda: smtplib.SMTPAuthenticationError(535, b"bad"))
    out = go(smtp)
    assert (out[0].status, out[0].attempts) == ("failed", 1)


def test_no_real_connection_is_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    seen: list[object] = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: seen.append(address))
    assert go(FakeSmtp())[0].status == "sent" and seen == []


def test_headers_contain_no_resolved_reference_value() -> None:
    smtp = FakeSmtp()
    go(smtp)
    heads = "\n".join(f"{k}: {v}" for k, v in smtp.sent[0].items())
    assert DUMMY_AUTH not in heads and DUMMY_HOST not in heads


def test_missing_mail_settings_are_unconfigured_not_errors() -> None:
    out = go(FakeSmtp(), CFG1.model_copy(update={"smtp_host": None}))
    assert out[0].status == "unconfigured"


def test_any_smtp_path_exception_is_reduced_to_its_type_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fx = FakeSmtp(fail=lambda: ValueError("bad " + DUMMY_AUTH + " " + DUMMY_TO))
    caplog.set_level("DEBUG")
    t = build_transports(CFG1, Calls().client(), fx.factory)
    out = send_message(message(), t, CFG1, Tracer(tmp_path, 1), sleep=lambda _: None)
    assert (out[0].status, out[0].detail, out[0].attempts) == ("failed", "ValueError", 1)
    log = (tmp_path / "trace.jsonl").read_text()
    assert DUMMY_AUTH not in repr(out) + log + caplog.text and DUMMY_TO not in log
