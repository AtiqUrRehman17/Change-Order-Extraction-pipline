"""Tests for normalization (money, dates, durations)."""

import datetime as dt

import pytest

from change_order_extractor.normalization import (
    fix_ocr_digits,
    fix_ocr_dollar,
    fix_ocr_letters,
    parse_date,
    parse_days,
    parse_money,
    strip_form_instructions,
    words_to_int,
)

# ------------------------------------------------------------------ helpers


def test_fix_ocr_digits_only_touches_numbers():
    assert fix_ocr_digits("T0TAL 2,915.OO") == ("T0TAL 2,915.00", True)
    assert fix_ocr_digits("1O/03/2024") == ("10/03/2024", True)
    assert fix_ocr_digits("Owner Lisa Olson") == ("Owner Lisa Olson", False)
    assert fix_ocr_digits("$1,200.00") == ("$1,200.00", False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sunset Ridge C1ubhouse", "Sunset Ridge Clubhouse"),
        ("PAC1FIC C0AST BUILDERS", "PACIFIC COAST BUILDERS"),
        ("Sunset Ridge H0A", "Sunset Ridge HOA"),
        ("frost-free va1ves", "frost-free valves"),
        ("Room A101, RTU-3, CO-08, 2nd floor", "Room A101, RTU-3, CO-08, 2nd floor"),
    ],
)
def test_fix_ocr_letters(raw, expected):
    assert fix_ocr_letters(raw)[0] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("five (5) working days", 5),
        ("ten", 10),
        ("twenty-one days", 21),
        ("Four Thousand Two Hundred Fifty Dollars", 4250),
        ("one hundred and five", 105),
        ("two million", 2_000_000),
        ("no numbers here", None),
    ],
)
def test_words_to_int(text, expected):
    assert words_to_int(text) == expected


# ------------------------------------------------------------------ money

EXACT_MONEY = [
    ("$3,685.00", 3685.00),
    ("3685", 3685.00),
    ("1,350", 1350.00),
    ("$540", 540.00),
    ("$ 940.00", 940.00),
    ("$15,765.20", 15765.20),
    ("$2,517,450.00", 2517450.00),
    ("($8,400.00)", -8400.00),
    ("(8,400.00)", -8400.00),
    ("-$500", -500.00),
    ("$-500", -500.00),
    ("USD 1,200.50", 1200.50),
    (".75", 0.75),
]


@pytest.mark.parametrize(("raw", "expected"), EXACT_MONEY)
def test_money_exact(raw, expected):
    result = parse_money(raw)
    assert result is not None
    assert result.value == expected
    assert result.is_exact, result.notes


@pytest.mark.parametrize(
    ("raw", "expected", "note"),
    [
        ("2,915.OO", 2915.00, "OCR"),
        ("1,28O.OO", 1280.00, "OCR"),
        ("500.00 CR", -500.00, "credit"),
        ("credit of $250", -250.00, "credit"),
        ("Decrease of this Change Order:\n$14,000.00", -14000.00, "credit"),
        ("Contract Sum decreased by $500", -500.00, "credit"),
        ("$7.8k", 7800.00, "thousands"),
        ("1.234,56", 1234.56, "European"),
        ("Four Thousand Two Hundred Fifty Dollars", 4250.00, "words"),
        ("labor: 18 hrs @ $85/hr = $1,530", 1530.00, "several numbers"),
    ],
)
def test_money_with_interpretation(raw, expected, note):
    result = parse_money(raw)
    assert result is not None
    assert result.value == expected
    assert any(note.lower() in n.lower() for n in result.notes), result.notes


def test_money_prefers_dollar_amount_in_mixed_text():
    result = parse_money("Four Thousand Two Hundred Fifty Dollars ($4,250.00)")
    assert result is not None
    assert result.value == 4250.00


@pytest.mark.parametrize("raw", [None, "", "   ", "N/A", "TBD"])
def test_money_missing(raw):
    assert parse_money(raw) is None


# ------------------------------------------------------------------ dates


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-04-03", dt.date(2025, 4, 3)),
        ("Mar 5th, 2025", dt.date(2025, 3, 5)),
        ("September 9, 2024", dt.date(2024, 9, 9)),
        ("Sept. 9, 2024", dt.date(2024, 9, 9)),
        ("January 22, 2025", dt.date(2025, 1, 22)),
        ("5 March 2025", dt.date(2025, 3, 5)),
        ("05-Mar-2025", dt.date(2025, 3, 5)),
        ("Mon, Nov 4, 2024 9:12 AM", dt.date(2024, 11, 4)),
        ("Date: 12/12/2024", dt.date(2024, 12, 12)),
    ],
)
def test_date_exact(raw, expected):
    result = parse_date(raw)
    assert result is not None
    assert result.value == expected
    assert result.is_exact, result.notes


@pytest.mark.parametrize(
    ("raw", "expected", "note"),
    [
        ("3/7/25", dt.date(2025, 3, 7), "two-digit year"),
        ("08/14/2024", dt.date(2024, 8, 14), None),
        ("08/12/2025", dt.date(2025, 8, 12), "ambiguous"),
        ("1O/03/2024", dt.date(2024, 10, 3), "OCR"),
        ("25/03/2025", dt.date(2025, 3, 25), "day-first"),
        ("9/9/24", dt.date(2024, 9, 9), "two-digit year"),
    ],
)
def test_date_numeric(raw, expected, note):
    result = parse_date(raw)
    assert result is not None
    assert result.value == expected
    if note:
        assert any(note.lower() in n.lower() for n in result.notes), result.notes


def test_date_dayfirst_option():
    result = parse_date("03/04/2025", dayfirst=True)
    assert result is not None
    assert result.value == dt.date(2025, 4, 3)


@pytest.mark.parametrize("raw", [None, "", "TBD", "02/30/2025", "13/13/2025", "1/1/1850"])
def test_date_invalid(raw):
    assert parse_date(raw) is None


# ------------------------------------------------------------------ durations


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("five (5) working days", 5),
        ("3 calendar days", 3),
        ("ten (10) days", 10),
        ("1 day", 1),
        ("0 days", 0),
        ("add 2 days", 2),
        ("No change", 0),
        ("No schedule impact on our end.", 0),
        ("No time extension requested.", 0),
        ("reduce contract time by 3 days", -3),
    ],
)
def test_days_exact(raw, expected):
    result = parse_days(raw)
    assert result is not None
    assert result.value == expected
    assert result.is_exact, result.notes


@pytest.mark.parametrize(
    ("raw", "expected", "note"),
    [
        ("2 weeks", 14, "weeks"),
        ("1 wk", 7, "weeks"),
        ("+2 days?", 2, "uncertain"),
        ("2 months", 60, "months"),
        ("4", 4, "no unit"),
        ("five (6) days", 6, "disagree"),
        ("1.5 days", 2, "rounded"),
    ],
)
def test_days_with_interpretation(raw, expected, note):
    result = parse_days(raw)
    assert result is not None
    assert result.value == expected
    assert any(note.lower() in n.lower() for n in result.notes), result.notes


def test_days_ignore_money_amounts():
    result = parse_days("Total is $2,070. No schedule impact.")
    assert result is not None
    assert result.value == 0


@pytest.mark.parametrize("raw", [None, "", "TBD", "see schedule"])
def test_days_missing(raw):
    assert parse_days(raw) is None


# ------------------------------------------------------------------ form instructions


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(Name and address)", ""),
        ("(Name and address) CONTRACT INFORMATION: CHANGE ORDER INFORMATION", ""),
        ("(Insert a detailed description of the change and, if applicable, attach)", ""),
        ("ACME Builders (Firm name)", "ACME Builders"),
        ("City of Marshall", "City of Marshall"),
        ("Riverside Clinic (Phase 2)", "Riverside Clinic (Phase 2)"),
        ("ELM STREET TOWNHOMES", "ELM STREET TOWNHOMES"),
    ],
)
def test_strip_form_instructions(raw, expected):
    assert strip_form_instructions(raw) == expected


# ------------------------------------------------------------------ minus signs and dashes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Steel - $5,000.00", 5000.00),  # dash as a separator
        ("Item - 5,000.00", 5000.00),
        ("- $5,000.00", 5000.00),
        ("$ - $ 5,000.00", 5000.00),  # '$ -' = empty accounting column
        ("truss-East $ - $9,700.00 $9,700.00", 9700.00),
        ("-$5,000", -5000.00),  # minus touching the amount
        ("$-5,000", -5000.00),
        ("Total: -$5,000", -5000.00),
        ("($5,000.00)", -5000.00),
    ],
)
def test_minus_only_when_attached(raw, expected):
    result = parse_money(raw)
    assert result is not None
    assert result.value == expected


def test_ocr_s_read_as_dollar():
    result = parse_money("S5,000.00")
    assert result is not None
    assert result.value == 5000.00
    assert "read OCR 'S' as '$'" in result.notes


@pytest.mark.parametrize("raw", ["Sheet S5 rev 2", "Section S12"])
def test_s_before_plain_numbers_is_not_a_dollar(raw):
    assert fix_ocr_dollar(raw) == (raw, False)
