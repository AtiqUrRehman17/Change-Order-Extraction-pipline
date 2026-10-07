"""Compare one extracted field with the correct answer.

Every scored field gets one of five outcomes:

    correct         the right value
    correct_empty   the document doesn't state it, and nothing was returned
    wrong           a value was returned, but not the right one
    missed          the document states it, but nothing was returned
    hallucinated    a value was returned, but the document doesn't state one

'Right' depends on the kind of field:

    money                 equal within 1 cent
    ids (CO / project #)  equal after cleanup ('CO-009' = '9')
    names                 word overlap (F1) >= 0.8   ('Brennan Construction of MN, Inc.')
    description, reason   word overlap (F1) >= 0.5   (wording may differ)
    dates, days, status   exactly equal
    line items            the same amounts (each true item paired with one extracted item)

The evaluation uses its own word-overlap rule for text, so it does not just
repeat the similarity test the merge step uses.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from change_order_extractor.schemas import (
    ChangeOrder,
    ChangeOrderStatus,
    ExtractedChangeOrder,
    ExtractedField,
    FieldSource,
    LineItem,
)
from change_order_extractor.scoring.merge import normalize_id

MONEY_FIELDS = {"subtotal", "markup_amount", "tax_amount", "total"}
ID_FIELDS = {"change_order_number", "project_number"}
NAME_FIELDS = {"project_name", "contractor", "owner", "requested_by", "approved_by"}
FREE_TEXT_FIELDS = {"description", "reason"}

MONEY_TOLERANCE = 0.01
NAME_MATCH = 0.8
TEXT_MATCH = 0.5


class Outcome(StrEnum):
    CORRECT = "correct"
    CORRECT_EMPTY = "correct_empty"
    WRONG = "wrong"
    MISSED = "missed"
    HALLUCINATED = "hallucinated"


@dataclass(frozen=True)
class ItemMatch:
    """True line items paired with extracted ones (by amount, then description)."""

    pairs: list[tuple[LineItem, LineItem]] = field(default_factory=list)
    missed: list[LineItem] = field(default_factory=list)  # true items not found
    extra: list[LineItem] = field(default_factory=list)  # extracted items that aren't real

    @property
    def all_found(self) -> bool:
        return not self.missed and not self.extra

    @property
    def category_correct(self) -> int:
        return sum(true.category == found.category for true, found in self.pairs)


@dataclass(frozen=True)
class FieldResult:
    name: str
    outcome: Outcome
    expected: Any
    predicted: Any
    confidence: float
    source: FieldSource
    evidence: str | None = None
    similarity: float | None = None  # word overlap, for text fields

    @property
    def is_correct(self) -> bool:
        return self.outcome in (Outcome.CORRECT, Outcome.CORRECT_EMPTY)

    @property
    def has_prediction(self) -> bool:
        return self.outcome in (Outcome.CORRECT, Outcome.WRONG, Outcome.HALLUCINATED)


# ------------------------------------------------------------------ text similarity


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def word_f1(a: str, b: str) -> float:
    """How many words two texts share (0-1), ignoring case and punctuation."""
    words_a, words_b = Counter(words(a)), Counter(words(b))
    common = sum((words_a & words_b).values())
    if not common:
        return 0.0
    precision = common / sum(words_b.values())
    recall = common / sum(words_a.values())
    return 2 * precision * recall / (precision + recall)


# ------------------------------------------------------------------ line items


def match_items(expected: list[LineItem], predicted: list[LineItem]) -> ItemMatch:
    """Pair each true item with an extracted item of the same amount (the most
    similar description wins when several have that amount)."""
    remaining = list(predicted)
    pairs, missed = [], []
    for item in expected:
        candidates = [p for p in remaining if abs(p.amount - item.amount) <= MONEY_TOLERANCE]
        if not candidates:
            missed.append(item)
            continue
        best = max(candidates, key=lambda p: word_f1(item.description, p.description))
        remaining.remove(best)
        pairs.append((item, best))
    return ItemMatch(pairs, missed, remaining)


# ------------------------------------------------------------------ fields


def is_empty(value: Any) -> bool:
    return value is None or value == [] or value == ChangeOrderStatus.UNKNOWN


def values_match(name: str, expected: Any, predicted: Any) -> tuple[bool, float | None]:
    """(is it right, word overlap for text fields)."""
    if name == "line_items":
        return match_items(expected, predicted).all_found, None
    if name in MONEY_FIELDS:
        return abs(expected - predicted) <= MONEY_TOLERANCE, None
    if name in ID_FIELDS:
        return normalize_id(str(expected)) == normalize_id(str(predicted)), None
    if name in NAME_FIELDS | FREE_TEXT_FIELDS:
        similarity = round(word_f1(expected, predicted), 2)
        threshold = NAME_MATCH if name in NAME_FIELDS else TEXT_MATCH
        return similarity >= threshold, similarity
    return expected == predicted, None


def compare_field(name: str, expected: Any, extracted: ExtractedField) -> FieldResult:
    predicted = extracted.value
    similarity = None
    if is_empty(expected) and is_empty(predicted):
        outcome = Outcome.CORRECT_EMPTY
    elif is_empty(predicted):
        outcome = Outcome.MISSED
    elif is_empty(expected):
        outcome = Outcome.HALLUCINATED
    else:
        right, similarity = values_match(name, expected, predicted)
        outcome = Outcome.CORRECT if right else Outcome.WRONG
    return FieldResult(
        name=name,
        outcome=outcome,
        expected=expected,
        predicted=predicted,
        confidence=extracted.confidence,
        source=extracted.source,
        evidence=extracted.evidence,
        similarity=similarity,
    )


def compare_document(
    truth: ChangeOrder, fields: ExtractedChangeOrder, unscored: frozenset[str] = frozenset()
) -> list[FieldResult]:
    """One FieldResult per scored field, in schema order."""
    return [
        compare_field(name, getattr(truth, name), getattr(fields, name))
        for name in ChangeOrder.model_fields
        if name not in unscored
    ]
