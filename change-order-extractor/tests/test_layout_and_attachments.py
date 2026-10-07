"""Side-by-side form columns, attached backup pages, and the fields that depend on them.

The fixture builds a small AIA-G701-like PDF:
    page 1  the change order: three labelled header columns (PROJECT / CONTRACT
            INFORMATION / CHANGE ORDER INFORMATION, then OWNER / ARCHITECT /
            CONTRACTOR), two 'Date:' values on one line, line items and the AIA
            "in the amount of" sentence
    page 2  an attached cost memo with its own TOTAL, Subtotal and Job #
"""

import datetime as dt

import pdfplumber
import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen.canvas import Canvas

from change_order_extractor.config import Settings
from change_order_extractor.extraction import RuleBasedExtractor
from change_order_extractor.ingestion import load_document
from change_order_extractor.ingestion.layout import read_signatures
from change_order_extractor.pipeline import Pipeline
from change_order_extractor.preprocessing import (
    clean_document,
    mark_attachments,
    starts_new_document,
)
from change_order_extractor.schemas import ChangeOrderStatus, Document, Page, SourceType
from change_order_extractor.validation import validate

TODAY = dt.date(2026, 10, 6)


@pytest.fixture(scope="module")
def form_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("forms") / "aia_form.pdf"
    canvas = Canvas(str(path), pagesize=LETTER)
    canvas.setFont("Helvetica", 9)

    def row(y, *cells):
        for x, text in zip((72, 250, 420), cells, strict=False):
            if text:
                canvas.drawString(x, y, text)

    canvas.drawString(72, 750, "Change Order")
    row(720, "PROJECT: (Name and address)", "CONTRACT INFORMATION:", "CHANGE ORDER INFORMATION:")
    row(708, "501 Lakeside Library", "Contract For: General Construction", "Change Order Number: 4")
    row(696, "Lakeside, MN", "Date: March 2, 2023", "Date: June 9, 2024")
    row(
        660,
        "OWNER: (Name and address)",
        "ARCHITECT: (Name and address)",
        "CONTRACTOR: (Name and address)",
    )
    row(648, "City of Lakeside", "North Design Group", "Granite Builders Inc.")
    canvas.drawString(72, 600, "THE CONTRACT IS CHANGED AS FOLLOWS:")
    canvas.drawString(
        72, 588, "(Insert a detailed description of the change and, if applicable, attach)"
    )
    canvas.drawString(72, 576, "Add exit signs at stairwells")
    canvas.drawString(300, 576, "$2,000.00")
    canvas.drawString(72, 564, "Relocate electrical panel")
    canvas.drawString(300, 564, "$1,500.00")
    canvas.drawString(72, 530, "The original Contract Sum was $ 900,000.00")
    canvas.drawString(
        72,
        518,
        "The Contract Sum will be increased by this Change Order in the amount of $ 3,500.00",
    )
    canvas.drawString(72, 506, "The Contract Time will be increased by Zero (0) days.")
    canvas.showPage()

    canvas.setFont("Helvetica", 9)
    for y, text in [
        (750, "DATE: May 1, 2024"),
        (738, "TO: North Design Group"),
        (726, "FROM: Granite Builders"),
        (714, "RE: Exit signs"),
        (690, "Job #: 99881"),
        (670, "Electrician  $1,800.00"),
        (658, "Subtotal: $1,800.00"),
        (646, "TOTAL: $2,000.00"),
    ]:
        canvas.drawString(72, y, text)
    canvas.save()
    return path


@pytest.fixture(scope="module")
def prepared(form_pdf):
    return mark_attachments(clean_document(load_document(form_pdf)))


# ------------------------------------------------------------------ columns (layout.py)


def test_columns_are_read_by_position(prepared):
    columns = {block.label: block.lines for block in prepared.pages[0].columns}

    assert columns["CHANGE ORDER INFORMATION"] == ["Change Order Number: 4", "Date: June 9, 2024"]
    assert columns["CONTRACT INFORMATION"] == [
        "Contract For: General Construction",
        "Date: March 2, 2023",
    ]
    assert columns["PROJECT"] == ["501 Lakeside Library", "Lakeside, MN"]
    assert columns["OWNER"] == ["City of Lakeside"]
    assert columns["CONTRACTOR"] == ["Granite Builders Inc."]


def test_ordinary_pages_have_no_columns(prepared):
    assert prepared.pages[1].columns == []


# ------------------------------------------------------------------ attachments (segmenter.py)


def test_memo_page_is_an_attachment(prepared):
    assert prepared.attachment_pages == [2]
    assert "TOTAL: $2,000.00" not in prepared.main_text
    assert "TOTAL: $2,000.00" in prepared.text


def page(number: int, text: str, **kwargs) -> Page:
    return Page(number=number, text=text, **kwargs)


@pytest.mark.parametrize(
    ("text", "new_document"),
    [
        ("DATE: Sept 18, 2020\nTO: Andy Engan\nFROM: John van Dyck\nRE: PR 04", True),
        ("QUOTE\nSparky's Welding\nTO: Brennan", True),
        ("Change Order Request\nBergh's Fabricating\nPage 1 of 1", True),
        ("CHANGE ORDER 12 - page 2 of 2\nTOTAL THIS CHANGE ORDER $15,765.20", False),
        ("Schedule Impact: 5 working days\nOWNER: Northside Parks  By: ____", False),
    ],
)
def test_starts_new_document(text, new_document):
    assert starts_new_document(page(2, text)) is new_document


def test_pages_after_an_attachment_stay_attachments_and_page_one_never_is():
    document = Document(
        source_file="x.pdf",
        source_type=SourceType.PDF_MIXED,
        pages=[
            page(1, "QUOTE (the change order itself starts with this word)"),
            page(2, "", is_scanned=True),  # e.g. a scanned signature page: stays main
            page(3, "DATE: 1/1/2024\nTO: Owner\nFROM: Contractor"),
            page(4, "more backup with no header"),
        ],
    )
    marked = mark_attachments(document)
    assert [p.is_attachment for p in marked.pages] == [False, False, True, True]


# ------------------------------------------------------------------ rules on the form


def test_rules_read_the_change_order_not_the_attachment(prepared):
    result = RuleBasedExtractor().extract(prepared)

    assert result.change_order_number.value == "4"
    assert result.date.value == dt.date(2024, 6, 9)  # the CHANGE ORDER column, not the contract
    assert result.project_name.value == "Lakeside Library"
    assert result.project_number.value == "501"  # not the memo's 'Job #: 99881'
    assert result.owner.value == "City of Lakeside"  # not '(Name and address)'
    assert result.contractor.value == "Granite Builders Inc."
    assert [item.amount for item in result.line_items.value] == [2000.00, 1500.00]
    assert result.total.value == 3500.00  # the AIA sentence, not the memo's TOTAL
    assert result.subtotal.value is None  # the memo's subtotal is ignored
    assert result.schedule_impact_days.value == 0
    assert result.description.value is None  # printed instructions are not a description


# ------------------------------------------------------------------ the whole pipeline


def test_pipeline_on_the_form(form_pdf):
    settings = Settings(GEMINI_API_KEY=None, _env_file=None)
    result = Pipeline(settings, use_llm=False, today=TODAY).run(form_pdf)

    assert result.document.attachment_pages == [2]
    assert result.fields.total.value == 3500.00
    assert result.fields.total.page == 1
    assert result.fields.owner.page == 1
    codes = [issue.code for issue in result.validation.issues]
    assert "subtotal_mismatch" not in codes and "total_mismatch" not in codes
    # Two 'Date:' values, but the date came from the CHANGE ORDER column: settled.
    assert "ambiguous_date" not in codes


def test_contract_date_on_the_form_is_still_flagged(prepared):
    """If a date other than the CHANGE ORDER column's were extracted, it is flagged."""
    fields = RuleBasedExtractor().extract(prepared)
    fields.date.value = dt.date(2023, 3, 2)  # the CONTRACT INFORMATION date
    report = validate(fields, prepared, today=TODAY)

    assert "ambiguous_date" in [issue.code for issue in report.issues]


# ------------------------------------------------------------------ signature areas


def signed_form(path, owner_date: str | None):
    """AIA-2017-style signature area: firm above the role, captions below the values."""
    canvas = Canvas(str(path), pagesize=LETTER)
    canvas.setFont("Helvetica", 9)

    def row(y, *cells):
        for x, text in zip((72, 250, 420), cells, strict=False):
            if text:
                canvas.drawString(x, y, text)

    canvas.drawString(72, 750, "CHANGE ORDER 7")
    canvas.drawString(72, 738, "Total of this change order  $1,000.00")
    row(600, "North Design Group", "Granite Builders Inc.", "City of Lakeside")
    row(589, "ARCHITECT (Firm name)", "CONTRACTOR (Firm name)", "OWNER (Firm name)")
    row(560, "SIGNATURE", "SIGNATURE", "SIGNATURE")
    row(544, "Ann Lee, AIA", "Sam Ortiz", "Maria Gomez, City Manager")
    row(528, "PRINTED NAME AND TITLE", "PRINTED NAME AND TITLE", "PRINTED NAME AND TITLE")
    row(512, "January 8, 2021", None, owner_date)
    row(500, "DATE", "DATE", "DATE")
    canvas.save()
    return path


def test_signature_columns_are_read(tmp_path):
    with pdfplumber.open(signed_form(tmp_path / "s.pdf", owner_date=None)) as pdf:
        blocks = {block.role: block for block in read_signatures(pdf.pages[0])}

    assert blocks["OWNER"].firm == "City of Lakeside"
    assert blocks["OWNER"].name == "Maria Gomez, City Manager"
    assert blocks["OWNER"].date is None
    assert blocks["ARCHITECT"].date == "January 8, 2021"  # not the owner's date


def test_blank_owner_date_means_pending(tmp_path):
    document = mark_attachments(
        clean_document(load_document(signed_form(tmp_path / "a.pdf", None)))
    )
    result = RuleBasedExtractor().extract(document)

    assert result.status.value == ChangeOrderStatus.PENDING
    assert result.approved_by.value is None
    assert result.approval_date.value is None  # the architect's date is not used


def test_dated_owner_signature_means_approved(tmp_path):
    path = signed_form(tmp_path / "b.pdf", owner_date="February 2, 2021")
    result = RuleBasedExtractor().extract(mark_attachments(clean_document(load_document(path))))

    assert result.status.value == ChangeOrderStatus.APPROVED
    assert result.approved_by.value == "Maria Gomez"  # title removed
    assert result.approval_date.value == dt.date(2021, 2, 2)


def test_approval_not_on_the_form_is_flagged_for_review(tmp_path):
    document = mark_attachments(
        clean_document(load_document(signed_form(tmp_path / "c.pdf", None)))
    )
    fields = RuleBasedExtractor().extract(document)
    # What a weak model said: 'approved', using the architect's date.
    fields.status.value = ChangeOrderStatus.APPROVED
    fields.approval_date.value = dt.date(2021, 1, 8)
    report = validate(fields, document, today=TODAY)

    [issue] = [i for i in report.issues if i.code == "approval_not_confirmed"]
    assert "owner's signature date on the form is blank" in issue.message
    assert set(issue.fields) == {"status", "approved_by", "approval_date"}
