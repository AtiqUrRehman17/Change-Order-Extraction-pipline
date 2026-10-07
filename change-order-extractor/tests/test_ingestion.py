"""Tests for ingestion (reading files into Documents)."""

import json
from pathlib import Path

import pytest

from change_order_extractor.ingestion import IngestionError, load_document
from change_order_extractor.schemas import DocumentInfo, SourceType

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------ text files


def test_reads_text_file():
    doc = load_document(SAMPLES / "co_001_clean.txt")

    assert doc.source_type == SourceType.TEXT
    assert doc.page_count == 1
    assert "CO-014" in doc.text
    assert not doc.needs_vision


def test_reads_windows_encoded_text(tmp_path):
    path = tmp_path / "co.txt"
    path.write_bytes("Total: 1,000 – café\r\n".encode("cp1252"))

    doc = load_document(path)

    assert doc.text == "Total: 1,000 – café\n"


def test_strips_utf8_bom(tmp_path):
    path = tmp_path / "co.txt"
    path.write_bytes(b"\xef\xbb\xbfCHANGE ORDER")

    assert load_document(path).text == "CHANGE ORDER"


# ------------------------------------------------------------------ PDFs


def test_reads_text_pdf():
    doc = load_document(SAMPLES / "co_016_weeks.pdf")

    assert doc.source_type == SourceType.PDF_TEXT
    assert "Silver Creek Warehouse" in doc.text
    assert not doc.needs_vision


def test_reads_every_page_of_multi_page_pdf():
    doc = load_document(SAMPLES / "co_009_multi_page.pdf")

    assert doc.page_count == 2
    assert "Acoustic panels" in doc.pages[0].text
    assert "15,765.20" in doc.pages[1].text


def test_extracts_tables_from_pdf():
    doc = load_document(SAMPLES / "co_002_table.pdf")

    table = doc.pages[0].tables[0]
    assert table[0] == ["Item", "Description", "Qty", "Unit", "Unit Price", "Amount"]
    assert table[1][-1] == "$1,125.00"


def test_scanned_pdf_is_flagged_and_rendered():
    doc = load_document(SAMPLES / "co_008_scanned.pdf")

    assert doc.source_type == SourceType.PDF_SCANNED
    assert doc.needs_vision
    assert doc.text == ""
    assert len(doc.scanned_images) == 1
    assert doc.scanned_images[0].startswith(PNG_SIGNATURE)


def full_page_scan_pdf(path: Path, footer_lines: list[str]) -> Path:
    """A one-page PDF: a page-sized image with some real text stamped on top."""
    from PIL import Image
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    canvas = Canvas(str(path), pagesize=LETTER)
    canvas.drawImage(ImageReader(Image.new("L", (300, 400), 240)), 0, 0, *LETTER)
    for number, line in enumerate(footer_lines):
        canvas.drawString(72, 60 - 12 * number, line)
    canvas.save()
    return path


def test_scan_with_stamped_footer_is_still_a_scan(tmp_path):
    """Agenda systems stamp a footer on scanned pages; the text layer is then a few
    lines, not the page's content, so the page must still go to Gemini as an image."""
    footer = ["Change Order No. 1 to C-24-0312-09-27", "HC & RGV Construction, LLC", "1 of 4"]
    doc = load_document(full_page_scan_pdf(tmp_path / "stamped.pdf", footer))

    page = doc.pages[0]
    assert page.is_scanned and page.image_png.startswith(PNG_SIGNATURE)
    assert "Change Order No. 1" in page.text  # the stamped text is kept
    assert doc.source_type == SourceType.PDF_SCANNED


def test_image_page_with_a_full_text_layer_is_not_a_scan(tmp_path):
    """An OCR'd scan (image + full text layer) is read as text, like before."""
    lines = [f"Line {n}: labor and material for the added work, $1,{n:03d}.00" for n in range(8)]
    doc = load_document(full_page_scan_pdf(tmp_path / "ocr.pdf", lines))
    assert not doc.pages[0].is_scanned


# ------------------------------------------------------------------ errors


def test_missing_file(tmp_path):
    with pytest.raises(IngestionError, match="not found"):
        load_document(tmp_path / "nope.txt")


def test_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("")
    with pytest.raises(IngestionError, match="empty"):
        load_document(path)


def test_whitespace_only_file(tmp_path):
    path = tmp_path / "blank.txt"
    path.write_text("   \n\n  ")
    with pytest.raises(IngestionError, match="empty"):
        load_document(path)


def test_unsupported_file_type(tmp_path):
    path = tmp_path / "co.docx"
    path.write_bytes(b"data")
    with pytest.raises(IngestionError, match="Unsupported"):
        load_document(path)


def test_corrupt_pdf(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf")
    with pytest.raises(IngestionError, match="Could not open PDF"):
        load_document(path)


# ------------------------------------------------------------------ all samples


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_every_sample_loads(entry):
    doc = load_document(SAMPLES / entry["file"])

    if entry["format"] == "pdf_scan":
        assert doc.needs_vision
    else:
        assert doc.text.strip()
        assert not doc.needs_vision

    info = DocumentInfo.from_document(doc)
    assert info.source_file == entry["file"]
    assert info.page_count == doc.page_count
