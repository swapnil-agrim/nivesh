import re
from pathlib import Path

from nivesh_core.pii_scan import scan_text

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = (ROOT / "docs" / "runbooks" / "investright.md").read_text()
VM = (ROOT / "docs" / "deploy" / "vm.md").read_text()


def test_investright_runbook_covers_registration_steps() -> None:
    for needle in (
        "developer.hdfcsec.com",
        "My Apps",
        "Create",
        "http://127.0.0.1:8765/callback",
        "primary",
        "secondary",
        "static IP",
        "Trading API",
        "activate",
        "nivesh secrets set INVESTRIGHT_API_KEY",
        "nivesh secrets set INVESTRIGHT_API_SECRET",
        "nivesh secrets set FOLIO_SALT",
        "nivesh login",
        "--paste",
    ):
        assert needle in RUNBOOK, needle


def test_runbook_explains_finding_public_ip_and_mismatch_error() -> None:
    for needle in ("egress-check", "SessionExpired", "401", "403", "static IP", "60014"):
        assert needle in RUNBOOK, needle
    assert "InvestRightError" in RUNBOOK


def test_runbook_marks_unverified_parts() -> None:
    assert "unverified against the live API" in RUNBOOK


def test_runbook_has_no_pii_or_credential_literal() -> None:
    assert scan_text(RUNBOOK) == []
    assert scan_text(VM) == []


def test_vm_guide_links_runbook() -> None:
    assert "docs/runbooks/investright.md" in VM or "runbooks/investright.md" in VM


def test_vm_guide_and_runbook_agree_on_callback_port() -> None:
    assert "-L 8765:localhost:8765" in VM and "8080" not in VM
    assert re.search(r"127\.0\.0\.1:8765/callback", RUNBOOK)
