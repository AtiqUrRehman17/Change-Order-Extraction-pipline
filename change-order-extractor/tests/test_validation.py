"""Tests for validation rules."""

import datetime as dt
import json
from pathlib import Path

import pytest

from change_order_extractor.extraction import RuleBasedExtractor
from change_order_extractor.ingestion import load_document
from change_order_extractor.preprocessing import clean_document
from change_order_extractor.schemas import (
    ChangeOrder,
    ChangeOrderStatus,
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    LineItem,
    Page,
    Severity,
    SignatureBlock,
    SourceType,
    ValidationReport,
)
from change_order_extractor.validation import validate

DATA = Path(__file__).resolve().parents[1] / "data"
MANIFEST = json.loads((DATA / "samples" / "manifest.json").read_text(encoding="utf-8"))
TODAY = dt.date(2026, 10, 6)


def check(document: Document | None = None, **values) -> ValidationReport:
    """Validate a change order built from plain values (plus required fields)."""
    base = {
        "change_order_number": "CO-1",
        "date": dt.date(2025, 3, 5),
        "total": 100.00,
        "description": "Work",
        "project_name": "Project",
        "contractor": "Builder",
    }
    base.update(values)
    fields = ExtractedChangeOrder.from_change_order(ChangeOrder(**base))
    return validate(fields, document, today=TODAY)


def codes(report: ValidationReport) -> list[str]:
    return [issue.code for issue in report.issues]


def items(*amounts: float) -> list[LineItem]:
    return [LineItem(description=f"item {i}", amount=a) for i, a in enumerate(amounts)]


def text_document(text: str) -> Document:
    return Document(
        source_file="co.txt", source_type=SourceType.TEXT, pages=[Page(number=1, text=text)]
    )


# ------------------------------------------------------------------ required fields


def test_complete_change_order_passes():
    report = check()
    assert report.passed
    assert report.issues == []


def test_missing_required_fields_are_errors():
    report = check(change_order_number=None, date=None, total=None)

    assert not report.passed
    assert sorted(i.code for i in report.errors) == [
        "missing_change_order_number",
        "missing_date",
        "missing_total",
    ]


def test_missing_optional_fields_are_warnings():
    report = check(description=None, project_name=None, contractor=None)

    assert report.passed
    assert [i.severity for i in report.issues] == [Severity.WARNING] * 3


# ------------------------------------------------------------------ math


def test_total_matches_items_markup_and_tax():
    report = check(line_items=items(1200, 2150), markup_amount=335, tax_amount=15, total=3700)
    assert report.passed


def test_total_mismatch_explains_the_difference():
    report = check(line_items=items(800, 450), total=1350)

    [error] = report.errors
    assert error.code == "total_mismatch"
    assert "$1,250.00" in error.message and "$100.00" in error.message
    assert set(error.fields) == {"total", "line_items"}


def test_total_checked_against_subtotal_when_given():
    report = check(line_items=items(1000), subtotal=1000, markup_amount=100, total=1200)
    assert codes(report) == ["total_mismatch"]


def test_small_rounding_differences_are_allowed():
    assert check(line_items=items(33.33, 33.33, 33.33), total=100.00).passed


def test_credits_add_up():
    assert check(line_items=items(-8400, 2150), total=-6250).passed


def test_lump_sum_has_nothing_to_check():
    assert check(total=7800).passed


def test_subtotal_mismatch():
    report = check(line_items=items(100, 200), subtotal=350, total=350)
    assert "subtotal_mismatch" in codes(report)


def test_markup_percentage_is_checked():
    def with_markup(evidence: str, amount: float) -> ValidationReport:
        fields = ExtractedChangeOrder.from_change_order(
            ChangeOrder(
                change_order_number="1",
                date=dt.date(2025, 1, 1),
                description="d",
                project_name="p",
                contractor="c",
                subtotal=3350,
                total=3350 + amount,
            )
        )
        fields.markup_amount = ExtractedField[float](value=amount, evidence=evidence)
        return validate(fields, today=TODAY)

    assert with_markup("Markup 10%  $335.00", 335).passed
    report = with_markup("Markup 10%  $350.00", 350)
    assert codes(report) == ["markup_percent_mismatch"]
    assert report.passed  # only a warning


def test_line_item_quantity_times_price():
    good = LineItem(description="panels", quantity=48, unit_price=145, amount=6960)
    bad = LineItem(description="panels", quantity=48, unit_price=145, amount=6900)
    assert check(line_items=[good], total=6960).passed
    assert "line_item_math" in codes(check(line_items=[bad], total=6900))


# ------------------------------------------------------------------ dates


def test_approval_before_change_order_date():
    report = check(date=dt.date(2025, 8, 20), approval_date=dt.date(2025, 8, 12))
    assert [i.code for i in report.errors] == ["approval_before_date"]


def test_future_dates_are_warnings():
    report = check(date=dt.date(2027, 1, 1))
    assert codes(report) == ["date_in_future"]
    assert report.passed


# ------------------------------------------------------------------ consistency


def test_approved_without_approver():
    report = check(status=ChangeOrderStatus.APPROVED)
    assert codes(report) == ["approval_incomplete"]


def test_rejected_with_approval_details():
    report = check(status=ChangeOrderStatus.REJECTED, approved_by="P. Shah")
    assert codes(report) == ["status_conflict"]


@pytest.mark.parametrize(
    ("days", "code"), [(-3, "schedule_reduction"), (400, "schedule_unusually_long")]
)
def test_unusual_schedules(days, code):
    assert codes(check(schedule_impact_days=days)) == [code]


# ------------------------------------------------------------------ evidence


def evidence_check(text: str, evidence: str, value: float = 3685.00) -> list[str]:
    fields = ExtractedChangeOrder(total=ExtractedField[float](value=value, evidence=evidence))
    report = validate(fields, text_document(text), today=TODAY)
    return [i.code for i in report.issues if not i.code.startswith("missing_")]


def test_real_evidence_passes():
    assert evidence_check("Labor $1,200\nTOTAL  $3,685.00\n", "TOTAL $3,685.00") == []


def test_made_up_evidence_is_an_error():
    assert evidence_check("Labor $1,200\nTOTAL $3,685.00", "Grand total due: $9,999.00", 9999) == [
        "evidence_not_found"
    ]


def test_nearly_matching_evidence_is_a_warning():
    text = "Labor $1,200\nTOTAL CHANGE ORDER AMOUNT  $3,685.00"
    # The model dropped a word when quoting: close enough to warn, not to reject.
    assert evidence_check(text, "TOTAL CHANGE ORDER $3,685.00") == ["evidence_inexact"]


def test_quote_with_wrong_numbers_is_an_error():
    """Looks 93% like the document, but the digits are swapped."""
    assert evidence_check("Labor $1,200\nTOTAL $3,685.00", "TOTAL $3,865.00", 3865) == [
        "evidence_not_found"
    ]


def test_value_must_match_its_evidence():
    assert evidence_check("TOTAL $3,685.00", "TOTAL $3,685.00", value=3865.00) == [
        "value_not_in_evidence"
    ]


def test_credit_written_in_words_matches_a_negative_value():
    text = "Decrease of this Change Order:\n$14,000.00"
    assert evidence_check(text, text, value=-14000.00) == []
    # without the word the quote does not show the sign
    assert evidence_check(text, "$14,000.00", value=-14000.00) == ["value_not_in_evidence"]


def test_missing_evidence_is_a_warning():
    assert evidence_check("TOTAL $3,685.00", "") == ["evidence_missing"]


def test_scanned_documents_cannot_be_verified():
    document = clean_document(load_document(DATA / "samples" / "co_008_scanned.pdf"))
    fields = ExtractedChangeOrder(
        total=ExtractedField[float](value=6708.80, evidence="TOTAL $6,708.80")
    )
    report = validate(fields, document, today=TODAY)

    assert "evidence_unverifiable" in codes(report)
    assert "evidence_not_found" not in codes(report)


# ------------------------------------------------------------------ every sample


def load_truth(entry: dict) -> ChangeOrder:
    text = (DATA / "ground_truth" / entry["ground_truth"]).read_text(encoding="utf-8")
    return ChangeOrder.model_validate_json(text)


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_ground_truth_errors_match_expected_issues(entry):
    """Correct values: errors only where the sample was built to be broken."""
    fields = ExtractedChangeOrder.from_change_order(load_truth(entry))
    report = validate(fields, today=TODAY)

    assert sorted(i.code for i in report.errors) == sorted(entry["expected_issues"])


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_rule_extractor_evidence_always_verifies(entry):
    document = clean_document(load_document(DATA / "samples" / entry["file"]))
    report = validate(RuleBasedExtractor().extract(document), document, today=TODAY)

    found = codes(report)
    assert "evidence_not_found" not in found
    assert "evidence_inexact" not in found
    assert "value_not_in_evidence" not in found


@pytest.mark.parametrize("sample_id", ["co_005_wrong_math", "co_015_bad_dates"])
def test_broken_samples_are_caught_from_real_extraction(sample_id):
    entry = next(e for e in MANIFEST if e["id"] == sample_id)
    document = clean_document(load_document(DATA / "samples" / entry["file"]))
    report = validate(RuleBasedExtractor().extract(document), document, today=TODAY)

    assert set(entry["expected_issues"]) <= {i.code for i in report.errors}


# ------------------------------------------------------------------ ambiguous dates


def test_several_labelled_dates_are_flagged():
    document = text_document(
        "CHANGE ORDER 1\nDate: March 2, 2023  Date: June 9, 2024\n"
        "Approved by: X. Owner  Date: July 1, 2024"
    )
    report = check(document, date=dt.date(2024, 6, 9))

    [issue] = [i for i in report.issues if i.code == "ambiguous_date"]
    assert issue.severity == Severity.WARNING
    assert "2023-03-02, 2024-06-09" in issue.message
    assert "2024-07-01" not in issue.message  # the approval line is not a CO date


def test_one_labelled_date_is_not_ambiguous():
    report = check(text_document("CHANGE ORDER 1\nDate: June 9, 2024"), date=dt.date(2024, 6, 9))
    assert "ambiguous_date" not in codes(report)


# ------------------------------------------------------------------ owner signature


def signed_document(owner_date: str | None) -> Document:
    owner = SignatureBlock(role="OWNER", firm="City", name="M. Gomez", date=owner_date)
    page = Page(number=1, text="CHANGE ORDER 7\nM. Gomez", signatures=[owner])
    return Document(source_file="co.pdf", source_type=SourceType.PDF_TEXT, pages=[page])


def test_approved_with_blank_owner_date_is_not_confirmed():
    report = check(signed_document(None), status=ChangeOrderStatus.APPROVED, approved_by="M. Gomez")
    assert "approval_not_confirmed" in codes(report)
    assert report.passed  # a warning: the reviewer decides


def test_approval_date_must_match_the_owner_signature():
    good = check(signed_document("2/2/2021"), approval_date=dt.date(2021, 2, 2))
    bad = check(signed_document("2/2/2021"), approval_date=dt.date(2021, 1, 8))
    assert "approval_not_confirmed" not in codes(good)
    assert "approval_not_confirmed" in codes(bad)


def test_pending_with_blank_owner_date_is_fine():
    report = check(signed_document(None), status=ChangeOrderStatus.PENDING)
    assert "approval_not_confirmed" not in codes(report)


# ------------------------------------------------------------------ scanned pages


def mixed_document() -> Document:
    return Document(
        source_file="mixed.pdf",
        source_type=SourceType.PDF_MIXED,
        pages=[Page(number=1, text="TOTAL  $100.00"), Page(number=2, is_scanned=True)],
    )


def test_no_scan_warning_when_every_quote_is_on_a_text_page():
    fields = ExtractedChangeOrder(
        total=ExtractedField[float](value=100.0, evidence="TOTAL $100.00")
    )
    report = validate(fields, mixed_document(), today=TODAY)
    assert "evidence_unverifiable" not in codes(report)


def test_scan_warning_names_the_values_read_from_images():
    fields = ExtractedChangeOrder(
        total=ExtractedField[float](value=100.0, evidence="TOTAL $100.00"),
        approved_by=ExtractedField[str](value="M. Gomez", evidence="M. Gomez, Owner"),
    )
    report = validate(fields, mixed_document(), today=TODAY)

    [issue] = [i for i in report.issues if i.code == "evidence_unverifiable"]
    assert "approved_by" in issue.message
    assert "total" not in issue.message
