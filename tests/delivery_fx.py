"""Delivery test helpers: dummy values built by concatenation, mock HTTP, fake mail."""

import json
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest

from nivesh_adapters.delivery import Message, build_message
from nivesh_adapters.report import save_report, to_html
from nivesh_core.delivery_config import DeliverySettings
from nivesh_core.trace import Tracer
from tests.report_fx import base_report

DUMMY_BOT = "ab" + "cd" + "12" + "34"
DUMMY_CHAT = "99" + "88" + "77"
DUMMY_HOOK = "https://hooks.example.test/" + "x1y2" + "z3"
DUMMY_AUTH = "pw" + "-fake-" + "5678"
DUMMY_HOST = "mail.example" + ".test"
DUMMY_FROM = "sender" + "@" + "example" + ".test"
DUMMY_TO = "owner" + "@" + "example" + ".test"
DUMMIES = (DUMMY_BOT, DUMMY_CHAT, DUMMY_HOOK, DUMMY_AUTH, DUMMY_HOST, DUMMY_FROM, DUMMY_TO)

CFG = DeliverySettings(
    channels=["telegram", "slack", "email"], telegram_bot="ref:T_BOT", telegram_chat="ref:T_CHAT",
    slack_hook="ref:S_HOOK", smtp_host="ref:M_HOST", smtp_user="ref:M_USER",
    smtp_auth="ref:M_AUTH", mail_to="ref:M_TO",
)  # fmt: skip


def set_refs(mp: pytest.MonkeyPatch) -> None:
    for k, v in (("T_BOT", DUMMY_BOT), ("T_CHAT", DUMMY_CHAT), ("S_HOOK", DUMMY_HOOK),
                 ("M_HOST", DUMMY_HOST), ("M_USER", DUMMY_FROM), ("M_AUTH", DUMMY_AUTH),
                 ("M_TO", DUMMY_TO)):  # fmt: skip
        mp.setenv(k, v)


def message(**over: object) -> Message:
    r = base_report(**over)
    return build_message(r, to_html(r), "runs/2026-10-12/7")


def tracer(tmp_path: Path) -> Tracer:
    return Tracer(tmp_path, 1)


def trace_text(tmp_path: Path) -> str:
    p = tmp_path / "trace.jsonl"
    return p.read_text() if p.is_file() else ""


class Calls:
    """Mock HTTP transport that records requests and answers from a script of status codes."""

    def __init__(self, codes: list[int | Exception] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.codes = list(codes or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        c: int | Exception = self.codes.pop(0) if self.codes else 200
        if isinstance(c, Exception):
            raise c
        return httpx.Response(c, json={"ok": True})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def body_json(req: httpx.Request) -> Any:
    return json.loads(req.content)


class FakeSmtp:
    """Records a mail session; `fail` raises on send."""

    def __init__(self, fail: Callable[[], Exception] | None = None) -> None:
        self.fail, self.hosts, self.logins = fail, [], []
        self.sent: list[EmailMessage] = []

    def factory(self, host: str, timeout: int) -> "FakeSmtp":
        self.hosts.append((host, timeout))
        return self

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def login(self, user: str, auth: str) -> None:
        self.logins.append((user, auth))

    def send_message(self, msg: EmailMessage) -> None:
        if self.fail:
            raise self.fail()
        self.sent.append(msg)


def saved_run(run_dir: Path, **over: object) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    save_report(run_dir, base_report(**over), {"x": 1})
