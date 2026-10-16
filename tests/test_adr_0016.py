from pathlib import Path

from nivesh_core.pii_scan import scan_text

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "adr"
ADR = DOCS / "0016-fundamentals-providers-quota-attributable-profit.md"


def text() -> str:
    return " ".join(ADR.read_text().split())


def test_adr_records_the_registry_the_ledger_and_the_attribution() -> None:
    t = text()
    for needle in (
        "vendor_call", "vendor_limit", "BEGIN IMMEDIATE", "00:00 IST", "reserve_pct",
        "month_start_day", "fundamentals.providers", "UPSTOX_ANALYTICS_TOKEN", "api.upstox.com",
        "UNVERIFIED", "reported_minority", "eps_x_shares", "revenue_basis", "NFR-18",
        "never averages", "append-only",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_lists_each_decision_d1_to_d13_and_each_deferral_x1_to_x6() -> None:
    t = text()
    for n in range(1, 14):
        assert f"D{n} " in t, n
    for n in range(1, 7):
        assert f"X{n}:" in t, n
    assert t.startswith("# ADR-0016") and "Status: accepted." in t


def test_adr_quotes_the_issue_sentence_and_says_the_nfr_text_was_not_available() -> None:
    t = text()
    sentence = "a statements/shareholding/actions provider is added by implementing one interface"
    assert sentence in t and "and registering it" in t
    assert "NFR-18" in t and "not in the repo" in t


def test_adr_states_the_quota_facts_as_the_owners_and_what_is_unverified() -> None:
    t = text()
    for needle in (
        "400", "5,000", "owner's", "monthly anchor", "failed-call billing", "remaining-quota",
        "quota set", "--used-today", "fail-closed", "never refunded", "80%", "halt",
        "kept for higher-priority", "set-once", "Budget.exhaust", "not observed",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_describes_the_reads_the_compare_and_the_upstox_slot() -> None:
    t = text()
    for needle in (
        "dense_rank", "point in time", "whole row", "per-field", "fundamentals compare",
        "--cross-check", "ISIN", "disabled", "estimated", "NotConfigured", "contract suite",
        "FakeProvider", "BUILTIN",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_discloses_the_attributable_rules_and_their_limits() -> None:
    t = text()
    for needle in (
        "GAP_TOLERANCE_PCT", "3 percent", "consolidated_no_minority_detected",
        "consolidated_minority_unknown", "latest fiscal year", "India only", "profit_basis",
        "free-cash-flow", "mix profit bases", "net_income_cagr", "cfo_to_pat", "ITC",
        "net of excise", "weighted EPS",
    ):  # fmt: skip
        assert needle in t, needle


def test_adr_records_the_hardening_and_the_defensive_wording_honestly() -> None:
    t = text()
    for needle in ("#136", "#135", "unknown-symbol", "skipped line", "defensive", "not observed"):
        assert needle in t, needle
    retired = "_minority" + "_note"  # the literal must not sit anywhere in the repository
    assert retired not in t and "the EPS-times-shares ingest note" in t


def test_adr_discloses_the_limits_found_in_review() -> None:
    flat = " ".join(text().split())
    for needle in (
        "Nothing takes a backup first",  # D3: the way back is the owner's own backup
        "nivesh backup",
        "per-minute limit",  # D6: a vendor halt row fills the window and cannot be taken back
        "cannot be taken back",
        "minority's equity is unverified",  # D10: the ROE denominator
        "counts as daily",  # D6: a reply naming both windows fills the day only
        "[profit: <basis>",  # D10: the fa text view shows the label
    ):
        assert needle in flat, needle


def test_adr_makes_no_claim_of_a_live_check() -> None:
    t = text()
    assert "Observed in the live check" not in t
    assert "no live vendor call" in t and "fixture" in t


def test_adr_0015_and_adr_0005_point_to_this_decision() -> None:
    old = (DOCS / "0015-india-fundamentals-source-tapetide.md").read_text()
    older = (DOCS / "0005-india-fundamentals-source.md").read_text()
    assert "ADR-0016" in old and "ADR-0016" in older
    flat = " ".join(old.split())
    assert "Update (" in flat and "5,000" in flat and "400" in flat  # the Starter defaults
    assert "retired" in flat and "attributable" in flat
    assert "50 calls" in flat  # the free-plan figures stay as recorded history


def test_adr_has_no_pii_or_credential_literal() -> None:
    assert scan_text(ADR.read_text()) == []


def test_readme_documents_the_providers_and_the_quota_commands() -> None:
    readme = " ".join((ROOT / "README.md").read_text().split())
    for needle in (
        "fundamentals.providers", "nivesh quota status", "nivesh quota set",
        "nivesh fundamentals compare", "nivesh fa SYMBOL --cross-check", "ADR-0016",
        "docs/runbooks/add-a-provider.md",
    ):  # fmt: skip
        assert needle in readme, needle
    assert scan_text((ROOT / "README.md").read_text()) == []
