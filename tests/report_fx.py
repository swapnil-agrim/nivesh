"""A small deterministic report used by the renderer, save and check tests."""

from datetime import UTC, date, datetime
from decimal import Decimal

from nivesh_adapters.report import Block, Num, Report, Table

RUN_AT = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)  # 11 Oct 20:00 UTC = 12 Oct 01:30 IST


def base_report(**over: object) -> Report:
    fields: dict[str, object] = {
        "title": "Example Energy research note",
        "run_at": RUN_AT,
        "as_of": (date(2026, 1, 2), date(2026, 1, 1)),
        "summary": ("Verdict is HOLD", "Composite score 62.5 of 100", "Coverage 80%"),
        "decision": Table(
            ("Name", "Verdict", "Weight"),
            (("Example Energy", "HOLD", "3%"), ("Example <b> Tech", "WATCH", "2%")),
        ),
        "blocks": (
            Block("thesis", "Investment thesis", "Steady cash flow.\n\nNet debt is 1.20 crore."),
            Block("labels", "Method", "Rules fixed in config.", checked=False),
        ),
        "gaps": ("No news feed loaded",),
        "sources": ("engine fa_compute", "stored prices"),
        "nums": (Num.show(Decimal("62.5"), 1), Num.inr(Decimal("12000000"))),
        "run_id": 7,
    }
    fields.update(over)
    return Report(**fields)  # type: ignore[arg-type]


# ---- typed inputs for the templates (everything invented) ------------------------------------
import json  # noqa: E402
from typing import Any  # noqa: E402

from nivesh_adapters.mf_report import DoctorReport, FundReport  # noqa: E402
from nivesh_adapters.report_templates import (  # noqa: E402
    AllocationRow,
    BriefInput,
    HoldingRow,
    IdeaEntry,
    IdeasInput,
    PortfolioInput,
    ResearchInput,
    ScorecardInput,
    ScoreRow,
)
from nivesh_agents.committee import SecurityResult  # noqa: E402
from nivesh_agents.schemas import AnalystView, CommitteeVerdict, RiskAssessment  # noqa: E402
from nivesh_engine.fund_doctor import Action, FundFacts, Reason, ReasonCode, Verdict  # noqa: E402
from nivesh_engine.scoring import ScoreCard  # noqa: E402
from tests.agents import committee_fx as fx  # noqa: E402

D = Decimal


def verdict(**over: Any) -> CommitteeVerdict:
    return CommitteeVerdict.model_validate_json(json.dumps(fx.verdict(**over)))


def security_result(
    *, views: tuple[str, ...] = ("fundamental", "technical", "news"), **over: Any
) -> SecurityResult:
    made: dict[str, Any] = {}
    for name in views:
        extra = {"levels": fx.levels()} if name == "technical" else {}
        gaps = {"data_gaps": ["quarterly cash flow missing"]} if name == "news" else {}
        made[name] = AnalystView.model_validate_json(json.dumps(fx.analyst(name, **extra, **gaps)))
    risk = RiskAssessment.model_validate_json(json.dumps(fx.risk(risks=["cycle risk"])))
    return SecurityResult(1, verdict(**over), made, None, None, risk, None, D(100))


def research_input(**over: Any) -> ResearchInput:
    fields: dict[str, Any] = {
        "result": security_result(),
        "symbol": "EXEN", "name": "Example Energy", "currency": "INR", "run_at": RUN_AT,
        "card": ScoreCard(1, "long_term", composite=D("62.5"), band="upper"),
        "close": D("105.25"), "close_date": date(2026, 1, 2), "usd_inr": None,
        "valuation": ("Fair value range 95.00 to 120.00 (engine)",), "run_id": 7,
    }  # fmt: skip
    fields.update(over)
    return ResearchInput(**fields)


def doctor() -> DoctorReport:
    facts = FundFacts(
        "100001", "Example Fund", plan="regular", ter_gap_pct=D("0.90"), overlap_pct=D("12.5"),
        beat_pct=D("55"), unavailable={"sortino": "short history"},
    )  # fmt: skip
    reason = Reason(ReasonCode.TER_GAP_WITH_DIRECT_TWIN, "ter_gap_pct", D("0.90"), D("0.50"))
    vd = Verdict("100001", "Example Fund", None, None, Action.SWITCH_TO_DIRECT, [reason], [])
    funds = [FundReport(None, facts, vd)]  # type: ignore[arg-type]
    return DoctorReport(funds, ["Example Skipped Fund: no NAV history"])


def portfolio_input(**over: Any) -> PortfolioInput:
    fields: dict[str, Any] = {
        "run_at": RUN_AT, "as_of": date(2026, 1, 2), "total_inr": D("25000000"),
        "health": ("Two holdings are above the position limit",),
        "allocation": (
            AllocationRow("Equity", D("70"), D("65")), AllocationRow("Debt", D("30"), None),
        ),
        "concentration": ("Top sector is Energy",),
        "holdings": (HoldingRow("Example Energy", "TRIM", D("12.5"), "above the position limit"),
                     HoldingRow("Example Tech 1", "HOLD", D("7"), "thesis intact")),
        "doctor": doctor(), "rebalance": ("Move 2.00 lakh from Equity to Debt",),
        "tax": ("Short-term gain 0.50 lakh if sold today",), "gaps": (), "run_id": 7,
    }  # fmt: skip
    fields.update(over)
    return PortfolioInput(**fields)


def ideas_input(**over: Any) -> IdeasInput:
    entries = (IdeaEntry(1, "EXEN", "IN", "ADD candidate", verdict()),)
    fields: dict[str, Any] = {
        "run_at": RUN_AT, "as_of": date(2026, 1, 2), "preset": "lt-quality-value",
        "entries": entries, "wanted": 3, "qualified": 1, "message": "Only 1 of 3 qualified",
        "notes": ("one shortlist name had no price",), "run_id": 7,
    }  # fmt: skip
    fields.update(over)
    return IdeasInput(**fields)


def brief_input(**over: Any) -> BriefInput:
    from nivesh_adapters.report import Table

    fields: dict[str, Any] = {
        "run_at": RUN_AT, "as_of": date(2026, 1, 2), "markets": "india",
        "sections": (("Index levels", ("NIFTY 50 closed at 23,500.00, up 0.40%",)),
                     ("Flows", ("FII net 1,200.00 crore",))),
        "table": Table(("Index", "Close", "Move"), (("NIFTY 50", "23,500.00", "0.40%"),)),
        "unavailable": (("Sector leaders and laggards", "sector index mapping not built"),),
        "run_id": 7,
    }  # fmt: skip
    fields.update(over)
    return BriefInput(**fields)


def scorecard_input(**over: Any) -> ScorecardInput:
    fields: dict[str, Any] = {
        "run_at": RUN_AT, "as_of": date(2026, 1, 2),
        "rows": (ScoreRow("long term", 4, 0, None),), "run_id": 7,
    }  # fmt: skip
    fields.update(over)
    return ScorecardInput(**fields)
