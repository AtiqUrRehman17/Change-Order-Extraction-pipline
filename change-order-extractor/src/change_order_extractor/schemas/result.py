"""ExtractedField (value + confidence + evidence) and pipeline result models.

ChangeOrder holds plain values. The models here wrap every value with the
information we need to trust it:

    value       what we extracted (already normalized)
    confidence  0.0 - 1.0, how sure we are
    evidence    the exact text in the document it came from
    source      which extractor(s) found it
"""

import datetime as dt
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from change_order_extractor.schemas.change_order import ChangeOrder, ChangeOrderStatus, LineItem
from change_order_extractor.schemas.document import Document, SourceType

T = TypeVar("T")


class FieldSource(StrEnum):
    RULES = "rules"  # only the regex extractor found it
    LLM = "llm"  # only Gemini found it
    BOTH = "both"  # both found it and agreed
    CONFLICT = "conflict"  # both found it but disagreed
    NONE = "none"  # nobody found it


class ExtractedField(BaseModel, Generic[T]):
    """One extracted value plus everything that says how much to trust it."""

    model_config = ConfigDict(extra="forbid")

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str | None = Field(default=None, description="Exact quote from the document.")
    page: int | None = Field(
        default=None, description="Page the evidence was found on (None if not located)."
    )
    source: FieldSource = FieldSource.NONE
    notes: list[str] = Field(default_factory=list, description="Why the confidence is what it is.")


class ExtractedChangeOrder(BaseModel):
    """Same fields as ChangeOrder, but each one is an ExtractedField."""

    model_config = ConfigDict(extra="forbid")

    change_order_number: ExtractedField[str] = Field(default_factory=ExtractedField[str])
    project_name: ExtractedField[str] = Field(default_factory=ExtractedField[str])
    project_number: ExtractedField[str] = Field(default_factory=ExtractedField[str])
    date: ExtractedField[dt.date] = Field(default_factory=ExtractedField[dt.date])

    contractor: ExtractedField[str] = Field(default_factory=ExtractedField[str])
    owner: ExtractedField[str] = Field(default_factory=ExtractedField[str])

    description: ExtractedField[str] = Field(default_factory=ExtractedField[str])
    reason: ExtractedField[str] = Field(default_factory=ExtractedField[str])

    line_items: ExtractedField[list[LineItem]] = Field(
        default_factory=ExtractedField[list[LineItem]]
    )
    subtotal: ExtractedField[float] = Field(default_factory=ExtractedField[float])
    markup_amount: ExtractedField[float] = Field(default_factory=ExtractedField[float])
    tax_amount: ExtractedField[float] = Field(default_factory=ExtractedField[float])
    total: ExtractedField[float] = Field(default_factory=ExtractedField[float])

    schedule_impact_days: ExtractedField[int] = Field(default_factory=ExtractedField[int])

    status: ExtractedField[ChangeOrderStatus] = Field(
        default_factory=ExtractedField[ChangeOrderStatus]
    )
    requested_by: ExtractedField[str] = Field(default_factory=ExtractedField[str])
    approved_by: ExtractedField[str] = Field(default_factory=ExtractedField[str])
    approval_date: ExtractedField[dt.date] = Field(default_factory=ExtractedField[dt.date])

    def to_change_order(self) -> ChangeOrder:
        """Drop confidence/evidence and return just the values."""
        values = {name: getattr(self, name).value for name in type(self).model_fields}
        if values["line_items"] is None:
            values["line_items"] = []
        if values["status"] is None:
            values["status"] = ChangeOrderStatus.UNKNOWN
        return ChangeOrder(**values)

    @classmethod
    def from_change_order(cls, change_order: ChangeOrder) -> "ExtractedChangeOrder":
        """Wrap plain values (no confidence or evidence). Handy for tests and ground truth."""
        fields = {}
        for name in cls.model_fields:
            value = getattr(change_order, name)
            if value in (None, []) or value == ChangeOrderStatus.UNKNOWN:
                continue
            fields[name] = {"value": value}
        return cls.model_validate(fields)


class Severity(StrEnum):
    ERROR = "error"  # data is wrong or missing something required
    WARNING = "warning"  # data looks suspicious but may be fine


class ValidationIssue(BaseModel):
    """One problem found during validation."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Short machine-readable id, e.g. 'total_mismatch'.")
    message: str = Field(description="Plain-English explanation.")
    severity: Severity = Severity.ERROR
    fields: list[str] = Field(default_factory=list, description="Fields involved.")


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool = Field(default=True, description="True when there are no errors.")
    issues: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def from_issues(cls, issues: list[ValidationIssue]) -> "ValidationReport":
        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return cls(passed=passed, issues=issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == Severity.WARNING]

    def issues_for(self, field_name: str) -> list[ValidationIssue]:
        """Issues that involve one field (used by confidence scoring)."""
        return [issue for issue in self.issues if field_name in issue.fields]


class DocumentInfo(BaseModel):
    """Where the data came from."""

    model_config = ConfigDict(extra="forbid")

    source_file: str
    source_type: SourceType
    page_count: int | None = None
    attachment_pages: list[int] = Field(
        default_factory=list, description="Pages detected as attached backup documents."
    )

    @classmethod
    def from_document(cls, document: Document) -> "DocumentInfo":
        return cls(
            source_file=document.source_file,
            source_type=document.source_type,
            page_count=document.page_count,
            attachment_pages=document.attachment_pages,
        )


class ExtractionResult(BaseModel):
    """The final output of the pipeline for one document."""

    model_config = ConfigDict(extra="forbid")

    document: DocumentInfo
    fields: ExtractedChangeOrder
    validation: ValidationReport = Field(default_factory=ValidationReport)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = True
    review_reasons: list[str] = Field(
        default_factory=list, description="Why a human should check this document."
    )
    extractors_used: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, description="LLM model that answered, if any.")
    warnings: list[str] = Field(
        default_factory=list,
        description="Pipeline problems, e.g. 'Gemini unavailable; used rules only'.",
    )
    processed_at: dt.datetime = Field(default_factory=dt.datetime.now)
    processing_seconds: float | None = None
