"""Fixes spacing, leader dots, underscores and odd characters.

Golden rule: cleaning only changes whitespace, decoration and look-alike
punctuation. It never adds, removes or changes letters or digits, so any
quote taken from the cleaned text can still be traced back to the original.
OCR mistakes like '1,28O.OO' are left alone here; normalization fixes them
inside money/date values, where it is safe to do so.

Runs of 2+ spaces are kept as exactly two spaces (a "column gap"). In forms,
that gap separates a value from the next label, e.g.

    "Project: Riverside Medical Clinic  Job No. 2291"

which helps the rule-based extractor know where a value ends.
"""

import re
import unicodedata

from change_order_extractor.schemas import ColumnBlock, Document, Page, SignatureBlock

COLUMN_GAP = "  "

# Look-alike characters -> plain ASCII.
TRANSLATIONS = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote / apostrophe
        "“": '"',  # left double quote
        "”": '"',  # right double quote
        "′": "'",  # prime (feet)
        "″": '"',  # double prime (inches)
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
        "−": "-",  # minus sign
        " ": " ",  # non-breaking space
        "\t": " ",
    }
)

# Invisible characters that only confuse matching.
INVISIBLE = re.compile("[​‌‍⁠﻿­]")
# Control characters except newline.
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
# Leader dots: "Labor ........ $1,200", ". . . . ." or a spaced " ... ".
# An ellipsis attached to a word ("Wait... then") is prose and survives.
LEADER_DOTS = re.compile(r"(?:\s?\.){4,}|\s\.{3}(?=\s)")
# Fill-in lines and separators: "__J. Martinez__", "-------", "======".
RULE_LINES = re.compile(r"_{2,}|-{4,}|={4,}|\*{4,}|~{4,}")
# "$ 940.00" -> "$940.00"
CURRENCY_SPACE = re.compile(r"\$\s+(?=[\d(])")
# 2+ spaces -> column gap
MULTI_SPACE = re.compile(r" {2,}")
# 3+ newlines (2+ blank lines) -> one blank line
MULTI_BLANK = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Return a tidied copy of `text`. Safe to run more than once."""
    # Translate first: NFKC would turn the inch mark into two apostrophes.
    text = text.translate(TRANSLATIONS)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = INVISIBLE.sub("", text)
    text = CONTROL.sub("", text)

    text = LEADER_DOTS.sub(COLUMN_GAP, text)
    text = RULE_LINES.sub(COLUMN_GAP, text)
    text = CURRENCY_SPACE.sub("$", text)

    lines = (MULTI_SPACE.sub(COLUMN_GAP, line).strip() for line in text.split("\n"))
    text = "\n".join(lines)
    text = MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def clean_cell(cell: str | None) -> str | None:
    """Table cells: same cleaning, but on one line."""
    if cell is None:
        return None
    return " ".join(clean_text(cell).split())


def clean_column(block: ColumnBlock) -> ColumnBlock:
    lines = [line for line in (clean_cell(text) for text in block.lines) if line]
    return block.model_copy(update={"lines": lines})


def clean_signature(block: SignatureBlock) -> SignatureBlock:
    return block.model_copy(
        update={name: clean_cell(getattr(block, name)) or None for name in ("firm", "name", "date")}
    )


def clean_page(page: Page) -> Page:
    return page.model_copy(
        update={
            "text": clean_text(page.text),
            "tables": [
                [[clean_cell(cell) for cell in row] for row in table] for table in page.tables
            ],
            "columns": [clean_column(block) for block in page.columns],
            "signatures": [clean_signature(block) for block in page.signatures],
        }
    )


def clean_document(document: Document) -> Document:
    """Return a new Document with every page cleaned. The input is not changed."""
    return document.model_copy(update={"pages": [clean_page(page) for page in document.pages]})
