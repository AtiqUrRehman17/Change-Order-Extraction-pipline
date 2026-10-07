"""Merge the rule-based and Gemini results field by field.

For each field:
    neither found it        -> empty                       source = none
    only one found it       -> that value                  source = rules | llm
    both found it, agree    -> one value, corroborated     source = both
    both found it, disagree -> pick a winner (below)       source = conflict

Picking a winner in a conflict, in order:
    1. the value whose evidence quote really is in the document (checked on the
       text pages, also in partly scanned PDFs)
    2. for the total: the value that matches line items + markup + tax
    3. a stronger model's second opinion: when a weaker model disagreed with the
       rules on a critical field and a stronger model was asked, its answer wins
       (a regex can't tell 'Effective Date' from 'Date of Issuance'; the model reads
       the labels)
    4. the higher preliminary confidence
    5. by field type: rules for numbers/dates/ids (exact patterns, tested to be
       precise), Gemini for free text and line items (it understands wording)
The losing value is kept in the field's notes so a reviewer can see both.

Approval follows the form when it can:
    * the OWNER column of the signature area decides the status (owner date blank ->
      pending, filled in -> approved). When the rules read it that way and Gemini
      says otherwise (e.g. it took a printed name for a signature), the form wins and
      Gemini's status is ignored, with a note.
    * a change order that ends up pending or rejected has no approver or approval
      date: such values that only Gemini gave are removed (kept in a note).
"""

import re
from difflib import SequenceMatcher
from typing import Any

from change_order_extractor.extraction.rule_based import owner_signature
from change_order_extractor.normalization import parse_date
from change_order_extractor.schemas import (
    ChangeOrderStatus,
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    FieldSource,
)
from change_order_extractor.validation.rules import (
    MONEY_TOLERANCE,
    coverage,
    evidence_pieces,
    squash,
)

MONEY_FIELDS = {"subtotal", "markup_amount", "tax_amount", "total"}
ID_FIELDS = {"change_order_number", "project_number"}
NAME_FIELDS = {"project_name", "contractor", "owner", "requested_by", "approved_by"}
FREE_TEXT_FIELDS = {"description", "reason"}
# Fields where Gemini wins a tie; everything else prefers the rules.
LLM_PREFERRED = FREE_TEXT_FIELDS | NAME_FIELDS | {"line_items"}

NAME_SIMILARITY = 0.85
TEXT_SIMILARITY = 0.6


# ------------------------------------------------------------------ agreement


def alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def normalize_id(value: str) -> str:
    """'CO-014', 'CO #14', '014', '14' -> '14'."""
    value = alnum(value)
    value = re.sub(r"^(?:co|changeorder|no)+", "", value)
    return value.lstrip("0") or "0"


def similar(a: str, b: str) -> float:
    a, b = alnum(a), alnum(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def values_agree(name: str, a: Any, b: Any) -> bool:
    if name == "line_items":
        amounts_a = sorted(round(item.amount, 2) for item in a)
        amounts_b = sorted(round(item.amount, 2) for item in b)
        return amounts_a == amounts_b
    if name in MONEY_FIELDS:
        return abs(a - b) <= 0.01
    if name in ID_FIELDS:
        return normalize_id(a) == normalize_id(b)
    if name in NAME_FIELDS:
        return similar(a, b) >= NAME_SIMILARITY
    if name in FREE_TEXT_FIELDS:
        return similar(a, b) >= TEXT_SIMILARITY
    return a == b  # dates, days, status


# ------------------------------------------------------------------ merging


def evidence_verified(field: ExtractedField, document: Document | None) -> bool | None:
    """True if the quote is on a text page, False if it is not, None if we can't tell.

    A quote missing from the text layer of a partly scanned PDF may come from a
    scanned page, so that case is 'can't tell' rather than 'not found'.
    """
    if document is None or not field.evidence or not document.text:
        return None
    text = squash(document.text)
    if all(coverage(piece, text) == 1.0 for piece in evidence_pieces(field.evidence)):
        return True
    return None if document.needs_vision else False


def expected_total(merged: dict[str, ExtractedField]) -> float | None:
    """(subtotal or sum of line items) + markup + tax, from the fields merged so far."""
    subtotal, items = merged["subtotal"].value, merged["line_items"].value
    base = subtotal if subtotal is not None else (sum(i.amount for i in items) if items else None)
    if base is None:
        return None
    return base + (merged["markup_amount"].value or 0.0) + (merged["tax_amount"].value or 0.0)


def pick_winner(
    name: str,
    rules: ExtractedField,
    llm: ExtractedField,
    document: Document | None,
    expected: float | None = None,
    second_opinion: bool = False,
) -> tuple[ExtractedField, str]:
    """(winning field, why it won)."""
    rules_ok, llm_ok = evidence_verified(rules, document), evidence_verified(llm, document)
    if rules_ok and llm_ok is False:
        return rules, "only the rules' quote is in the document"
    if llm_ok and rules_ok is False:
        return llm, "only Gemini's quote is in the document"
    if expected is not None:
        matching = [c for c in (rules, llm) if abs(c.value - expected) <= MONEY_TOLERANCE]
        if len(matching) == 1:
            return matching[0], f"matches line items + markup + tax ({expected:,.2f})"
    if second_opinion:
        return llm, "a stronger model's second opinion"
    if abs(rules.confidence - llm.confidence) >= 0.1:
        winner = rules if rules.confidence > llm.confidence else llm
        return winner, "higher extractor confidence"
    if name in LLM_PREFERRED:
        return llm, "Gemini preferred for this kind of field"
    return rules, "rules preferred for this kind of field"


def describe(value: Any) -> str:
    if isinstance(value, list):
        return f"{len(value)} items totalling {sum(item.amount for item in value):,.2f}"
    return repr(
        value.isoformat() if hasattr(value, "isoformat") else getattr(value, "value", value)
    )


def merge_field(
    name: str,
    rules: ExtractedField,
    llm: ExtractedField,
    document: Document | None,
    expected: float | None = None,
    second_opinion: bool = False,
) -> ExtractedField:
    has_rules, has_llm = rules.value is not None, llm.value is not None

    if not has_rules and not has_llm:
        return ExtractedField()
    if has_rules != has_llm:
        return (rules if has_rules else llm).model_copy()

    if name == "status" and (settled := status_from_signature(rules, llm, document)):
        return settled

    if values_agree(name, rules.value, llm.value):
        # Equivalent values: Gemini's wording/categories for text and items, the
        # rules' exact parse for everything else.
        chosen = llm if name in LLM_PREFERRED else rules
        return chosen.model_copy(
            update={
                "source": FieldSource.BOTH,
                "notes": list(dict.fromkeys([*rules.notes, *llm.notes])),
            }
        )

    winner, reason = pick_winner(name, rules, llm, document, expected, second_opinion)
    loser_label = "Gemini" if winner is rules else "rules"
    loser = llm if winner is rules else rules
    note = f"conflict: {loser_label} said {describe(loser.value)}; kept this value ({reason})"
    return winner.model_copy(
        update={"source": FieldSource.CONFLICT, "notes": [*winner.notes, note]}
    )


# ------------------------------------------------------------------ approval


def form_status(document: Document | None) -> ChangeOrderStatus | None:
    """What the OWNER column of the form's signature area says, if there is one:
    a dated owner signature -> approved, a blank owner date -> pending."""
    owner = owner_signature(document) if document is not None else None
    if owner is None:
        return None
    signed = owner.date is not None and parse_date(owner.date) is not None
    return ChangeOrderStatus.APPROVED if signed else ChangeOrderStatus.PENDING


def status_from_signature(
    rules: ExtractedField, llm: ExtractedField, document: Document | None
) -> ExtractedField | None:
    """The rules' status when it is what the form's OWNER column says and Gemini says
    something else: the form is the evidence, so Gemini's status is ignored."""
    expected = form_status(document)
    if expected is None or rules.value != expected or llm.value == expected:
        return None
    note = (
        f"Gemini said {describe(llm.value)}, but the OWNER signature column on the form "
        f"shows {'a dated signature' if expected == ChangeOrderStatus.APPROVED else 'no date'}"
        f"; kept the form's status"
    )
    return rules.model_copy(update={"notes": [*rules.notes, note]})


APPROVAL_DETAILS = ("approved_by", "approval_date")


def drop_unsupported_approval(merged: dict[str, ExtractedField]) -> None:
    """A pending or rejected change order has no approver or approval date. Such values
    that only Gemini gave (e.g. a printed name, or the architect's date) are removed."""
    status = merged["status"].value
    if status not in (ChangeOrderStatus.PENDING, ChangeOrderStatus.REJECTED):
        return
    for name in APPROVAL_DETAILS:
        field = merged[name]
        if field.value is not None and field.source == FieldSource.LLM:
            note = (
                f"removed: Gemini said {describe(field.value)}, but the status is "
                f"{status.value}, so there is no approval to record"
            )
            merged[name] = ExtractedField(notes=[*field.notes, note])


# ------------------------------------------------------------------ merging


def merge(
    rules: ExtractedChangeOrder,
    llm: ExtractedChangeOrder | None,
    document: Document | None = None,
    *,
    second_opinion_fields: frozenset[str] = frozenset(),
) -> ExtractedChangeOrder:
    """Combine both extractors. With no LLM result, the rules' result is used as is.

    second_opinion_fields: fields a stronger model was asked about (see the pipeline);
    the stronger model's answer wins a remaining conflict there.
    """
    if llm is None:
        return rules.model_copy(deep=True)
    # Field order matters: line items, subtotal, markup and tax are merged before the
    # total, so a conflicting total can be checked against them.
    merged: dict[str, ExtractedField] = {}
    for name in ExtractedChangeOrder.model_fields:
        expected = expected_total(merged) if name == "total" else None
        merged[name] = merge_field(
            name,
            getattr(rules, name),
            getattr(llm, name),
            document,
            expected,
            second_opinion=name in second_opinion_fields,
        )
    drop_unsupported_approval(merged)
    return ExtractedChangeOrder.model_validate(
        {name: field.model_dump() for name, field in merged.items()}
    )
