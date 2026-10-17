"""PII scans of text that carries random test-environment values (#113).

The scanner (`nivesh_core.pii_scan`) is strict on purpose: any 9 to 18 digit run is a finding. Two
test-only sources put such runs into otherwise clean text: an absolute temp path echoed by the CLI
(macOS temp dirs hold a digit run) and the random multipart boundary of an HTTP upload. This module
masks exactly those two, nothing else, so the scan of everything the product wrote stays strict.
"""

import os
import re
import tempfile
from pathlib import Path

import httpx

from nivesh_core.pii_scan import Finding, scan_text

MASK = "<path>"
BOUNDARY_MASK = "<boundary>"
MIN_ROOT = 8  # a shorter root ("/tmp", "/var") would mask unrelated text
MIN_BOUNDARY = 16  # a shorter boundary string would rewrite unrelated content
_BOUNDARY = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)  # quoted may hold spaces
_ALIASES = (("/private/var/", "/var/"), ("/private/tmp/", "/tmp/"))  # macOS: /var is /private/var


def _forms(root: str) -> set[str]:
    """The root as given, resolved, and with the macOS /private alias both ways."""
    out = {root, os.path.realpath(root)}
    for form in tuple(out):
        for private, plain in _ALIASES:
            if form.startswith(private):
                out.add(plain + form[len(private) :])
            elif form.startswith(plain):
                out.add(private + form[len(plain) :])
    return {f for f in out if len(f) >= MIN_ROOT}


def mask_roots(*roots: str | Path) -> list[str]:
    """Every string `clean_output` masks, longest first: the given roots, the temp dir, the cwd
    and the repo root, each in its plain, resolved and /private forms. A root as given that is
    not an absolute path (a phone number, a bare digit run), is `/` or is shorter than MIN_ROOT
    is dropped before its forms are derived."""
    try:
        cwd = os.getcwd()
    except OSError:  # the working directory was deleted
        cwd = ""
    repo = str(Path(__file__).resolve().parents[1])
    given = [tempfile.gettempdir(), cwd, repo, *(r for r in map(str, roots) if os.path.isabs(r))]
    forms: set[str] = set()
    for root in given:
        root = root.rstrip("/")
        if len(root) >= MIN_ROOT:
            forms |= _forms(root)
    return sorted(forms, key=lambda f: (-len(f), f))


def clean_output(text: str, *roots: str | Path) -> str:
    """`text` with every temp, cwd, repo and given root replaced by MASK. A root masks only
    itself: `<root>12345` or `<root>x` is another name and is left as it is."""
    forms = mask_roots(*roots)
    if not forms:
        return text
    pattern = re.compile("(?:" + "|".join(map(re.escape, forms)) + ")(?![A-Za-z0-9_])")
    return pattern.sub(MASK, text)


def scan_output(text: str, *roots: str | Path) -> list[Finding]:
    """`scan_text` of `clean_output(text, *roots)`: the scanner is unchanged, so `== []` keeps its
    meaning. Finding offsets refer to the cleaned text, not to `text`. Pass `tmp_path`: it covers a
    pytest temp dir that `tempfile.gettempdir()` does not (`--basetemp`, a poisoned leaf)."""
    return scan_text(clean_output(text, *roots))


def fix_boundary(body: str, content_type: str | None) -> str:
    """`body` with the multipart boundary named in `content_type` (every occurrence) replaced by
    BOUNDARY_MASK. No boundary, or one under MIN_BOUNDARY characters, changes nothing."""
    m = _BOUNDARY.search(content_type or "")
    boundary = (m.group(1) or m.group(2)) if m else ""
    if len(boundary) < MIN_BOUNDARY:
        return body
    return body.replace(boundary, BOUNDARY_MASK)


def request_text(request: httpx.Request) -> str:
    """The body of a recorded HTTP request with its multipart boundary masked. The framing and
    the file part stay under the scanner."""
    body = request.content.decode(errors="replace")
    return fix_boundary(body, request.headers.get("content-type"))
