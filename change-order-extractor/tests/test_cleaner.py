"""Tests for preprocessing (text cleaning)."""

import json
import unicodedata
from pathlib import Path

import pytest

from change_order_extractor.ingestion import load_document
from change_order_extractor.preprocessing import clean_cell, clean_document, clean_text

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ individual rules


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Labor ........ $1,200.00", "Labor  $1,200.00"),
        ("TOTAL . . . . . $3,685.00", "TOTAL  $3,685.00"),
        ("Markup 10% ... $335.00", "Markup 10%  $335.00"),
        ("Approved by: __J. Martinez____", "Approved by:  J. Martinez"),
        ("CHANGE  ORDER #   CO-014", "CHANGE  ORDER #  CO-014"),
        ("Materials .... $  940.00", "Materials  $940.00"),
        ("Credit ($ 8,400.00)", "Credit ($8,400.00)"),
        ("“Owner” request – add’l", '"Owner" request - add\'l'),
        ("3/4″ plywood", '3/4" plywood'),
        ("price: $540", "price: $540"),
        ("Total​: 10", "Total: 10"),
        ("line one\r\nline two", "line one\nline two"),
        ("a\tb", "a b"),
    ],
)
def test_cleaning_rules(raw, expected):
    assert clean_text(raw) == expected


def test_keeps_ellipsis_in_prose():
    assert clean_text("Wait... then proceed") == "Wait... then proceed"


def test_keeps_hyphens_and_ids():
    assert clean_text("Project No.: MF-2024-017") == "Project No.: MF-2024-017"


def test_removes_separator_lines():
    raw = "Sealed concrete  $2,150.00\n    ------------\nNET CREDIT ($6,250.00)"
    assert clean_text(raw) == "Sealed concrete  $2,150.00\n\nNET CREDIT ($6,250.00)"


def test_collapses_blank_lines_and_trims():
    assert clean_text("\n\n  Header  \n\n\n\n\nBody   \n\n") == "Header\n\nBody"


def test_does_not_fix_ocr_errors():
    """OCR fixes belong in normalization, not cleaning."""
    assert clean_text("T0TAL ....... 2,915.OO") == "T0TAL  2,915.OO"


def test_clean_cell():
    assert clean_cell("  $1,125.00 \n") == "$1,125.00"
    assert clean_cell("Install\nsubfloor") == "Install subfloor"
    assert clean_cell(None) is None


# ------------------------------------------------------------------ documents


def test_clean_document_does_not_change_the_original():
    doc = load_document(SAMPLES / "co_001_clean.txt")
    cleaned = clean_document(doc)

    assert "........" in doc.text
    assert "........" not in cleaned.text
    assert cleaned.source_file == doc.source_file


def test_clean_document_cleans_table_cells():
    doc = clean_document(load_document(SAMPLES / "co_002_table.pdf"))
    assert doc.pages[0].tables[0][1] == [
        "1",
        "Remove damaged subfloor",
        "450",
        "SF",
        "$2.50",
        "$1,125.00",
    ]


def test_clean_document_keeps_scanned_images():
    doc = load_document(SAMPLES / "co_008_scanned.pdf")
    cleaned = clean_document(doc)

    assert cleaned.needs_vision
    assert cleaned.scanned_images == doc.scanned_images


# ------------------------------------------------------------------ safety on every sample


def alnum(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum())


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_cleaning_never_changes_letters_or_digits(entry):
    text = load_document(SAMPLES / entry["file"]).text
    assert alnum(clean_text(text)) == alnum(text)


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_cleaning_is_idempotent(entry):
    once = clean_text(load_document(SAMPLES / entry["file"]).text)
    assert clean_text(once) == once
