"""Parses dates like 'Mar 5th, 2025' into ISO format.

Handles:  '2025-04-03'  'Mar 5th, 2025'  'September 9, 2024'  '5 March 2025'
          '05-Mar-2025'  '3/7/25'  '08/14/2024'  '1O/03/2024' (OCR)
          'Mon, Nov 4, 2024 9:12 AM'

Numeric dates are read month-first (US style) unless that is impossible
(e.g. 25/03/2025) or `dayfirst=True` is passed.
"""

import datetime as dt
import re

from change_order_extractor.normalization.common import Normalized, fix_ocr_digits

# Keyed by the first 3 letters, which are unique for every month name.
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
MONTH = r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"  # noqa: E501
ORDINAL = r"(?:st|nd|rd|th)?"

ISO = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b")
TEXT_MDY = re.compile(rf"\b{MONTH}\s+(?P<day>\d{{1,2}}){ORDINAL},?\s+(?P<year>\d{{4}})\b", re.I)
TEXT_DMY = re.compile(
    rf"\b(?P<day>\d{{1,2}}){ORDINAL}[\s-]+{MONTH}[\s,-]+(?P<year>\d{{2,4}})\b", re.I
)
NUMERIC = re.compile(r"\b(?P<a>\d{1,2})[/.-](?P<b>\d{1,2})[/.-](?P<year>\d{4}|\d{2})\b")

MIN_YEAR, MAX_YEAR = 1990, 2100


def parse_date(raw: str | None, *, dayfirst: bool = False) -> Normalized[dt.date] | None:
    """Return the first date in `raw`, or None if there isn't a valid one."""
    if raw is None or not raw.strip():
        return None

    notes: list[str] = []
    text, ocr_fixed = fix_ocr_digits(raw)
    if ocr_fixed:
        notes.append("fixed OCR look-alike characters in digits")

    if match := ISO.search(text):
        parts = int(match["year"]), int(match["month"]), int(match["day"])
    elif match := TEXT_MDY.search(text) or TEXT_DMY.search(text):
        year = expand_year(match["year"], notes)
        parts = year, MONTHS[match["month"].lower()[:3]], int(match["day"])
    elif match := NUMERIC.search(text):
        parts = numeric_parts(match, dayfirst, notes)
    else:
        return None

    year, month, day = parts
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    try:
        value = dt.date(year, month, day)
    except ValueError:
        return None
    return Normalized(value, raw, tuple(notes))


def numeric_parts(match: re.Match[str], dayfirst: bool, notes: list[str]) -> tuple[int, int, int]:
    a, b = int(match["a"]), int(match["b"])
    year = expand_year(match["year"], notes)

    if a > 12 and b <= 12:
        month, day = b, a
        if not dayfirst:
            notes.append("day-first date (first number > 12)")
    elif dayfirst:
        month, day = b, a
    else:
        month, day = a, b

    if a <= 12 and b <= 12 and a != b:
        notes.append("day/month order is ambiguous; assumed " + ("D/M" if dayfirst else "M/D"))
    return year, month, day


def expand_year(year: str, notes: list[str]) -> int:
    if len(year) == 4:
        return int(year)
    notes.append("two-digit year")
    short = int(year)
    return 2000 + short if short < 70 else 1900 + short
