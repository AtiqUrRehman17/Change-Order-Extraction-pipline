"""Tests for the rule-based (regex) extractor."""

import datetime as dt
import json
from pathlib import Path

import pytest

from change_order_extractor.extraction import RuleBasedExtractor
from change_order_extractor.extraction.line_items import (
    categorize,
    classify_summary,
    money_tokens,
    parse_item_line,
)
from change_order_extractor.extraction.rule_based import REFERENCE_NOTE, SEVERAL_DATES_NOTE
from change_order_extractor.ingestion import load_document
from change_order_extractor.preprocessing import clean_document
from change_order_extractor.schemas import (
    ChangeOrder,
    ChangeOrderStatus,
    Document,
    ExtractedChangeOrder,
    FieldSource,
    LineItemCategory,
    Page,
    SourceType,
)

DATA = Path(__file__).resolve().parents[1] / "data"
MANIFEST = json.loads((DATA / "samples" / "manifest.json").read_text(encoding="utf-8"))
EXTRACTOR = RuleBasedExtractor()


def extract_sample(sample_id: str) -> tuple[ExtractedChangeOrder, Document]:
    entry = next(e for e in MANIFEST if e["id"] == sample_id)
    document = clean_document(load_document(DATA / "samples" / entry["file"]))
    return EXTRACTOR.extract(document), document


def extract_text(text: str) -> ExtractedChangeOrder:
    document = Document(
        source_file="inline.txt", source_type=SourceType.TEXT, pages=[Page(number=1, text=text)]
    )
    return EXTRACTOR.extract(clean_document(document))


def load_truth(entry: dict) -> ChangeOrder:
    text = (DATA / "ground_truth" / entry["ground_truth"]).read_text(encoding="utf-8")
    return ChangeOrder.model_validate_json(text)


# ------------------------------------------------------------------ money helpers


def test_money_tokens_skip_dates_percentages_and_ids():
    line = "Markup 10% on Job No. 2291 dated 3/7/25 (BH-1022): $335.00"
    assert [t.parsed.value for t in money_tokens(line)] == [335.00]


def test_money_tokens_read_credits_and_ocr():
    assert [t.parsed.value for t in money_tokens("Credit  ($8,400.00)")] == [-8400.00]
    assert [t.parsed.value for t in money_tokens("T0TAL  2,915.OO")] == [2915.00]


@pytest.mark.parametrize(
    ("line", "name", "value"),
    [
        ("Subtotal  $3,350.00", "subtotal", 3350.00),
        ("Markup 10%  $335.00", "markup_amount", 335.00),
        ("Overhead & Profit 15%: $700.80", "markup_amount", 700.80),
        ("OH&P 10%  $2,083.00", "markup_amount", 2083.00),
        ("Fee 8%  $189.60", "markup_amount", 189.60),
        ("Sales Tax 8.25% (materials only)  $574.20", "tax_amount", 574.20),
        ("TOTAL  $3,685.00", "total", 3685.00),
        ("NET CREDIT TO OWNER  ($6,250.00)", "total", -6250.00),
        ("Lump Sum Price: $7,800.00", "total", 7800.00),
        ("Total for this change is $2,070. No schedule impact.", "total", 2070.00),
    ],
)
def test_classify_summary(line, name, value):
    result = classify_summary(line)
    assert result is not None
    assert result[0] == name
    assert result[1].value == value


def test_overhead_doors_are_an_item_not_markup():
    assert classify_summary("Overhead doors (Precision Door Co., sub)  $14,200.00") is None


def test_parse_item_line_with_quantity():
    item = parse_item_line("1. Acoustic panels 48 EA @ $145.00  $6,960.00")
    assert item is not None
    assert (item.description, item.quantity, item.unit, item.unit_price, item.amount) == (
        "Acoustic panels",
        48,
        "EA",
        145.00,
        6960.00,
    )


def test_parse_item_line_from_email_bullet():
    item = parse_item_line("- labor: 18 hrs @ $85/hr = $1,530")
    assert item is not None
    assert (item.description, item.quantity, item.unit_price, item.amount) == (
        "labor",
        18,
        85.00,
        1530.00,
    )


@pytest.mark.parametrize(
    "line",
    [
        "The original Contract Sum was  $2,450,000.00",
        "in the amount of  $4,250.00",
        "Job No. 2291",
        "Date: 08/14/2024",
        "Add 4 recessed lights in kitchen",
    ],
)
def test_lines_that_are_not_items(line):
    assert parse_item_line(line) is None


@pytest.mark.parametrize(
    ("description", "category"),
    [
        ("Labor", LineItemCategory.LABOR),
        ("Plumbing lab0r", LineItemCategory.LABOR),
        ("Materials", LineItemCategory.MATERIAL),
        ("Crane rental", LineItemCategory.EQUIPMENT),
        ("AV wiring (Clearwave AV, subcontractor)", LineItemCategory.SUBCONTRACT),
        ("Acoustic panels", LineItemCategory.OTHER),
    ],
)
def test_categorize(description, category):
    assert categorize(description) == category


# ------------------------------------------------------------------ whole documents


def test_clean_form_is_fully_extracted():
    result, _ = extract_sample("co_001_clean")
    truth = load_truth(next(e for e in MANIFEST if e["id"] == "co_001_clean"))
    got = result.to_change_order()

    for name in ChangeOrder.model_fields:  # every field found, and right (ignoring case)
        assert comparable(name, getattr(got, name)) == comparable(name, getattr(truth, name))
    assert result.contractor.value == "ACME BUILDERS INC."  # kept as written
    assert result.total.source == FieldSource.RULES
    assert result.total.evidence == "TOTAL  $3,685.00"
    assert result.total.confidence > 0.8


def test_table_pdf_items_come_from_the_table():
    result, _ = extract_sample("co_002_table")
    items = result.line_items.value

    assert [i.amount for i in items] == [1125.00, 1440.00, 1632.00, 475.00]
    assert (items[1].quantity, items[1].unit, items[1].unit_price) == (450, "SF", 3.20)
    assert result.description.value == (
        "Replace damaged subfloor discovered during demolition in the east reading room."
    )
    assert result.status.value == ChangeOrderStatus.PENDING  # blank owner signature


def test_credit_amounts_are_negative():
    result, _ = extract_sample("co_004_credit")

    assert [i.amount for i in result.line_items.value] == [-8400.00, 2150.00]
    assert result.total.value == -6250.00
    assert result.schedule_impact_days.value == 0


def test_aia_form_total_is_the_change_not_the_contract_sum():
    result, _ = extract_sample("co_010_aia_style")

    assert result.total.value == 4250.00
    assert result.line_items.value is None
    assert result.schedule_impact_days.value == 10


def test_ocr_noise_is_repaired():
    result, _ = extract_sample("co_012_ocr_noise")

    assert result.date.value == dt.date(2024, 10, 3)
    assert result.total.value == 2915.00
    assert result.project_name.value == "Sunset Ridge Clubhouse"
    assert result.owner.value == "Sunset Ridge HOA"
    assert any("OCR" in note for note in result.project_name.notes)


def test_first_change_order_number_wins_over_superseded_one():
    result, _ = extract_sample("co_014_supersedes")
    assert result.change_order_number.value == "CO-018"


def test_approval_date_is_not_used_as_change_order_date():
    result, _ = extract_sample("co_015_bad_dates")

    assert result.date.value == dt.date(2025, 8, 20)
    assert result.approval_date.value == dt.date(2025, 8, 12)
    assert result.approved_by.value == "J. Ramirez"


def test_email_reads_total_and_no_schedule_impact():
    result, _ = extract_sample("co_003_email")

    assert result.total.value == 2070.00
    assert result.schedule_impact_days.value == 0
    assert result.owner.value is None  # 'To: Sarah Lopez <...>' is a person's email


def test_rejected_status():
    result, _ = extract_sample("co_011_rejected")
    assert result.status.value == ChangeOrderStatus.REJECTED


def test_scanned_pdf_gives_nothing_to_rules():
    result, _ = extract_sample("co_008_scanned")
    assert all(getattr(result, name).value is None for name in ExtractedChangeOrder.model_fields)


def test_heuristic_values_get_lower_confidence():
    result = extract_text("SMITH BUILDERS LLC\nCHANGE ORDER 3\nDate: 01/02/2025")

    assert result.contractor.value == "SMITH BUILDERS LLC"
    assert result.contractor.confidence < result.change_order_number.confidence
    assert result.contractor.notes == ["guessed from letterhead (first line)"]


def test_empty_text_gives_empty_result():
    result = extract_text("nothing useful here")
    assert result.to_change_order() == ChangeOrder()


# ------------------------------------------------------------------ guarantees on every sample


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_evidence_is_copied_from_the_document(entry):
    result, document = extract_sample(entry["id"])
    for name in ExtractedChangeOrder.model_fields:
        field = getattr(result, name)
        if field.value is None or (name == "line_items" and document.pages[0].tables):
            continue
        assert field.evidence, name
        for evidence_line in field.evidence.splitlines():
            assert evidence_line in document.text, (name, evidence_line)


def comparable(name: str, value):
    if name == "line_items":  # categories are a judgment call; compare what was written
        return [(i.description.lower(), i.amount) for i in value]
    return str(value).lower()


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_rules_are_precise(entry):
    """Rules may miss fields, but every value they DO return must be right."""
    result, _ = extract_sample(entry["id"])
    truth = load_truth(entry)

    for name in ChangeOrder.model_fields:
        value = getattr(result, name).value
        if value is None:
            continue
        assert comparable(name, value) == comparable(name, getattr(truth, name)), name


# ------------------------------------------------------------------ real-world improvements


def test_aia_sentence_beats_a_total_line():
    result = extract_text(
        "Cost breakdown\nTOTAL: $2,000.00\n"
        "The Contract Sum will be increased by this Change Order in the amount of $3,500.00"
    )
    assert result.total.value == 3500.00
    assert "in the amount of" in result.total.evidence


EJCDC_FORM = (
    "Change Order No. 08\n"
    "Date of Issuance: 11/19/2024 Effective Date: 11/18/2024\n"
    "Owner: City of Lawrence Utilities Owner's Contract No.:\n"
    "Contractor: J.G. Case Construction, Inc. Contractor's Project No.:\n"
    "Original Contract Price: Original Contract Times:\n"
    "Substantial Completion: 360 calendar days (August 5, 2022)\n"
    "$ 3,367,585.00 Ready for Final Payment: 425 calendar days (October 9, 2022)\n"
    "{direction} of this Change Order: {time_direction} of this Change Order:\n"
    "Substantial Completion: {days}\n"
    "$14,000.00 Ready for Final Payment: NA\n"
)


def ejcdc(direction="Decrease", time_direction="Increase", days="NA") -> ExtractedChangeOrder:
    return extract_text(
        EJCDC_FORM.format(direction=direction, time_direction=time_direction, days=days)
    )


def test_ejcdc_price_and_time_change():
    result = ejcdc()
    assert result.total.value == -14000.0
    assert "Decrease of this Change Order" in result.total.evidence
    assert result.schedule_impact_days.value == 0  # 'NA', not the original 360 days
    assert ejcdc(direction="Increase").total.value == 14000.0
    assert ejcdc(days="12 calendar days").schedule_impact_days.value == 12
    assert ejcdc(time_direction="Decrease", days="5 days").schedule_impact_days.value == -5


def test_issue_date_beats_effective_date():
    result = ejcdc()
    assert result.date.value == dt.date(2024, 11, 19)
    assert SEVERAL_DATES_NOTE not in result.date.notes


def test_next_column_label_is_cut_off():
    result = ejcdc()
    assert result.contractor.value == "J.G. Case Construction, Inc."
    assert result.owner.value == "City of Lawrence Utilities"


def test_contract_time_totals_are_not_the_schedule():
    result = extract_text("CHANGE ORDER 4\nSubstantial Completion: 360 calendar days\nTOTAL $5.00")
    assert result.schedule_impact_days.value is None


def test_printed_form_instructions_are_not_values():
    result = extract_text(
        "OWNER: (Name and address)\n"
        "PROJECT: (Name and address) CONTRACT INFORMATION: CHANGE ORDER INFORMATION:\n"
        "Description: (Insert a detailed description of the change and, if applicable)"
    )
    assert result.owner.value is None
    assert result.project_name.value is None
    assert result.description.value is None


def test_several_dates_on_one_line_are_noted():
    result = extract_text(
        "CHANGE ORDER 3\nMarshall, MN Date: November 18, 2019 Date: November 5, 2020"
    )

    assert result.date.value == dt.date(2019, 11, 18)
    assert SEVERAL_DATES_NOTE in result.date.notes
    assert result.date.confidence == 0.75


def test_attachment_pages_are_not_read():
    document = Document(
        source_file="co.pdf",
        source_type=SourceType.PDF_TEXT,
        pages=[
            Page(number=1, text="CHANGE ORDER 5\nTOTAL  $100.00"),
            Page(number=2, text="RE: backup\nJob #: 99881\nTOTAL: $999.00", is_attachment=True),
        ],
    )
    result = EXTRACTOR.extract(document)

    assert result.total.value == 100.00
    assert result.project_number.value is None


def test_reason_from_referenced_documents():
    result = extract_text(
        "CHANGE ORDER 10\nBrennan Change Order PR04 Steel Only  $11,129.00\n"
        "Fire Alarm changes per RFI 24  $6,522.00\nFraming for RFI 27 and RFI 24  $356.00"
    )
    assert result.reason.value == "PR04; RFI 24; RFI 27"  # in order, no duplicates
    assert REFERENCE_NOTE in result.reason.notes
    assert result.reason.confidence == 0.45  # a heuristic, below a labelled reason


def test_labelled_reason_wins_over_references():
    result = extract_text("CHANGE ORDER 1\nReason: Owner request\nper RFI 9")
    assert result.reason.value == "Owner request"


def test_lowercase_words_are_not_references():
    result = extract_text("CHANGE ORDER 1\nthe pr 12 team and asi 3 notes")
    assert result.reason.value is None


# ------------------------------------------------------------------ dashes in money lines


@pytest.mark.parametrize(
    ("line", "amount"),
    [
        ("Steel - $5,000.00", 5000.00),
        ("Slip connections $ - $9,700.00 $9,700.00", 9700.00),
        ("Structural steel S5,000.00", 5000.00),
        ("Credit for deleted work  ($8,400.00)", -8400.00),
    ],
)
def test_line_item_signs(line, amount):
    item = parse_item_line(line)
    assert item is not None
    assert item.amount == amount


def test_money_tokens_agree_with_the_parser_on_dashes():
    assert [t.parsed.value for t in money_tokens("Labor $ - $5,000.00")] == [5000.00]
    assert [t.parsed.value for t in money_tokens("Credit  -$500.00")] == [-500.00]
