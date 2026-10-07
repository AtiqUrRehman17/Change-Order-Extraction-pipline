"""Regression tests on real, public change orders (rules only: offline, no API quota).

Generated samples can only test what we thought of. These documents were found
in city and county meeting agendas (see fixtures/real/SOURCES.md) and their
correct values were read by hand from page 1.

* Every value the system gets right today is asserted, so a change that breaks
  it is caught.
* Every known failure is an `xfail(strict=True)` test with its reason: the list
  of failure modes is written down, and if a later change fixes one, pytest
  reports it (XPASS -> failure) so the test is promoted to a normal one.
"""

import datetime as dt
from pathlib import Path

import pytest

from change_order_extractor.config import Settings
from change_order_extractor.extraction.llm_extractor import build_contents
from change_order_extractor.ingestion import load_document
from change_order_extractor.pipeline import Pipeline
from change_order_extractor.preprocessing import clean_document
from change_order_extractor.schemas import ChangeOrderStatus, ExtractionResult, SourceType

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
TODAY = dt.date(2026, 10, 6)


def run(file_name: str) -> ExtractionResult:
    settings = Settings(GEMINI_API_KEY=None, _env_file=None)
    return Pipeline(settings, use_llm=False, today=TODAY).run(REAL / file_name)


def codes(result: ExtractionResult) -> set[str]:
    return {issue.code for issue in result.validation.issues}


# ------------------------------------------------------------------ City of Marshall, MN
# AIA G701, Change Order 10: three side-by-side header columns, a signature area
# with a blank owner date, and seven pages of backup with their own TOTAL lines.


@pytest.fixture(scope="module")
def marshall() -> ExtractionResult:
    return run("marshall_mn_co10.pdf")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("change_order_number", "10"),
        ("date", dt.date(2020, 11, 5)),  # CHANGE ORDER column, not the contract's 2019 date
        ("project_name", "Marshall Municipal Building"),
        ("project_number", "777.01"),
        ("owner", "City of Marshall"),
        ("contractor", "Brennan Construction of MN, Inc."),
        ("total", 18007.00),  # AIA sentence, not a backup page's TOTAL
        ("schedule_impact_days", 0),
        ("status", ChangeOrderStatus.PENDING),  # owner's signature date is blank
        ("approval_date", None),  # 'January 8, 2021' is the architect's date
        ("reason", "PR04; RFI 24; RFI 27"),
    ],
)
def test_marshall_fields(marshall, field, expected):
    assert getattr(marshall.fields, field).value == expected


def test_marshall_line_items(marshall):
    assert [item.amount for item in marshall.fields.line_items.value] == [11129.0, 6522.0, 356.0]


def test_marshall_structure(marshall):
    assert marshall.document.attachment_pages == [2, 3, 4, 5, 6, 7, 8]
    assert marshall.validation.passed  # no false math errors from the backup pages
    assert "ambiguous_date" not in codes(marshall)  # settled by the CHANGE ORDER column
    assert all(
        getattr(marshall.fields, name).page == 1
        for name in ("change_order_number", "date", "owner", "total")
    )


# ------------------------------------------------------------------ Sumter County, FL
# A contractor's Change Order Request: scanned with an OCR text layer (':,cope of
# Work', 'TOTAl'), handwritten signatures, subcontractor quote and drawing attached.


@pytest.fixture(scope="module")
def sumter() -> ExtractionResult:
    return run("sumter_county_fl_notice9.pdf")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("date", dt.date(2024, 10, 28)),
        ("project_name", "Sumter County Supervisor of Elections"),
        ("owner", "Board of Sumter County Commissioners"),
        ("contractor", "Mark Cook Builders"),
        ("markup_amount", 970.00),
        ("total", 10670.00),
        ("reason", "ASI#5"),
    ],
)
def test_sumter_fields(sumter, field, expected):
    assert getattr(sumter.fields, field).value == expected


def test_sumter_attachments(sumter):
    assert sumter.document.attachment_pages == [2, 3]
    assert sumter.needs_review  # it does have real gaps (below), so it must be flagged


# Known failure modes (each one is a documented limitation of the rules).


@pytest.mark.xfail(strict=True, reason="the form labels it 'Notice No.', not 'Change Order No.'")
def test_sumter_change_order_number(sumter):
    assert sumter.fields.change_order_number.value == "9"


@pytest.mark.xfail(strict=True, reason="the $9,700 line is split over two OCR text lines")
def test_sumter_line_items(sumter):
    assert 9700.0 in [item.amount for item in sumter.fields.line_items.value or []]


@pytest.mark.xfail(strict=True, reason="'Will Contract time be affected by this change: no'")
def test_sumter_schedule(sumter):
    assert sumter.fields.schedule_impact_days.value == 0


@pytest.mark.xfail(strict=True, reason="this form's subtotal already includes the markup")
def test_sumter_no_false_math_errors(sumter):
    assert not {"subtotal_mismatch", "total_mismatch"} & codes(sumter)


@pytest.mark.xfail(strict=True, reason="'Date: 1/2/25' is the requester's signature date")
def test_sumter_date_is_not_ambiguous(sumter):
    assert "ambiguous_date" not in codes(sumter)


# ------------------------------------------------------------------ Hidalgo County, TX
# AIA G701 Change Order 1. Page 1 (the change order itself) is a scanned image with a
# text footer stamped on it by the agenda system; page 3 is a cost table (net-zero
# change); page 4 is a date-calculator printout.


@pytest.fixture(scope="module")
def hidalgo() -> ExtractionResult:
    return run("hidalgo_tx_co1.pdf")


def test_hidalgo_stamped_scans_go_to_gemini():
    """Pages 1 and 4 have 71 characters of footer text but are page-sized images:
    they must be sent to Gemini as images (they used to be read as text pages)."""
    document = clean_document(load_document(REAL / "hidalgo_tx_co1.pdf"))
    assert [page.number for page in document.pages if page.is_scanned] == [1, 4]
    assert document.source_type == SourceType.PDF_MIXED

    contents = build_contents(document)
    images = [part for part in contents[1:] if not isinstance(part, str)]
    assert len(images) == 2
    assert "Change Order No. 1 to C-24-0312-09-27" in contents[0]  # the footer text is kept


def test_hidalgo_rules_read_what_the_text_layer_has(hidalgo):
    assert hidalgo.fields.change_order_number.value == "1"  # from the stamped footer
    assert hidalgo.needs_review


@pytest.mark.xfail(strict=True, reason="page 1 is an image: the rules need local OCR")
def test_hidalgo_rules_read_the_scanned_page(hidalgo):
    assert hidalgo.fields.date.value == dt.date(2025, 10, 21)
    assert hidalgo.fields.total.value == 0.0


# ------------------------------------------------------------------ City of Lawrence
# EJCDC C-941 Change Order 08: 'Date of Issuance' next to 'Effective Date' on one line,
# and the price change written as 'Decrease of this Change Order: $14,000.00'.


@pytest.fixture(scope="module")
def lawrence() -> ExtractionResult:
    return run("lawrence_co8.pdf")


def test_lawrence_issue_date_beats_effective_date(lawrence):
    assert lawrence.fields.date.value == dt.date(2024, 11, 19)
    assert "ambiguous_date" not in codes(lawrence)  # the 'Date of Issuance' label settles it


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("change_order_number", "08"),
        ("status", ChangeOrderStatus.PENDING),
        # the next column's label ('Contractor's Project No.:') is cut off
        ("contractor", "J.G. Case Construction, Inc."),
        ("owner", "City of Lawrence Utilities"),
        # EJCDC 'Decrease of this Change Order:' with the amount two lines below
        ("total", -14000.0),
        # 'Substantial Completion: NA' under this change order, not the original 360 days
        ("schedule_impact_days", 0),
    ],
)
def test_lawrence_fields(lawrence, field, expected):
    assert getattr(lawrence.fields, field).value == expected


def test_lawrence_has_no_false_warnings(lawrence):
    assert lawrence.validation.issues == []  # the 'Decrease' quote contains -14,000
