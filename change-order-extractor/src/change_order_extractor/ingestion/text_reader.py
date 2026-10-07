"""Reads plain .txt files."""

from pathlib import Path

from change_order_extractor.ingestion.errors import IngestionError
from change_order_extractor.schemas import Document, Page, SourceType

# Tried in order. utf-8-sig also strips the BOM that Windows tools often add.
# latin-1 accepts any byte, so it is the last-resort fallback.
ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def decode(raw: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError("unreachable: latin-1 decodes any bytes")


def read_text(path: Path) -> Document:
    text = decode(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise IngestionError(f"File is empty: {path.name}")

    return Document(
        source_file=path.name,
        source_type=SourceType.TEXT,
        pages=[Page(number=1, text=text)],
    )
