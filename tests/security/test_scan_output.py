"""The PII scan of CLI output and HTTP bodies ignores random test-environment text (#113)."""

import os
import tempfile
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core.pii_scan import scan_text
from tests import pii_values as pv
from tests.agents import committee_fx as fx
from tests.cli.test_ideas_report_cli import clean_pm
from tests.ideas_fx import ASOF, seed_ideas_store, with_benchmarks
from tests.scan_fx import (
    BOUNDARY_MASK,
    MIN_ROOT,
    clean_output,
    fix_boundary,
    mask_roots,
    request_text,
    scan_output,
)

RUN = "7" * 10  # a ten-digit run: the shape of the macOS runner temp token that tripped `digits`
runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every test here runs under a temp dir whose name holds a digit run (cli_env follows it)."""
    return tmp_path_factory.mktemp("w" + RUN)


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed_ideas_store(cli_env[1])
    with_benchmarks(Path(cli_env[0][1]))
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    monkeypatch.setattr(runtime, "query", fx.happy(pm=clean_pm))
    return cli_env


def kinds(text: str) -> set[str]:
    return {f.kind for f in scan_text(text)}


def test_a_ten_digit_run_in_a_temp_path_is_flagged_raw_and_clean_after_masking(
    tmp_path: Path,
) -> None:
    text = f"saved: {tmp_path}/runs/2026-10-17/1"
    assert "digits" in kinds(text)  # the poison is real
    assert scan_output(text, tmp_path) == []
    assert clean_output(text, tmp_path) == "saved: <path>/runs/2026-10-17/1"


def test_a_real_phone_and_a_bare_ten_digit_number_still_flag(tmp_path: Path) -> None:
    text = f"{tmp_path}/x phone {pv.phone()} ref {'4' * 10}"
    found = scan_output(text, tmp_path)
    assert {"phone", "digits"} <= {f.kind for f in found}
    cleaned = clean_output(text, tmp_path)
    assert [cleaned[f.start : f.end] for f in found if f.kind == "digits"] == ["4" * 10]


def test_a_run_glued_to_a_root_is_not_masked_into_silence() -> None:
    root = "/srv/n" + RUN
    glued = root + "5" * 5  # another directory name that merely starts with the root
    assert clean_output(glued, root) == glued
    assert "digits" in kinds(clean_output(glued, root))
    ahead = "5" * 10 + root
    assert clean_output(ahead, root) == "5" * 10 + "<path>"
    assert "digits" in kinds(clean_output(ahead, root))


def test_short_roots_and_slash_are_ignored() -> None:
    text = "/ab/cd/" + "6" * 10
    assert clean_output(text, "/", "", "/ab/cd") == text
    assert "digits" in kinds(clean_output(text, "/", "", "/ab/cd"))
    assert "/ab/cd" not in mask_roots("/ab/cd")
    assert all(len(x) >= MIN_ROOT for x in mask_roots("/", "", "/ab/cd"))


def test_a_root_that_is_not_an_absolute_path_is_ignored() -> None:
    phone, bare = pv.phone(), "5" * 10  # values, not paths: masking them would hide real PII
    relative = "rel/" + "n" * 12
    text = f"call {phone} ref {bare}"
    assert mask_roots(phone, bare, relative) == mask_roots()
    assert clean_output(text, phone, bare, relative) == text
    assert {"phone", "digits"} <= {f.kind for f in scan_output(text, phone, bare, relative)}


def test_a_root_masks_only_itself(tmp_path: Path) -> None:
    other = "/srv/n" + "8" * 10 + "z"  # a different directory that holds a digit run
    text = f"{other}/a {tmp_path}x/b"  # and a sibling that merely starts with the root
    cleaned = clean_output(text, tmp_path)
    assert other in cleaned
    assert f"{tmp_path.name}x/b" in cleaned
    assert "digits" in kinds(cleaned)
    assert len([f for f in scan_text(cleaned) if f.kind == "digits"]) >= 2


def test_plain_and_private_var_forms_are_both_masked() -> None:
    root = "/var/folders/n6/n" + "1" * 10 + "0000gn/T"  # a macOS-style temp root, no filesystem
    private = "/private" + root
    both = f"{root}/a {private}/b"
    assert clean_output(both, root) == "<path>/a <path>/b"
    assert clean_output(both, private) == "<path>/a <path>/b"
    nested = private + "/pytest-of-u/pytest-0/t0"
    assert clean_output(f"{nested}/data", root, nested) == "<path>/data"
    assert clean_output(f"{nested}/data", nested, root) == "<path>/data"


def test_the_tmp_alias_masks_both_forms_whichever_one_is_given() -> None:
    plain = "/tmp/" + "n" * 12 + "/x"  # a synthetic root: no filesystem needed
    private = "/private" + plain
    both = f"{plain}/a {private}/b"
    assert clean_output(both, plain) == "<path>/a <path>/b"
    assert clean_output(both, private) == "<path>/a <path>/b"


def test_a_root_given_through_a_symlink_masks_the_resolved_path_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/")  # no default root may cover tmp_path
    real = tmp_path.resolve() / ("real" + "n" * 8)
    real.mkdir()
    link = tmp_path.resolve() / ("link" + "n" * 8)
    link.symlink_to(real, target_is_directory=True)
    assert str(real) in mask_roots(link)
    assert clean_output(f"wrote {real}/out.txt", link) == "wrote <path>/out.txt"
    assert clean_output(f"wrote {link}/out.txt", link) == "wrote <path>/out.txt"


def test_default_roots_cover_tempdir_cwd_and_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tmp = "/var/folders/aa/" + "b" * 24 + "0000gn/T"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: fake_tmp)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    roots = mask_roots()
    assert fake_tmp in roots and "/private" + fake_tmp in roots
    cwd = str(Path.cwd())
    assert cwd in roots and os.path.realpath(cwd) in roots
    assert str(Path(__file__).resolve().parents[2]) in roots
    assert clean_output(f"{fake_tmp}/pytest-of-u/x") == "<path>/pytest-of-u/x"
    assert clean_output(f"{cwd}/out.txt") == "<path>/out.txt"


def post_page(page: str, boundary: str | None = None, n: int = 1) -> list[httpx.Request]:
    """`n` multipart uploads through a mock transport, as the Telegram sendDocument call does."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    headers = {"content-type": "multipart/form-data; boundary=" + boundary} if boundary else {}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for _ in range(n):
            client.post(
                "https://api.example.test/sendDocument",
                data={"chat_id": "42"},
                files={"document": ("report.html", page, "text/html")},
                headers=headers,
            )
    return sent


def test_multipart_boundary_with_a_digit_run_is_normalised_and_the_file_part_is_scanned() -> None:
    boundary = "1" * 12 + "a" * 20
    (req,) = post_page("<html><body>report</body></html>", boundary)
    assert req.headers["content-type"].endswith(boundary)  # control: the boundary was forced
    body = req.content.decode()
    assert "digits" in kinds(body)  # control: the raw body trips the scanner
    text = request_text(req)
    assert scan_text(text) == []
    assert boundary not in text and BOUNDARY_MASK in text
    assert "Content-Disposition" in text and 'name="document"' in text
    (dirty,) = post_page(f"<html><body>{pv.phone()}</body></html>", boundary)
    assert "phone" in kinds(request_text(dirty))  # the file part stays under the scanner


def test_fix_boundary_leaves_short_missing_and_non_multipart_headers_alone() -> None:
    short, long = "b" * 15, "b" * 16  # literal lengths: they pin the minimum at 16
    body = f"--{short}\r\nname=x\r\n--{short}--"
    assert fix_boundary(body, "multipart/form-data; boundary=" + short) == body
    for header in (None, "", "application/json", "multipart/form-data"):
        assert fix_boundary(body, header) == body
    framed = f"--{long}\r\nname=x\r\n--{long}--"
    masked = f"--{BOUNDARY_MASK}\r\nname=x\r\n--{BOUNDARY_MASK}--"
    assert fix_boundary(framed, f'multipart/form-data; boundary="{long}"; charset=utf-8') == masked
    assert fix_boundary(framed, f"multipart/form-data; boundary={long}; charset=utf-8") == masked


def test_fix_boundary_reads_the_parameter_name_in_any_case_and_a_quoted_value_with_spaces() -> None:
    long = "b" * 16
    framed = f"--{long}\r\nname=x\r\n--{long}--"
    masked = f"--{BOUNDARY_MASK}\r\nname=x\r\n--{BOUNDARY_MASK}--"
    for name in ("Boundary", "BOUNDARY"):
        assert fix_boundary(framed, f"multipart/form-data; {name}={long}") == masked
    spaced = "b" * 8 + " " + "b" * 8  # 17 characters, none of the words reaches 16 alone
    framed = f"--{spaced}\r\nname=x\r\n--{spaced}--"
    for header in (f'boundary="{spaced}"', f'Boundary="{spaced}"; charset=utf-8'):
        assert fix_boundary(framed, "multipart/form-data; " + header) == masked


def test_random_multipart_boundaries_never_trip_the_scanner() -> None:
    # ~14% of httpx's random 32-hex boundaries hold a 9-18 digit run; a no-op fix fails near-surely
    sent = post_page("<html><body>report</body></html>", n=300)
    assert len(sent) == 300
    assert [i for i, req in enumerate(sent) if scan_text(request_text(req))] == []


def test_a_cli_run_echoing_a_poisoned_run_dir_is_clean_once_the_root_is_masked(
    env: Env, tmp_path: Path
) -> None:
    r = runner.invoke(app, [*env[0], "research", "AAA"])
    assert r.exit_code == 0, r.output
    assert str(tmp_path) in r.output  # the run dir is echoed on stderr, merged into .output
    assert any(f.kind == "digits" for f in scan_text(r.output))  # the poison is real
    assert scan_output(r.output, tmp_path) == []
