"""Parses money strings like '$3,685.00' into numbers.

Handles:  '$3,685.00'  '1,350'  '$ 940.00'  '($8,400.00)'  '-$500'  '500.00 CR'
          'credit of $250'  'Decrease of this Change Order: $14,000.00'
          '1,28O.OO' (OCR)  'S5,000.00' (OCR)  '$7.8k'
          '1.234,56' (European)  'Four Thousand Two Hundred Fifty Dollars ($4,250.00)'

A dash only makes an amount negative when it touches it ('-$500', '$-500'); a
dash used as a separator ('Steel - $5,000') or an empty column ('$ - $9,700')
does not.
"""

import re

from change_order_extractor.normalization.common import Normalized, fix_ocr_digits, words_to_int

EUROPEAN_AMOUNT = r"\d{1,3}(?:\.\d{3})+,\d{1,2}(?!\d)"
AMOUNT = re.compile(rf"{EUROPEAN_AMOUNT}|\d[\d,]*(?:\.\d+)?|\.\d+")
EUROPEAN = re.compile(rf"^{EUROPEAN_AMOUNT}$")
VALID_GROUPING = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d+)?$")
# 'Decrease of this Change Order: $14,000.00' (EJCDC C-941) is a credit too.
CREDIT_WORDS = re.compile(
    r"\b(?:CR|credit|deduct(?:ion|ive)?|reduction|decrease[ds]?)\b", re.IGNORECASE
)
THOUSANDS_SUFFIX = re.compile(r"^\s*[kK]\b")
# A minus sign touching the amount, as the text just before the digits ends:
# '-' / '-$' (at the start or after a space, '(', ':' or '='), or '$-' / '$ -'.
ATTACHED_MINUS = re.compile(r"(?:^|[\s(:=])-\$?$|\$\s?-$")
# OCR often reads '$' as 'S': 'S5,000.00'. Only before a clear money amount
# (cents or thousands commas), so words and codes like 'S5' are left alone.
OCR_DOLLAR = re.compile(r"(?<![A-Za-z0-9])S(?=\d{1,3}(?:,\d{3})+(?:\.\d{2})?\b|\d+\.\d{2}\b)")


def fix_ocr_dollar(text: str) -> tuple[str, bool]:
    """'S5,000.00' -> '$5,000.00'. Same length, so positions in the text stay valid."""
    fixed = OCR_DOLLAR.sub("$", text)
    return fixed, fixed != text


def parse_money(raw: str | None) -> Normalized[float] | None:
    """Return the amount in `raw`, or None if there isn't one."""
    if raw is None or not raw.strip():
        return None

    notes: list[str] = []
    text, ocr_fixed = fix_ocr_digits(raw.strip())
    if ocr_fixed:
        notes.append("fixed OCR look-alike characters in digits")
    text, dollar_fixed = fix_ocr_dollar(text)
    if dollar_fixed:
        notes.append("read OCR 'S' as '$'")

    matches = list(AMOUNT.finditer(text))
    if not matches:
        return parse_money_words(raw)

    match = pick_amount(text, matches)
    if len({m.group() for m in matches}) > 1:
        notes.append(f"several numbers found; used '{match.group()}'")

    number = match.group()
    if EUROPEAN.match(number):
        number = number.replace(".", "").replace(",", ".")
        notes.append("read as European format (1.234,56)")
    elif "," in number and not VALID_GROUPING.match(number):
        notes.append("unusual thousands separators")

    value = float(number.replace(",", ""))

    if THOUSANDS_SUFFIX.match(text[match.end() :]):
        value *= 1000
        notes.append("'k' suffix read as thousands")

    if is_negative(text, match.start(), match.end()) and not restated_in_words(text, value):
        value = -abs(value)
    elif CREDIT_WORDS.search(text):
        value = -abs(value)
        notes.append("treated as credit because of wording")

    return Normalized(round(value, 2), raw, tuple(notes))


def pick_amount(text: str, matches: list[re.Match[str]]) -> re.Match[str]:
    """Choose which number in the text is the amount.

    1. In a calculation ('18 hrs @ $85/hr = $1,530') the result after '=' wins.
    2. Otherwise prefer a number written right after '$'.
    3. Otherwise the first number.
    """
    equals = text.rfind("=")
    after_equals = [m for m in matches if m.start() > equals] if equals >= 0 else []
    if after_equals:
        matches = after_equals

    for match in matches:
        before = text[: match.start()].rstrip(" -(")
        if before.endswith("$"):
            return match
    return matches[0]


def is_negative(text: str, start: int, end: int) -> bool:
    """'(1,000)', '($1,000)', '-1,000', '-$1,000', '$-1,000' are negative.

    A minus sign must be attached to the amount. On forms a dash is often something
    else: a separator ('Steel - $5,000.00') or an empty accounting column
    ('$ - $9,700.00' = nothing in the first column, $9,700 in the next). Those
    amounts are positive.
    """
    before = text[:start]
    after = text[end:].lstrip()
    before_no_symbol = before.rstrip().removesuffix("$").removesuffix("USD").rstrip()

    in_parentheses = before_no_symbol.endswith("(") and after.startswith(")")
    return in_parentheses or ATTACHED_MINUS.search(before) is not None


def restated_in_words(text: str, value: float) -> bool:
    """'Four Thousand Two Hundred Fifty Dollars ($4,250.00)': the parentheses
    repeat the amount in digits; they don't mean a credit."""
    return words_to_int(text) == int(value)


def parse_money_words(raw: str) -> Normalized[float] | None:
    value = words_to_int(raw)
    if value is None:
        return None
    notes = ["amount written in words"]
    if CREDIT_WORDS.search(raw):
        value = -value
        notes.append("treated as credit because of wording")
    return Normalized(float(value), raw, tuple(notes))
