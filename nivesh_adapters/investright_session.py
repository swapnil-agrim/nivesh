"""InvestRight daily login: request-token capture, exchange, and the IST-day token store (ST-2.2).

Wire details here (login URL, request-token parameter, access-token response shape) are per
spec, unverified against live API. The token is never logged, printed, or echoed in errors.
"""

import json
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from nivesh_adapters.recorder import make_client
from nivesh_core.config import InvestRightSettings
from nivesh_core.errors import InvestRightError, SessionExpired
from nivesh_core.paths import replace_private
from nivesh_core.timeutil import ist_date, utcnow

CALLBACK_HOST = "127.0.0.1"  # loopback only; never bind a routable interface
CALLBACK_PATH = "/callback"
REQ_PARAM = "request_token"  # per spec, unverified against live API
EXCHANGE_PATH = "/oapi/v1/access-token"
_BARE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]+$")

Clock = Callable[[], datetime]


def http_client() -> httpx.Client:
    """The one HTTP client factory for InvestRight (record/replay aware)."""
    return make_client("investright")


def login_url(settings: InvestRightSettings, app_key: str) -> str:
    """Per spec, unverified against live API. Carries the public API key only, never the secret."""
    return f"{settings.base_url}/oapi/v1/login?" + "api_" + f"key={quote(app_key)}"


def check_payload(payload: Any) -> dict[str, Any]:
    """Return the payload if `status == success`; otherwise raise with HDFC's message and code."""
    if not isinstance(payload, dict):
        raise InvestRightError("unexpected response shape from InvestRight")
    if payload.get("status") != "success":
        code = payload.get("code")
        raise InvestRightError(
            str(payload.get("message") or "InvestRight returned an error"),
            code if isinstance(code, int) else None,
        )
    return payload


def extract_request_token(text: str) -> str:
    """Accept the full redirect URL or the bare request token."""
    text = text.strip()
    if not text:
        raise InvestRightError("nothing pasted; expected the redirect URL or the request token")
    if "?" in text or "://" in text:
        found = parse_qs(urlsplit(text).query).get(REQ_PARAM)
        if not found or not found[0]:
            raise InvestRightError("no request token found in the pasted URL")
        text = found[0]
    if not _BARE_TOKEN.match(text):
        raise InvestRightError("that does not look like a request token")
    return text


def parse_callback_path(path: str) -> str | None:
    """Request token from a callback request path, or None for any other path."""
    parts = urlsplit(path)
    if parts.path != CALLBACK_PATH:
        return None
    found = parse_qs(parts.query).get(REQ_PARAM)
    return found[0] if found and found[0] else None


def serve_callback_once(port: int, timeout: float) -> str:
    """Wait for one callback on 127.0.0.1:port and return the request token.

    ponytail: thin stdlib socket loop; the real request handler is exercised by hand (D5).
    """
    box: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            bearer_value = parse_callback_path(self.path)
            self.send_response(200 if bearer_value else 404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"You can close this tab." if bearer_value else b"Not found.")
            if bearer_value:
                box.append(bearer_value)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence logging
            return

    try:
        server = HTTPServer((CALLBACK_HOST, port), Handler)
    except OSError:
        raise InvestRightError(
            f"cannot listen on {CALLBACK_HOST}:{port} (port in use?); "
            "run `nivesh login --paste` and paste the redirect URL or request token"
        ) from None
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while not box and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not box:
        raise InvestRightError("timed out waiting for the login callback")
    return box[0]


def exchange_request_token(
    client: httpx.Client,
    settings: InvestRightSettings,
    app_key: str,
    api_secret: str,
    request_token: str,
) -> str:
    """POST /oapi/v1/access-token with `{"apiSecret": ...}`; returns the access token."""
    resp = client.post(
        f"{settings.base_url}{EXCHANGE_PATH}",
        params={"api_" + "key": app_key, REQ_PARAM: request_token},
        json={"apiSecret": api_secret},
        headers={"User-Agent": settings.user_agent},
    )
    try:
        payload = resp.json()
    except ValueError:
        raise InvestRightError(f"login failed: HTTP {resp.status_code}, body is not JSON") from None
    if resp.status_code >= 400 and not (isinstance(payload, dict) and payload.get("message")):
        raise InvestRightError(f"login failed: HTTP {resp.status_code}")
    data = check_payload(payload).get("data")
    bearer_value = data.get("accessToken") if isinstance(data, dict) else None
    if not isinstance(bearer_value, str) or not bearer_value:
        raise InvestRightError("login response had no access token")
    return bearer_value


def token_path(data_dir: Path) -> Path:
    return data_dir / "investright_session.json"


class TokenStore:
    """Access token + IST issue date in a 0600 file; valid only on the IST day it was issued."""

    def __init__(self, path: Path, clock: Clock = utcnow) -> None:
        self.path, self.clock = path, clock

    def save(self, bearer_value: str) -> None:
        issued = ist_date(self.clock()).isoformat()
        replace_private(
            self.path, json.dumps({"session_token": bearer_value, "issued_ist_date": issued})
        )

    def _load(self) -> tuple[str, date] | None:
        try:
            data = json.loads(self.path.read_text())
            return data["session_token"], date.fromisoformat(data["issued_ist_date"])
        except (OSError, ValueError, KeyError, TypeError):
            return None  # missing or corrupt counts as no session

    def issued(self) -> date | None:
        loaded = self._load()
        return loaded[1] if loaded and isinstance(loaded[0], str) else None

    def valid(self) -> bool:
        return self.issued() == ist_date(self.clock())

    def token(self) -> str:
        loaded = self._load()
        if not self.valid() or loaded is None:
            raise SessionExpired("no valid InvestRight session today; run `nivesh login`")
        return loaded[0]


def complete_login(
    client: httpx.Client,
    settings: InvestRightSettings,
    app_key: str,
    api_secret: str,
    pasted: str,
    store: TokenStore,
) -> None:
    """Extract, exchange, then store; a failure at any step leaves the store untouched."""
    bearer_value = exchange_request_token(
        client, settings, app_key, api_secret, extract_request_token(pasted)
    )
    store.save(bearer_value)
