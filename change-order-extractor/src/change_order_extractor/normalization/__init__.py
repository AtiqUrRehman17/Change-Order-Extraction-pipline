"""Step 5: NORMALIZE - convert raw values into standard formats."""

from change_order_extractor.normalization.common import (
    Normalized,
    fix_ocr_digits,
    fix_ocr_letters,
    strip_form_instructions,
    words_to_int,
)
from change_order_extractor.normalization.dates import parse_date
from change_order_extractor.normalization.durations import parse_days
from change_order_extractor.normalization.money import fix_ocr_dollar, parse_money

__all__ = [
    "Normalized",
    "fix_ocr_digits",
    "fix_ocr_dollar",
    "fix_ocr_letters",
    "parse_date",
    "parse_days",
    "parse_money",
    "strip_form_instructions",
    "words_to_int",
]
