import pytest

from nivesh_engine.classify import Classification, RuleClassifier, classify_item


@pytest.mark.parametrize(
    ("title", "event", "materiality"),
    [
        ("Alpha Q3 results: net profit rises 12 percent", "results", "high"),
        ("Alpha raises FY27 revenue guidance", "guidance", "medium"),
        ("Alpha bags Rs 500 crore order from state utility", "contract_win", "medium"),
        ("Alpha to acquire Beta in all-cash deal", "mna", "high"),
        ("Alpha CFO resigns, board appoints interim", "management_change", "medium"),
        ("SEBI imposes penalty on Alpha", "regulatory", "medium"),
        ("Broker upgrades Alpha, target price raised", "rating", "low"),
        ("Alpha faces lawsuit over patent", "litigation", "medium"),
        ("Alpha opens new store in Pune", "other", "low"),
    ],
)
def test_rule_classifier_event_types(title: str, event: str, materiality: str) -> None:
    c = RuleClassifier().classify(title, "")
    assert (c.event_type, c.materiality) == (event, materiality)
    assert c.classified_by == "rules"


def test_materiality_high_for_results_and_ma_low_for_other() -> None:
    rc = RuleClassifier()
    assert rc.classify("quarterly results", "").materiality == "high"
    assert rc.classify("merger agreement signed", "").materiality == "high"
    assert rc.classify("weather update", "").materiality == "low"


def test_sentiment_keywords() -> None:
    rc = RuleClassifier()
    assert rc.classify("Alpha shares surge after record results", "").sentiment == "positive"
    assert rc.classify("Alpha shares plunge on SEBI penalty", "").sentiment == "negative"
    assert rc.classify("Alpha schedules board meeting", "").sentiment == "neutral"


def test_summary_is_used_too() -> None:
    assert (
        RuleClassifier().classify("Update", "the company will acquire a rival").event_type == "mna"
    )


def test_classifier_output_validated_literal_values() -> None:
    class Good:
        def classify(self, title: str, summary: str) -> dict[str, str]:
            return {"event_type": "mna", "materiality": "high", "sentiment": "positive",
                    "classified_by": "model-x"}  # fmt: skip

    c = classify_item(Good(), "t", "s")
    assert c == Classification(event_type="mna", materiality="high", sentiment="positive",
                               classified_by="model-x")  # fmt: skip


def test_invalid_classifier_output_falls_back_to_other_low_marked_fallback() -> None:
    class Bad:
        def classify(self, title: str, summary: str) -> dict[str, str]:
            return {"event_type": "moon_landing", "materiality": "huge", "sentiment": "x",
                    "classified_by": "m"}  # fmt: skip

    c = classify_item(Bad(), "t", "s")
    assert (c.event_type, c.materiality, c.classified_by) == ("other", "low", "fallback")


def test_classifier_exception_falls_back_not_propagates() -> None:
    class Boom:
        def classify(self, title: str, summary: str) -> Classification:
            raise RuntimeError("model down")

    c = classify_item(Boom(), "t", "s")
    assert c.classified_by == "fallback" and c.sentiment == "neutral"


def test_fake_classifier_plugs_in_through_protocol() -> None:
    class Fake:
        def classify(self, title: str, summary: str) -> Classification:
            return Classification(event_type="rating", materiality="medium", sentiment="neutral",
                                  classified_by="fake")  # fmt: skip

    assert classify_item(Fake(), "x", "y").classified_by == "fake"
