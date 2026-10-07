"""Tests for schemas."""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from change_order_extractor.schemas import (
    ChangeOrder,
    ChangeOrderStatus,
    DocumentInfo,
    ExtractedChangeOrder,
    ExtractedField,
    ExtractionResult,
    FieldSource,
    LineItem,
    LineItemCategory,
)


def make_change_order() -> ChangeOrder:
    return ChangeOrder(
        change_order_number="CO-014",
        project_name="Riverside Medical Clinic",
        project_number="2291",
        date=date(2025, 3, 5),
        contractor="ACME Builders Inc.",
        owner="Greenfield Properties LLC",
        description="Add 2 windows on north wall (2nd floor).",
        reason="Owner request",
        line_items=[
            LineItem(description="Labor", category=LineItemCategory.LABOR, amount=1200.00),
            LineItem(description="Materials", category=LineItemCategory.MATERIAL, amount=2150.00),
        ],
        subtotal=3350.00,
        markup_amount=335.00,
        total=3685.00,
        schedule_impact_days=5,
        status=ChangeOrderStatus.APPROVED,
        approved_by="J. Martinez",
        approval_date=date(2025, 3, 7),
    )


def test_change_order_round_trips_through_json():
    co = make_change_order()
    data = json.loads(co.model_dump_json())

    assert data["date"] == "2025-03-05"
    assert data["total"] == 3685.00
    assert data["status"] == "approved"
    assert ChangeOrder.model_validate(data) == co


def test_empty_change_order_is_allowed():
    co = ChangeOrder()
    assert co.total is None
    assert co.line_items == []
    assert co.status == ChangeOrderStatus.UNKNOWN


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        ChangeOrder(not_a_field="x")


def test_confidence_must_be_between_0_and_1():
    with pytest.raises(ValidationError):
        ExtractedField[float](value=1.0, confidence=1.5)


def test_extracted_fields_match_change_order_fields():
    assert set(ExtractedChangeOrder.model_fields) == set(ChangeOrder.model_fields)


def test_extracted_change_order_converts_to_plain_values():
    extracted = ExtractedChangeOrder(
        change_order_number=ExtractedField[str](
            value="CO-014",
            confidence=0.97,
            evidence="CHANGE ORDER # CO-014",
            source=FieldSource.BOTH,
        ),
        total=ExtractedField[float](value=3685.00, confidence=0.98, source=FieldSource.BOTH),
    )
    co = extracted.to_change_order()

    assert co.change_order_number == "CO-014"
    assert co.total == 3685.00
    assert co.project_name is None
    assert co.line_items == []


def test_extraction_result_serializes():
    result = ExtractionResult(
        document=DocumentInfo(source_file="co_001.txt", source_type="text"),
        fields=ExtractedChangeOrder(
            total=ExtractedField[float](value=3685.00, confidence=0.98, evidence="TOTAL $3,685.00")
        ),
        overall_confidence=0.91,
        needs_review=False,
    )
    data = json.loads(result.model_dump_json())

    assert data["fields"]["total"]["value"] == 3685.00
    assert data["fields"]["total"]["evidence"] == "TOTAL $3,685.00"
    assert data["fields"]["date"]["value"] is None
    assert data["validation"]["passed"] is True
