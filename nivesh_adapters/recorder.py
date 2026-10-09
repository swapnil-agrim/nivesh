"""Record/replay of HTTP traffic for offline-testable adapters.

NIVESH_RECORD=1  -> live call, redacted response saved as a fixture.
NIVESH_REPLAY=1  -> serve fixtures only; an unrecorded call fails (set by the test harness).
neither          -> plain live client.
"""

import hashlib
import json
import os
from pathlib import Path

import httpx

from nivesh_core.redact import MASK, is_sensitive_key, redact_json, redact_text, safe_query

_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


class FixtureMissing(AssertionError):
    pass


def _key(request: httpx.Request) -> str:
    base = str(request.url).partition("?")[0]
    body = redact_text(request.content.decode("utf-8", "replace"))
    blob = json.dumps([request.method, base, safe_query(str(request.url)), body])
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        k: (MASK if is_sensitive_key(k) else redact_text(v))
        for k, v in headers.items()
        if k.lower() not in _DROP_HEADERS
    }


def _redact_body(text: str) -> str:
    try:
        return json.dumps(redact_json(json.loads(text)))
    except ValueError:
        return redact_text(text)


class _Replay(httpx.BaseTransport):
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = self.dir / f"{_key(request)}.json"
        if not path.exists():
            raise FixtureMissing(
                f"no recorded fixture for {request.method} {request.url.host}{request.url.path}"
            )
        saved = json.loads(path.read_text())["response"]
        return httpx.Response(
            saved["status"], headers=saved["headers"], content=saved["body"].encode()
        )


class _Record(httpx.BaseTransport):
    def __init__(self, directory: Path, inner: httpx.BaseTransport) -> None:
        self.dir, self.inner = directory, inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self.inner.handle_request(request)
        resp.read()
        body = resp.content.decode("utf-8", "replace")
        self.dir.mkdir(parents=True, exist_ok=True)
        url = str(request.url).partition("?")[0]
        q = "&".join(f"{k}={v}" for k, v in safe_query(str(request.url)))
        fixture = {
            "request": {"method": request.method, "url": f"{url}?{q}" if q else url},
            "response": {
                "status": resp.status_code,
                "headers": _safe_headers(resp.headers),
                "body": _redact_body(body),
            },
        }
        (self.dir / f"{_key(request)}.json").write_text(
            json.dumps(fixture, indent=2, sort_keys=True)
        )
        drop = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_HEADERS}
        return httpx.Response(resp.status_code, headers=drop, content=resp.content)


def make_client(
    adapter: str, *, fixture_dir: Path | None = None, inner: httpx.BaseTransport | None = None
) -> httpx.Client:
    """The one way adapters get an HTTP client."""
    root = fixture_dir or Path(os.environ.get("NIVESH_FIXTURE_DIR", "tests/fixtures/adapters"))
    directory = root / adapter
    live = inner or httpx.HTTPTransport()
    transport: httpx.BaseTransport
    if os.environ.get("NIVESH_RECORD") == "1":
        transport = _Record(directory, live)
    elif os.environ.get("NIVESH_REPLAY") == "1":
        transport = _Replay(directory)
    else:
        transport = live
    return httpx.Client(transport=transport)
