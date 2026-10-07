"""Document and Page models: what ingestion produces from an input file.

Every reader (text, PDF) returns the same Document shape, so the rest of the
pipeline never needs to know what kind of file it started from.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceType(StrEnum):
    TEXT = "text"  # .txt / .md / .eml
    PDF_TEXT = "pdf_text"  # PDF with a real text layer
    PDF_SCANNED = "pdf_scanned"  # every page is an image (needs vision)
    PDF_MIXED = "pdf_mixed"  # some pages have text, some are images


class ColumnBlock(BaseModel):
    """One labelled column of a side-by-side form header, read by word position.

    In forms like AIA G701 three labels share a line and each value sits *below*
    its label:

        OWNER: (Name and address)   ARCHITECT: (Name and address)   CONTRACTOR: ...
        City of Marshall            Engan Associates                Brennan Construction ...

    gives ColumnBlock(label="OWNER", lines=["City of Marshall", ...]), etc.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Upper-case label without the colon, e.g. 'OWNER'.")
    lines: list[str] = Field(default_factory=list, description="Text below the label, in order.")


class SignatureBlock(BaseModel):
    """One column of a form's signature area, read by word position.

        Engan Associates          ...   City of Marshall
        ARCHITECT (Firm name)     ...   OWNER (Firm name)
        SIGNATURE                       SIGNATURE
        Andy M. Engan, AIA        ...   Scott VanDerMillen, Owner
        PRINTED NAME AND TITLE          PRINTED NAME AND TITLE
        January 8, 2021                 (blank)
        DATE                            DATE

    gives SignatureBlock(role="OWNER", firm="City of Marshall",
    name="Scott VanDerMillen, Owner", date=None). A blank date matters: a printed
    name alone does not show that the form was signed.
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field(description="'OWNER', 'CONTRACTOR', 'ARCHITECT' or 'ENGINEER'.")
    firm: str | None = None
    name: str | None = Field(default=None, description="Printed name (and title) as written.")
    date: str | None = Field(default=None, description="Date written in the block, as text.")


class Page(BaseModel):
    """One page of the input. A text file is a single page."""

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    text: str = ""
    tables: list[list[list[str | None]]] = Field(
        default_factory=list, description="Tables found on the page: rows of cells."
    )
    columns: list[ColumnBlock] = Field(
        default_factory=list, description="Side-by-side labelled columns (PDF forms only)."
    )
    signatures: list[SignatureBlock] = Field(
        default_factory=list, description="Signature-area columns (PDF forms only)."
    )
    is_scanned: bool = Field(default=False, description="True if the page is only an image.")
    is_attachment: bool = Field(
        default=False,
        description="True if the page belongs to a backup document (memo, quote...) attached "
        "after the change order itself.",
    )
    image_png: bytes | None = Field(
        default=None, repr=False, description="Rendered page image, kept for scanned pages."
    )


class Document(BaseModel):
    """The input file turned into text (plus page images where text is missing)."""

    model_config = ConfigDict(extra="forbid")

    source_file: str
    source_type: SourceType
    pages: list[Page]

    @property
    def text(self) -> str:
        """All page text joined together."""
        return "\n\n".join(page.text for page in self.pages if page.text)

    @property
    def main_pages(self) -> list[Page]:
        """The change order itself (everything except attached backup documents)."""
        return [page for page in self.pages if not page.is_attachment]

    @property
    def main_text(self) -> str:
        """Text of the change order itself, without attachments."""
        return "\n\n".join(page.text for page in self.main_pages if page.text)

    @property
    def attachment_pages(self) -> list[int]:
        return [page.number for page in self.pages if page.is_attachment]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def needs_vision(self) -> bool:
        """True if any page has no text and must be read from its image."""
        return any(page.is_scanned for page in self.pages)

    @property
    def scanned_images(self) -> list[bytes]:
        return [page.image_png for page in self.pages if page.is_scanned and page.image_png]
