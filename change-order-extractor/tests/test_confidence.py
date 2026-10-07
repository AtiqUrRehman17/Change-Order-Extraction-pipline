"""Tests for merging extractors and confidence scoring."""

import datetime as dt
from pathlib import Path

import pytest

from change_order_extractor.extraction import RuleBasedExtractor
from change_order_extractor.ingestion import load_document
from change_order_extractor.preprocessing import clean_document
from change_order_extractor.schemas import (
    ChangeOrderStatus,
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    FieldSource,
    LineItem,
    Page,
    Severity,
    SignatureBlock,
    SourceType,
    ValidationIssue,
    ValidationReport,
)
from change_order_extractor.scoring import merge, overall_confidence, score, values_agree
from change_order_extractor.scoring.merge import evidence_verified
from change_order_extractor.validation import validate

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
TODAY = dt.date(2026, 10, 6)


def rules_field(value, confidence=0.85, evidence=None, notes=()):
    return {
        "value": value,
        "confidence": confidence,
        "evidence": evidence,
        "source": FieldSource.RULES,
        "notes": list(notes),
    }


def llm_field(value, confidence=0.85, evidence=None, notes=()):
    return {**rules_field(value, confidence, evidence, notes), "source": FieldSource.LLM}


def extracted(**fields) -> ExtractedChangeOrder:
    return ExtractedChangeOrder.model_validate(fields)


def text_document(text: str) -> Document:
    return Document(
        source_file="co.txt", source_type=SourceType.TEXT, pages=[Page(number=1, text=text)]
    )


# ------------------------------------------------------------------ agreement


@pytest.mark.parametrize(
    ("name", "a", "b", "agree"),
    [
        ("change_order_number", "CO-014", "CO 14", True),
        ("change_order_number", "0005", "5", True),
        ("change_order_number", "CO-018", "CO-017", False),
        ("total", 3685.00, 3685.004, True),
        ("total", 3685.00, 3865.00, False),
        ("contractor", "ACME BUILDERS INC.", "ACME Builders Inc", True),
        ("approved_by", "Linda Chen", "Linda Chen, Facilities Director", True),
        ("owner", "Greenfield Properties LLC", "Riverside Holdings", False),
        (
            "description",
            "Add 12 duplex outlets in units 4A-4D",
            "12 duplex outlets units 4A-4D",
            True,
        ),
        ("date", dt.date(2025, 3, 5), dt.date(2025, 3, 5), True),
        ("date", dt.date(2025, 3, 5), dt.date(2025, 5, 3), False),
    ],
)
def test_values_agree(name, a, b, agree):
    assert values_agree(name, a, b) is agree


def test_line_items_agree_on_amounts():
    a = [LineItem(description="Labor", amount=800), LineItem(description="Mat", amount=450)]
    b = [LineItem(description="Material", amount=450), LineItem(description="lab", amount=800)]
    c = [LineItem(description="Labor", amount=1250)]
    assert values_agree("line_items", a, b)
    assert not values_agree("line_items", a, c)


# ------------------------------------------------------------------ merging


def test_merge_agreement_marks_both():
    merged = merge(extracted(total=rules_field(100.0)), extracted(total=llm_field(100.0)))
    assert merged.total.source == FieldSource.BOTH
    assert merged.total.value == 100.0


def test_merge_single_source_keeps_it():
    merged = merge(extracted(total=rules_field(100.0)), extracted(owner=llm_field("Greenfield")))
    assert merged.total.source == FieldSource.RULES
    assert merged.owner.source == FieldSource.LLM


def test_merge_without_llm_returns_rules():
    rules = extracted(total=rules_field(100.0))
    assert merge(rules, None) == rules


def test_conflict_prefers_the_value_whose_quote_is_in_the_document():
    document = text_document("TOTAL  $3,685.00")
    merged = merge(
        extracted(total=rules_field(3685.0, evidence="TOTAL  $3,685.00")),
        extracted(total=llm_field(3865.0, evidence="TOTAL $3,865.00")),
        document,
    )
    assert merged.total.value == 3685.0
    assert merged.total.source == FieldSource.CONFLICT
    assert "Gemini said 3865.0" in merged.total.notes[-1]
    assert "only the rules' quote is in the document" in merged.total.notes[-1]


def test_conflict_falls_back_to_confidence_then_field_type():
    by_confidence = merge(
        extracted(change_order_number=rules_field("CO-1", 0.55)),
        extracted(change_order_number=llm_field("CO-2", 0.85)),
    )
    assert by_confidence.change_order_number.value == "CO-2"

    numbers = merge(extracted(total=rules_field(1.0)), extracted(total=llm_field(2.0)))
    text = merge(
        extracted(description=rules_field("Add windows")),
        extracted(description=llm_field("Replace roof")),
    )
    assert numbers.total.value == 1.0  # rules preferred for numbers
    assert text.description.value == "Replace roof"  # Gemini preferred for text


def test_second_opinion_wins_a_remaining_conflict():
    """'Date of Issuance: 11/19/2024 Effective Date: 11/18/2024': both quotes are in the
    document; a stronger model asked about the date beats the rules' fixed preference."""
    document = text_document("Date of Issuance: 11/19/2024 Effective Date: 11/18/2024")
    rules = extracted(
        date=rules_field(dt.date(2024, 11, 18), evidence="Effective Date: 11/18/2024")
    )
    llm = extracted(date=llm_field(dt.date(2024, 11, 19), evidence="Date of Issuance: 11/19/2024"))

    assert merge(rules, llm, document).date.value == dt.date(2024, 11, 18)  # rules preferred
    merged = merge(rules, llm, document, second_opinion_fields=frozenset({"date"}))
    assert merged.date.value == dt.date(2024, 11, 19)
    assert merged.date.source == FieldSource.CONFLICT
    assert "a stronger model's second opinion" in merged.date.notes[-1]


# ------------------------------------------------------------------ approval follows the form


def signed_form(owner_date: str | None) -> Document:
    """An AIA-style form whose OWNER signature column has a printed name and maybe a date."""
    owner = SignatureBlock(
        role="OWNER", firm="City of Marshall", name="Scott VanDerMillen, Owner", date=owner_date
    )
    page = Page(number=1, text="CHANGE ORDER 10\nScott VanDerMillen, Owner", signatures=[owner])
    return Document(source_file="co.pdf", source_type=SourceType.PDF_TEXT, pages=[page])


def gemini_says_approved() -> ExtractedChangeOrder:
    return extracted(
        status=llm_field(ChangeOrderStatus.APPROVED, evidence="Scott VanDerMillen, Owner"),
        approved_by=llm_field("Scott VanDerMillen", evidence="Scott VanDerMillen, Owner"),
        approval_date=llm_field(dt.date(2021, 1, 8), evidence="January 8, 2021"),
    )


def test_blank_owner_date_beats_geminis_approval():
    """Gemini took a printed name (and the architect's date) for an owner approval."""
    rules = extracted(status=rules_field(ChangeOrderStatus.PENDING, evidence="Scott VanDerMillen"))
    merged = merge(rules, gemini_says_approved(), signed_form(owner_date=None))

    assert merged.status.value == ChangeOrderStatus.PENDING
    assert merged.status.source == FieldSource.RULES  # the form decides; not a coin-flip conflict
    assert "OWNER signature column on the form shows no date" in merged.status.notes[-1]
    # a pending change order has no approver or approval date
    assert merged.approved_by.value is None
    assert merged.approval_date.value is None
    assert "removed: Gemini said 'Scott VanDerMillen'" in merged.approved_by.notes[-1]


def test_dated_owner_signature_keeps_the_approval():
    rules = extracted(status=rules_field(ChangeOrderStatus.APPROVED, evidence="1/12/2021"))
    merged = merge(rules, gemini_says_approved(), signed_form(owner_date="1/12/2021"))
    assert merged.status.value == ChangeOrderStatus.APPROVED
    assert merged.status.source == FieldSource.BOTH
    assert merged.approved_by.value == "Scott VanDerMillen"


def test_without_a_signature_area_a_status_conflict_is_merged_as_before():
    rules = extracted(status=rules_field(ChangeOrderStatus.PENDING))
    merged = merge(rules, gemini_says_approved(), text_document("CHANGE ORDER 10"))
    assert merged.status.source == FieldSource.CONFLICT


def test_rejected_change_order_keeps_no_approver_from_gemini():
    rules = extracted(status=rules_field(ChangeOrderStatus.REJECTED, evidence="REJECTED"))
    llm = extracted(
        status=llm_field(ChangeOrderStatus.REJECTED, evidence="REJECTED"),
        approved_by=llm_field("P. Shah", evidence="P. Shah"),
        approval_date=llm_field(dt.date(2025, 7, 25), evidence="7/25/2025"),
    )
    merged = merge(rules, llm, text_document("REJECTED by P. Shah 7/25/2025"))
    assert merged.status.value == ChangeOrderStatus.REJECTED
    assert (merged.approved_by.value, merged.approval_date.value) == (None, None)


def test_agreeing_text_uses_gemini_wording_and_combines_notes():
    merged = merge(
        extracted(contractor=rules_field("PACIFIC COAST BUILDERS", notes=["fixed OCR"])),
        extracted(contractor=llm_field("Pacific Coast Builders")),
    )
    assert merged.contractor.value == "Pacific Coast Builders"
    assert merged.contractor.notes == ["fixed OCR"]


# ------------------------------------------------------------------ field scores


def scored_total(field: dict, report: ValidationReport | None = None) -> ExtractedField:
    return score(extracted(total=field), report or ValidationReport()).fields.total


def test_agreement_raises_confidence():
    field = scored_total({**rules_field(1.0, 0.85), "source": FieldSource.BOTH})
    assert field.confidence == pytest.approx(0.98, abs=0.01)
    assert "rules and Gemini agree" in field.notes


def test_single_source_and_conflict_lower_confidence():
    single = scored_total(rules_field(1.0, 0.85))
    conflict = scored_total({**rules_field(1.0, 0.85), "source": FieldSource.CONFLICT})
    assert single.confidence == pytest.approx(0.77, abs=0.01)
    assert conflict.confidence == pytest.approx(0.51, abs=0.01)


def test_validation_issues_lower_confidence():
    both = {**rules_field(1.0, 0.85), "source": FieldSource.BOTH}
    error = ValidationReport.from_issues(
        [ValidationIssue(code="total_mismatch", message="m", fields=["total"])]
    )
    warning = ValidationReport.from_issues(
        [ValidationIssue(code="w", message="m", severity=Severity.WARNING, fields=["total"])]
    )
    assert scored_total(both, error).confidence == pytest.approx(0.49, abs=0.01)
    assert scored_total(both, warning).confidence == pytest.approx(0.83, abs=0.01)
    assert "validation error: total_mismatch" in scored_total(both, error).notes


def test_confidence_never_reaches_one():
    field = scored_total({**rules_field(1.0, 0.99), "source": FieldSource.BOTH})
    assert field.confidence == 0.99


def test_empty_fields_score_zero():
    assert score(extracted(), ValidationReport()).fields.total.confidence == 0.0


# ------------------------------------------------------------------ overall + review


def test_overall_is_weighted_and_missing_required_fields_count_as_zero():
    complete = extracted(
        total={**rules_field(1.0, 0.9)},
        change_order_number={**rules_field("1", 0.9)},
        date={**rules_field(dt.date(2025, 1, 1), 0.9)},
    )
    assert overall_confidence(complete) == 0.9

    missing_date = extracted(
        total={**rules_field(1.0, 0.9)}, change_order_number={**rules_field("1", 0.9)}
    )
    assert overall_confidence(missing_date) == pytest.approx(0.9 * 5 / 7, abs=0.01)


def test_review_reasons():
    fields = extracted(
        total={**rules_field(1.0, 0.85), "source": FieldSource.CONFLICT},
        change_order_number={**rules_field("1", 0.95), "source": FieldSource.BOTH},
        date={**rules_field(dt.date(2025, 1, 1), 0.95), "source": FieldSource.BOTH},
    )
    report = ValidationReport.from_issues(
        [ValidationIssue(code="total_mismatch", message="Totals differ.", fields=["total"])]
    )
    result = score(fields, report)

    assert result.needs_review
    assert "total_mismatch: Totals differ." in result.review_reasons
    assert "rules and Gemini disagree on total" in result.review_reasons
    assert any(reason.startswith("low confidence in total") for reason in result.review_reasons)


def test_clean_agreeing_document_needs_no_review():
    both = FieldSource.BOTH
    fields = extracted(
        total={**rules_field(1.0, 0.85), "source": both},
        change_order_number={**rules_field("1", 0.85), "source": both},
        date={**rules_field(dt.date(2025, 1, 1), 0.85), "source": both},
    )
    result = score(fields, ValidationReport())
    assert not result.needs_review
    assert result.review_reasons == []


def test_a_very_low_confidence_value_is_a_review_reason():
    """A good overall score must not hide one likely-wrong value (e.g. a schedule read
    from the original contract time)."""
    both = FieldSource.BOTH
    fields = extracted(
        total={**rules_field(1.0, 0.85), "source": both},
        change_order_number={**rules_field("1", 0.85), "source": both},
        date={**rules_field(dt.date(2025, 1, 1), 0.85), "source": both},
        schedule_impact_days=rules_field(360, 0.45, notes=["unlabelled schedule line"]),
    )
    result = score(fields, ValidationReport())
    assert result.overall_confidence >= 0.75  # the overall score alone would let it pass
    assert result.review_reasons == ["very low confidence in schedule_impact_days (0.41)"]
    # Rules only: every value is single-source and correct guesses score as low, so the
    # rule would only add false alarms.
    assert not score(fields, ValidationReport(), cross_checked=False).needs_review


# ------------------------------------------------------------------ real samples (rules only)


def run_rules(sample: str):
    document = clean_document(load_document(SAMPLES / sample))
    fields = merge(RuleBasedExtractor().extract(document), None, document)
    return score(fields, validate(fields, document, today=TODAY))


def test_broken_sample_scores_lower_than_clean_sample():
    clean, broken = run_rules("co_001_clean.txt"), run_rules("co_005_wrong_math.txt")

    assert clean.overall_confidence > broken.overall_confidence
    assert broken.needs_review
    assert broken.fields.total.confidence < clean.fields.total.confidence


def test_scanned_sample_without_llm_needs_review():
    result = run_rules("co_008_scanned.pdf")
    assert result.needs_review
    assert result.overall_confidence == 0.0


# ------------------------------------------------------------------ real-world improvements


def test_quotes_are_checked_on_text_pages_of_mixed_pdfs():
    document = Document(
        source_file="mixed.pdf",
        source_type=SourceType.PDF_MIXED,
        pages=[Page(number=1, text="TOTAL  $3,685.00"), Page(number=2, is_scanned=True)],
    )
    on_text_page = ExtractedField[float](value=3685.0, evidence="TOTAL $3,685.00")
    maybe_on_scan = ExtractedField[float](value=3865.0, evidence="TOTAL $3,865.00")
    absent = ExtractedField[float](value=1.0, evidence="nowhere")

    assert evidence_verified(on_text_page, document) is True
    assert evidence_verified(maybe_on_scan, document) is None  # could be on the scan
    assert evidence_verified(absent, text_document("TOTAL $3,685.00")) is False


def test_total_conflict_is_settled_by_the_line_items():
    items = [LineItem(description=d, amount=a) for d, a in [("a", 11129), ("b", 6522), ("c", 356)]]
    merged = merge(
        extracted(line_items=rules_field(items), total=rules_field(11129.0)),
        extracted(total=llm_field(18007.0)),
    )
    assert merged.total.value == 18007.0
    assert merged.total.source == FieldSource.CONFLICT
    assert "matches line items + markup + tax (18,007.00)" in merged.total.notes[-1]


def test_ambiguous_field_gets_no_agreement_boost():
    fields = extracted(date={**rules_field(dt.date(2024, 6, 9), 0.85), "source": FieldSource.BOTH})
    report = ValidationReport.from_issues(
        [
            ValidationIssue(
                code="ambiguous_date",
                message="Several dates.",
                severity=Severity.WARNING,
                fields=["date"],
            )
        ]
    )
    result = score(fields, report)

    assert result.fields.date.confidence == pytest.approx(0.72, abs=0.01)  # 0.85 x 0.85
    assert any("no boost" in note for note in result.fields.date.notes)
    assert "ambiguous_date: Several dates." in result.review_reasons


def test_uncertain_approval_is_a_review_reason():
    fields = extracted(status={**llm_field(ChangeOrderStatus.APPROVED, 0.65)})
    result = score(fields, ValidationReport())

    assert result.fields.status.confidence < 0.75
    assert any(reason.startswith("approval not confirmed") for reason in result.review_reasons)


def test_confident_approval_is_not_a_review_reason():
    fields = extracted(
        status={**rules_field(ChangeOrderStatus.APPROVED, 0.85), "source": FieldSource.BOTH}
    )
    result = score(fields, ValidationReport())
    assert not any("approval" in reason for reason in result.review_reasons)


def test_approval_not_confirmed_warning_is_a_review_reason():
    report = ValidationReport.from_issues(
        [
            ValidationIssue(
                code="approval_not_confirmed",
                message="Owner date blank.",
                severity=Severity.WARNING,
                fields=["status"],
            )
        ]
    )
    result = score(extracted(), report)
    assert "approval_not_confirmed: Owner date blank." in result.review_reasons
