"""News and announcement classification (ST-4.7). A `Classifier` is any object with
`classify(title, summary)`; the rule-based default is deterministic and offline. A model-backed
classifier plugs in through the same Protocol (deferred, D4). Output is always validated; a bad
or failing classifier yields `other`/`low` marked `fallback`, never an exception."""

import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

EventType = Literal[
    "results", "guidance", "contract_win", "mna", "management_change", "regulatory", "rating",
    "litigation", "other",
]  # fmt: skip
Materiality = Literal["high", "medium", "low"]
Sentiment = Literal["positive", "negative", "neutral"]


class Classification(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_type: EventType
    materiality: Materiality
    sentiment: Sentiment
    classified_by: str


class Classifier(Protocol):
    def classify(self, title: str, summary: str) -> Classification | dict[str, str]: ...


def _re(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


# First matching rule wins, so the more specific events come first.
RULES: list[tuple[EventType, Materiality, re.Pattern[str]]] = [
    ("results", "high", _re(r"\b(results?|earnings|q[1-4]\b|quarterly|net profit)")),
    ("mna", "high", _re(r"\b(acqui\w+|merger|merge[sd]?|takeover|buyout|demerger)\b")),
    ("guidance", "medium", _re(r"\b(guidance|outlook|forecast)\b")),
    ("contract_win", "medium", _re(r"\b(bags?|wins?|secures?|bagged)\b.*\b(order|contract)s?\b")),
    ("contract_win", "medium", _re(r"\border wins?\b")),
    ("management_change", "medium", _re(r"\b(resigns?|steps? down|appoints?|ceo|cfo|md)\b")),
    ("litigation", "medium", _re(r"\b(lawsuit|litigation|court|sues?|sued|probe)\b")),
    ("regulatory", "medium", _re(r"\b(sebi|rbi|regulator\w*|penalty|fined?|sec|fda)\b")),
    ("rating", "low", _re(r"\b(upgrades?|downgrades?|rating|target price)\b")),
]
POSITIVE = _re(r"\b(surge[sd]?|jumps?|rises?|record|beats?|wins?|bags?|upgrades?|gains?)\b")
NEGATIVE = _re(
    r"\b(plunge[sd]?|falls?|slump[sd]?|miss(?:es|ed)?|downgrades?|penalty|fined?|probe|"
    r"loss(?:es)?|resigns?)\b"
)


class RuleClassifier:
    def classify(self, title: str, summary: str) -> Classification:
        text = f"{title}. {summary}"
        event: EventType = "other"
        materiality: Materiality = "low"
        for ev, mat, pattern in RULES:
            if pattern.search(text):
                event, materiality = ev, mat
                break
        pos, neg = bool(POSITIVE.search(text)), bool(NEGATIVE.search(text))
        sentiment: Sentiment = "neutral" if pos == neg else ("positive" if pos else "negative")
        return Classification(event_type=event, materiality=materiality, sentiment=sentiment,
                              classified_by="rules")  # fmt: skip


FALLBACK = Classification(event_type="other", materiality="low", sentiment="neutral",
                          classified_by="fallback")  # fmt: skip


def classify_item(classifier: Classifier, title: str, summary: str = "") -> Classification:
    try:
        return Classification.model_validate(classifier.classify(title, summary))
    except Exception:  # noqa: BLE001 - any classifier failure degrades safely
        return FALLBACK
