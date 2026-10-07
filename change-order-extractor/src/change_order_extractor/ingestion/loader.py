"""Detects the file type and picks the right reader."""

from collections.abc import Callable
from pathlib import Path

from change_order_extractor.ingestion.errors import IngestionError
from change_order_extractor.ingestion.pdf_reader import read_pdf
from change_order_extractor.ingestion.text_reader import read_text
from change_order_extractor.schemas import Document

READERS: dict[str, Callable[[Path], Document]] = {
    ".txt": read_text,
    ".text": read_text,
    ".md": read_text,
    ".eml": read_text,
    ".pdf": read_pdf,
}

SUPPORTED_EXTENSIONS = tuple(READERS)


def load_document(path: str | Path) -> Document:
    """Read any supported file into a Document."""
    path = Path(path)
    if not path.is_file():
        raise IngestionError(f"File not found: {path}")
    if path.stat().st_size == 0:
        raise IngestionError(f"File is empty: {path.name}")

    reader = READERS.get(path.suffix.lower())
    if reader is None:
        supported = ", ".join(SUPPORTED_EXTENSIONS)
        raise IngestionError(f"Unsupported file type '{path.suffix}'. Supported: {supported}")

    return reader(path)
