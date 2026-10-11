"""Send a saved report to the owner's own channels (ST-10.7): Telegram, Slack, email.

Opt-in and fail closed: nothing is sent unless a channel is configured; subject, body and page
are scanned for personal data first and a finding blocks that channel (only kinds and positions
are logged). Connection details are `ref:NAME` references resolved at send time and never
traced, printed or put in an error. The HTTP client and the mail connection are injected, so
tests send nothing; this module never builds a client through the replay recorder, because
record mode would persist URLs that carry the bot or hook value. Wire formats follow the
public Bot API, incoming-webhook and SMTP shapes and are unverified against live services.
"""

import html
import smtplib
import ssl
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Literal, Protocol

import httpx

from nivesh_adapters.report import Report, clean, from_json
from nivesh_core.delivery_config import CHANNELS, Channel, DeliverySettings
from nivesh_core.errors import NiveshError, SecretNotFound
from nivesh_core.pii_scan import scan_text
from nivesh_core.secrets import SecretRef
from nivesh_core.timeutil import IST
from nivesh_core.trace import Tracer

TELEGRAM_LIMIT = 4000
SLACK_LIMIT = 3000
SMTP_PORT = 465
Status = Literal["sent", "blocked", "failed", "unconfigured"]
HOLDINGS_COMMANDS = frozenset({"review-portfolio", "fund-doctor"})  # carry the owner's holdings


@dataclass(frozen=True)
class Message:
    subject: str
    text: str  # summary, decision table, footer: what a chat message carries
    html: str  # the full page, attached or sent as a document


@dataclass(frozen=True)
class Outcome:
    channel: str
    status: Status
    attempts: int = 0
    detail: str = ""


class DeliveryError(Exception):
    """A failed attempt. `detail` is safe to show: a short reason, never a URL or value."""

    def __init__(self, detail: str, *, retry: bool, unset: bool = False) -> None:
        super().__init__(detail)
        self.detail, self.retry, self.unset = detail, retry, unset


class Transport(Protocol):
    def uses_page(self) -> bool: ...

    def send(self, msg: Message) -> None: ...


def _row(cells: Sequence[str]) -> str:
    return " | ".join(" ".join(clean(c).split()) for c in cells)


def build_message(report: Report, html_page: str, where: str | None = None) -> Message:
    """Subject with the title and run date; body of banner (if any), the five summary lines,
    the decision table and the footer; `where` names the saved page (a path under the data dir)."""
    day = report.run_at.astimezone(IST).date().isoformat()
    lines: list[str] = []
    if report.banner:
        lines += [report.banner, ""]
    lines += [" ".join(clean(report.title).split()), ""]
    lines += [f"{i}. {s}" for i, s in enumerate(report.summary_lines, start=1)] + [""]
    lines += [_row(report.decision.headers)] + [_row(r) for r in report.decision.rows] + [""]
    lines += [f"Full report: {where}/report.html" if where else "Full report attached.", ""]
    lines += [report.footer]
    subject = f"{' '.join(clean(report.title).split())} ({day})"
    return Message(subject, "\n".join(lines), html_page)


def blocked_reason(msg: Message, *, page: bool) -> str:
    """Empty when clean; otherwise kinds and positions only, never the matched text."""
    parts = [("subject", msg.subject), ("text", msg.text)] + ([("page", msg.html)] if page else [])
    found = [f"{w}:{f.kind}@{f.start}" for w, t in parts for f in scan_text(t)]
    return ", ".join(found)


def _ref(ref: str | None, field: str) -> str:
    if ref is None:
        raise DeliveryError(f"{field} is not set", retry=False, unset=True)
    try:
        return SecretRef(ref).resolve()
    except SecretNotFound:
        raise DeliveryError(f"missing reference {ref}", retry=False) from None
    except NiveshError:
        raise DeliveryError(f"bad reference in {field}", retry=False) from None


def _check(resp: httpx.Response) -> None:
    code = resp.status_code
    if 200 <= code < 300:
        return
    raise DeliveryError(f"http {code}", retry=code == 429 or code >= 500)


def _call(fn: Callable[[], httpx.Response]) -> None:
    try:
        resp = fn()
    except httpx.HTTPError as e:  # type name only: the text of these can carry the address
        raise DeliveryError(type(e).__name__, retry=True) from None
    except Exception as e:  # e.g. InvalidURL: its text can quote the resolved address
        raise DeliveryError(type(e).__name__, retry=False) from None
    _check(resp)


class TelegramTransport:
    def __init__(self, client: httpx.Client, bot: str | None, chat: str | None) -> None:
        self.client, self.bot, self.chat = client, bot, chat
        self._text_sent: Message | None = None  # a retry resends the document only

    def uses_page(self) -> bool:
        return True

    def send(self, msg: Message) -> None:
        bot, chat = _ref(self.bot, "telegram_bot"), _ref(self.chat, "telegram_chat")
        base = f"https://api.telegram.org/bot{bot}"
        body = f"{msg.subject}\n\n{msg.text}"[:TELEGRAM_LIMIT]  # no parse mode: plain text
        if self._text_sent is not msg:
            data = {"chat_id": chat, "text": body}
            _call(lambda: self.client.post(f"{base}/sendMessage", data=data))
            self._text_sent = msg
        page = ("report.html", msg.html.encode(), "text/html")
        _call(
            lambda: self.client.post(
                f"{base}/sendDocument", data={"chat_id": chat}, files={"document": page}
            )
        )


def slack_escape(text: str) -> str:
    """The three characters Slack treats as markup."""
    return html.escape(clean(text), quote=False)


class SlackTransport:
    def __init__(self, client: httpx.Client, hook: str | None) -> None:
        self.client, self.hook = client, hook

    def uses_page(self) -> bool:
        return False

    def send(self, msg: Message) -> None:
        url = _ref(self.hook, "slack_hook")
        text = slack_escape(f"{msg.subject}\n\n{msg.text}")[:SLACK_LIMIT]
        _call(lambda: self.client.post(url, json={"text": text}))


class SmtpLike(Protocol):
    def login(self, user: str, auth: str) -> object: ...

    def send_message(self, msg: EmailMessage) -> object: ...


SmtpFactory = Callable[[str, int], AbstractContextManager[SmtpLike]]


def ssl_smtp(host: str, timeout: int) -> AbstractContextManager[SmtpLike]:
    return smtplib.SMTP_SSL(host, SMTP_PORT, timeout=timeout, context=ssl.create_default_context())


class EmailTransport:
    def __init__(
        self, factory: SmtpFactory, settings: DeliverySettings
    ) -> None:  # fmt: skip
        self.factory, self.cfg = factory, settings

    def uses_page(self) -> bool:
        return True

    def send(self, msg: Message) -> None:
        c = self.cfg
        host, user = _ref(c.smtp_host, "smtp_host"), _ref(c.smtp_user, "smtp_user")
        auth, to = _ref(c.smtp_auth, "smtp_auth"), _ref(c.mail_to, "mail_to")
        try:
            mail = EmailMessage()
            mail["Subject"], mail["From"], mail["To"] = " ".join(msg.subject.split()), user, to
            mail.set_content(msg.text)
            mail.add_attachment(
                msg.html.encode(), maintype="text", subtype="html", filename="report.html"
            )
            with self.factory(host, c.timeout_s) as smtp:
                smtp.login(user, auth)
                smtp.send_message(mail)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused):
            raise DeliveryError("smtp refused", retry=False) from None
        except (smtplib.SMTPException, OSError) as e:
            raise DeliveryError(type(e).__name__, retry=True) from None
        except Exception as e:  # type name only: the text can quote a resolved value
            raise DeliveryError(type(e).__name__, retry=False) from None


def build_transports(
    cfg: DeliverySettings, client: httpx.Client, smtp: SmtpFactory = ssl_smtp
) -> dict[Channel, Transport]:
    return {
        "telegram": TelegramTransport(client, cfg.telegram_bot, cfg.telegram_chat),
        "slack": SlackTransport(client, cfg.slack_hook),
        "email": EmailTransport(smtp, cfg),
    }


def send_message(
    msg: Message,
    transports: dict[Channel, Transport],
    cfg: DeliverySettings,
    tracer: Tracer | None = None,
    *,
    only: Channel | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    """One outcome per configured channel. A blocked or failed channel never stops the others
    and never raises; transient failures retry with doubling waits up to `max_attempts`."""
    out: list[Outcome] = []
    for ch in (c for c in CHANNELS if c in cfg.channels and only in (None, c)):
        t = transports[ch]
        why = blocked_reason(msg, page=t.uses_page())
        if why:
            _log(tracer, f"delivery blocked ({ch}): {why}")
            out.append(Outcome(ch, "blocked", 0, why))
            continue
        out.append(_attempt(ch, t, msg, cfg.max_attempts, tracer, sleep))
    return out


def _log(tracer: Tracer | None, text: str) -> None:
    if tracer is not None:
        tracer.error(text)


def _attempt(
    ch: Channel, t: Transport, msg: Message, limit: int, tracer: Tracer | None,
    sleep: Callable[[float], None],
) -> Outcome:  # fmt: skip
    detail = ""
    for n in range(1, limit + 1):
        try:
            t.send(msg)
        except DeliveryError as e:
            detail = e.detail
            _log(tracer, f"delivery failed ({ch}, attempt {n}): {detail}")
            if not e.retry:
                return Outcome(ch, "unconfigured" if e.unset else "failed", n, detail)
            if n < limit:
                sleep(2.0 ** (n - 1))
        except Exception as e:  # a transport must not raise; the text is never kept
            detail = type(e).__name__
            _log(tracer, f"delivery failed ({ch}, attempt {n}): {detail}")
            return Outcome(ch, "failed", n, detail)
        else:
            return Outcome(ch, "sent", n)
    return Outcome(ch, "failed", limit, detail)


def holdings_run(command: str) -> bool:
    return command in HOLDINGS_COMMANDS


def deliver_run(
    run_dir: Path,
    cfg: DeliverySettings,
    transports: dict[Channel, Transport],
    tracer: Tracer | None = None,
    *,
    where: str | None = None,
    only: Channel | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    """Load the saved report and page of a run and send them. A draft report keeps its banner."""
    try:
        report = from_json((run_dir / "report.json").read_text())
        page = (run_dir / "report.html").read_text()
    except (OSError, ValueError, KeyError):
        raise NiveshError("this run has no saved report to deliver") from None
    msg = build_message(report, page, where)
    return send_message(msg, transports, cfg, tracer, only=only, sleep=sleep)
