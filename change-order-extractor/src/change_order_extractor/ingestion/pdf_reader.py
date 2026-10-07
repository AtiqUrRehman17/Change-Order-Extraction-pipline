"""Reads text PDFs with pdfplumber; falls back to Gemini vision for scans.

For each page we take the text layer, any tables, side-by-side labelled columns
of form headers and signature-area columns (see layout.py). A page with (almost) no
text is treated as scanned: we render it to a PNG so the LLM extractor can
read it with vision later.

A scan can still carry a little real text: agenda systems often stamp a footer
('Change Order No. 1 to C-24-0312-09-27 / 1 of 4') on every page. So a page that is
one big image with only a few lines of text is a scan too; its text is kept.
"""

import io
from pathlib import Path

import pdfplumber
from pdfplumber.page import Page as PlumberPage
from pdfplumber.utils.exceptions import PdfminerException

from change_order_extractor.ingestion.errors import IngestionError
from change_order_extractor.ingestion.layout import read_columns, read_signatures
from change_order_extractor.schemas import Document, Page, SourceType

# A page with fewer real characters than this is considered an image-only scan.
MIN_TEXT_CHARS = 20
# A page whose largest image covers this share of it, with fewer real characters
# than STAMPED_TEXT_CHARS (a stamped footer, not an OCR text layer), is a scan too.
SCAN_IMAGE_COVERAGE = 0.8
STAMPED_TEXT_CHARS = 200
# Resolution for rendering scanned pages. 200 DPI is sharp enough for vision
# models while keeping images small.
SCAN_RENDER_DPI = 200


def read_pdf(path: Path) -> Document:
    try:
        with pdfplumber.open(path) as pdf:
            pages = [read_page(page, number) for number, page in enumerate(pdf.pages, start=1)]
    except PdfminerException as error:  # corrupt, truncated or password-protected
        raise IngestionError(f"Could not open PDF {path.name}: {error}") from error

    if not pages:
        raise IngestionError(f"PDF has no pages: {path.name}")

    return Document(source_file=path.name, source_type=classify(pages), pages=pages)


def read_page(page: PlumberPage, number: int) -> Page:
    text = page.extract_text() or ""
    if not looks_scanned(page, text):
        return Page(
            number=number,
            text=text,
            tables=page.extract_tables(),
            columns=read_columns(page),
            signatures=read_signatures(page),
        )

    return Page(number=number, text=text, is_scanned=True, image_png=render_png(page))


def looks_scanned(page: PlumberPage, text: str) -> bool:
    chars = len("".join(text.split()))
    if chars < MIN_TEXT_CHARS:
        return True
    return chars < STAMPED_TEXT_CHARS and image_coverage(page) >= SCAN_IMAGE_COVERAGE


def image_coverage(page: PlumberPage) -> float:
    """Share of the page (0-1) covered by its largest image."""
    area = page.width * page.height
    if not area or not page.images:
        return 0.0
    largest = max(
        max(0.0, min(image["x1"], page.width) - max(image["x0"], 0.0))
        * max(0.0, min(image["bottom"], page.height) - max(image["top"], 0.0))
        for image in page.images
    )
    return largest / area


def render_png(page: PlumberPage) -> bytes:
    buffer = io.BytesIO()
    page.to_image(resolution=SCAN_RENDER_DPI).original.save(buffer, format="PNG")
    return buffer.getvalue()


def classify(pages: list[Page]) -> SourceType:
    scanned = sum(page.is_scanned for page in pages)
    if scanned == 0:
        return SourceType.PDF_TEXT
    if scanned == len(pages):
        return SourceType.PDF_SCANNED
    return SourceType.PDF_MIXED
