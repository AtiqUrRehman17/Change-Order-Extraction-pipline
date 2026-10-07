"""Shared helpers: the Normalized result type, OCR fixes, number words, form instructions."""

import re
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Normalized(Generic[T]):
    """A parsed value plus notes on anything we had to interpret.

    No notes = the raw text said exactly this. Every note is a small reason
    to trust the value less (used later by confidence scoring).
    """

    value: T
    raw: str
    notes: tuple[str, ...] = field(default=())

    @property
    def is_exact(self) -> bool:
        return not self.notes


# ------------------------------------------------------------------ OCR digit fixes

# Letters that OCR commonly reads instead of digits.
OCR_DIGITS = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})

# A "numeric-looking" token: made of digits, OCR look-alikes and separators,
# containing at least one real digit, and not glued to other letters.
# Matches '1,28O.OO' and '1O/03/2024' but not 'T0TAL' or 'Owner'.
NUMERIC_TOKEN = re.compile(r"(?<![A-Za-z])(?=[\dOoIl|,./-]*\d)[\dOoIl|,./-]+(?![A-Za-z])")


def fix_ocr_digits(text: str) -> tuple[str, bool]:
    """Fix O->0, l->1, etc. inside numeric-looking tokens only."""
    fixed = NUMERIC_TOKEN.sub(lambda m: m.group().translate(OCR_DIGITS), text)
    return fixed, fixed != text


# A 0 or 1 squeezed between two letters is almost always an OCR'd O / l / I:
# 'C1ubhouse', 'H0A', 'PAC1FIC', 'va1ves'. Codes like 'A101' or 'RTU-3' are untouched.
DIGIT_IN_WORD = re.compile(r"(?<=[A-Za-z])[01](?=[A-Za-z])")


def fix_ocr_letters(text: str) -> tuple[str, bool]:
    """Fix 0->O/o and 1->l/I inside words (the reverse of fix_ocr_digits)."""

    def replace(match: re.Match[str]) -> str:
        before, after = text[match.start() - 1], text[match.end()]
        upper = before.isupper() and after.isupper()
        if match.group() == "0":
            return "O" if before.isupper() else "o"
        return "I" if upper else "l"

    fixed = DIGIT_IN_WORD.sub(replace, text)
    return fixed, fixed != text


# ------------------------------------------------------------------ number words

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}
NUMBER_WORDS = set(UNITS) | set(SCALES)


def words_to_int(text: str) -> int | None:
    """First run of number words in `text` -> int. 'Four Thousand Two Hundred Fifty' -> 4250."""
    tokens = re.findall(r"[a-z]+", text.lower())

    total = current = 0
    in_run = False
    for token in tokens:
        if token in UNITS:
            current += UNITS[token]
        elif token == "hundred":
            current = max(current, 1) * 100
        elif token in SCALES:
            total += max(current, 1) * SCALES[token]
            current = 0
        elif token == "and" and in_run:
            continue
        elif in_run:
            break
        else:
            continue
        in_run = True

    return total + current if in_run else None


# ------------------------------------------------------------------ form instructions

# Printed instructions on blank forms, e.g. '(Name and address)', '(Firm name)',
# '(Insert a detailed description of the change and, if applicable, ...)'.
FORM_INSTRUCTION = re.compile(
    r"\([^()]*\b(?:name\s+and\s+address|firm\s+name|printed\s+name|typed\s+name|insert|"
    r"if\s+applicable|signature|print\s+name)\b[^()]*\)",
    re.IGNORECASE,
)
# What's left of a header row once instructions are removed: 'CONTRACT INFORMATION:'.
HEADER_LABELS_ONLY = re.compile(r"^[A-Z0-9 &/#.,'-]*:[A-Z0-9 &/#.,':-]*$")


def strip_form_instructions(text: str) -> str:
    """Remove printed form instructions; return '' if nothing real is left.

    '(Name and address)'                                   -> ''
    '(Name and address) CONTRACT INFORMATION: CHANGE ...'  -> ''  (only labels left)
    'City of Marshall'                                     -> 'City of Marshall'
    """
    cleaned = " ".join(FORM_INSTRUCTION.sub(" ", text).split()).strip(" :;,-")
    if not any(c.isalnum() for c in cleaned) or HEADER_LABELS_ONLY.match(cleaned):
        return ""
    return cleaned
