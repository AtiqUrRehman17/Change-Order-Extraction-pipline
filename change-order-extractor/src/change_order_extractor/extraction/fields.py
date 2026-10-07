"""Helpers both extractors use to build ExtractedFields consistently, and to find
which page a field's evidence quote is on."""

from typing import TypeVar

from change_order_extractor.normalization import fix_ocr_letters
from change_order_extractor.schemas import (
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    FieldSource,
)

T = TypeVar("T")

NOTE_PENALTY = 0.1  # each interpretation note lowers the preliminary confidence
MIN_CONFIDENCE = 0.3
OCR_LETTERS_NOTE = "fixed OCR look-alike digits inside words"


def make_field(
    value: T | None,
    evidence: str | None,
    source: FieldSource,
    confidence: float,
    notes: tuple[str, ...] | list[str] = (),
) -> ExtractedField[T]:
    """An ExtractedField, or an empty one if there is no value."""
    if value is None or value == "" or value == []:
        return ExtractedField[T]()
    confidence = max(MIN_CONFIDENCE, confidence - NOTE_PENALTY * len(notes))
    return ExtractedField[T](
        value=value,
        confidence=round(confidence, 2),
        evidence=evidence,
        source=source,
        notes=list(notes),
    )


def make_text_field(
    value: str | None,
    evidence: str | None,
    source: FieldSource,
    confidence: float,
    notes: tuple[str, ...] | list[str] = (),
) -> ExtractedField[str]:
    """Like make_field, for names and descriptions: also fixes 'C1ubhouse' -> 'Clubhouse'."""
    if value:
        value = " ".join(value.split())
        value, fixed = fix_ocr_letters(value)
        if fixed:
            notes = [*notes, OCR_LETTERS_NOTE]
    return make_field(value, evidence, source, confidence, notes)


def squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def find_page(evidence: str | None, document: Document) -> int | None:
    """The page whose text contains the quote's first line (None if not on a text page)."""
    if not evidence:
        return None
    first_line = next((line for line in evidence.splitlines() if line.strip()), "")
    piece = squash(first_line.split(" | ")[0])  # table-row quotes: 'a | b | c'
    if not piece:
        return None
    for page in document.pages:
        if piece in squash(page.text):
            return page.number
    return None


def locate_pages(fields: ExtractedChangeOrder, document: Document) -> ExtractedChangeOrder:
    """Set `page` on every field whose evidence can be found in the document."""
    updated = {}
    for name in ExtractedChangeOrder.model_fields:
        field = getattr(fields, name)
        page = find_page(field.evidence, document) if field.value is not None else None
        updated[name] = field.model_copy(update={"page": page}).model_dump()
    return ExtractedChangeOrder.model_validate(updated)
