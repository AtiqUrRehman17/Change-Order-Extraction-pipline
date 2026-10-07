"""Parses durations like 'five (5) working days' into integers.

Returns the schedule impact in days:
    'five (5) working days' -> 5     '2 weeks' -> 14     '+2 days?' -> 2 (uncertain)
    'No change' -> 0                 'reduce by 3 days' -> -3

Working vs calendar days are kept as written (not converted); the count is
what the document says.
"""

import re

from change_order_extractor.normalization.common import Normalized, fix_ocr_digits, words_to_int

NO_IMPACT = re.compile(
    r"\b(?:no\s+(?:change|impact|schedule\s+impact|time(?:\s+extension)?|extension|delay)|"
    r"none|not\s+required|unchanged)\b",
    re.IGNORECASE,
)
# A count of days; never part of a money amount like '$2,070'.
DIGITS = re.compile(r"(?<![$\d,.])[-+]?\d+(?:\.\d+)?(?![\d,])")
UNIT = re.compile(
    r"\b(?P<unit>days?|d|wks?|weeks?|mos?|months?)\b",
    re.IGNORECASE,
)
DECREASE = re.compile(r"\b(?:reduc\w*|decreas\w*|shorten\w*|less)\b", re.IGNORECASE)
DAYS_PER_UNIT = {"d": 1, "day": 1, "wk": 7, "week": 7, "mo": 30, "month": 30}


def parse_days(raw: str | None) -> Normalized[int] | None:
    """Return the number of days in `raw`, or None if there isn't one."""
    if raw is None or not raw.strip():
        return None

    notes: list[str] = []
    text, ocr_fixed = fix_ocr_digits(raw)
    if ocr_fixed:
        notes.append("fixed OCR look-alike characters in digits")

    count = read_count(text, notes)
    if count is None:
        if NO_IMPACT.search(text):
            return Normalized(0, raw, tuple(notes))
        return None

    days = count * unit_multiplier(text, notes)

    if DECREASE.search(text):
        days = -abs(days)
    if "?" in text:
        notes.append("marked as uncertain ('?')")
    if days != int(days):
        notes.append(f"rounded {days} days")

    return Normalized(round(days), raw, tuple(notes))


def read_count(text: str, notes: list[str]) -> float | None:
    """Digits and/or number words. 'five (5)' -> 5; flags 'five (6)'."""
    digit_match = DIGITS.search(text)
    digits = float(digit_match.group()) if digit_match else None
    words = words_to_int(text)

    if digits is not None and words is not None and abs(digits) != words:
        notes.append(f"words ({words}) and digits ({digits:g}) disagree; used digits")
    if digits is not None:
        return digits
    if words is not None:
        return float(words)
    return None


def unit_multiplier(text: str, notes: list[str]) -> int:
    match = UNIT.search(text)
    if match is None:
        notes.append("no unit given; assumed days")
        return 1

    unit = match["unit"].lower().rstrip("s")
    multiplier = DAYS_PER_UNIT[unit]
    if multiplier == 7:
        notes.append("converted weeks to days (x7)")
    elif multiplier == 30:
        notes.append("converted months to days (x30, approximate)")
    return multiplier
