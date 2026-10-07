"""Gemini-based extractor with structured JSON output and evidence.

Flow:
    Document -> prompt (+ page images for scans) -> Gemini (JSON schema enforced)
             -> LLMChangeOrder (raw values + evidence + certainty)
             -> our own normalizers -> ExtractedChangeOrder

Gemini returns values *as written*; we normalize them with the same code the
rule-based extractor uses, so both extractors produce comparable values.

Pages detected as attachments (backup memos, quotes) are labelled as such in
the prompt, so the model reads the change order itself first. `extract_with_models`
lets the pipeline ask specific (stronger) models for a second opinion.
"""

import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

from change_order_extractor.config import Settings, get_settings
from change_order_extractor.extraction.base import Extractor
from change_order_extractor.extraction.fields import make_field, make_text_field, squash
from change_order_extractor.extraction.prompts import (
    ATTACHMENT_NOTE,
    PROMPT_VERSION,
    SCAN_NOTE,
    SYSTEM_INSTRUCTION,
    USER_PROMPT,
)
from change_order_extractor.normalization import parse_date, parse_days, parse_money
from change_order_extractor.normalization.money import CREDIT_WORDS
from change_order_extractor.schemas import (
    ChangeOrderStatus,
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    FieldSource,
    LineItem,
    LineItemCategory,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# If the server asks us to wait longer than this, the quota is gone (not a brief spike).
MAX_SERVER_WAIT_SECONDS = 60
SCAN_NOTE_TEXT = "read from a scanned image"


class LLMExtractionError(Exception):
    """Gemini could not produce a usable answer (after retries)."""


class ModelUnavailableError(LLMExtractionError):
    """One model kept failing with retryable errors (overloaded, rate-limited)."""


# ------------------------------------------------------------------ response schema
# What Gemini must return. Kept flat and simple (all strings) so the model only has
# to read and quote; parsing numbers and dates is our job.


class Certainty(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


CERTAINTY_CONFIDENCE = {Certainty.HIGH: 0.85, Certainty.MEDIUM: 0.65, Certainty.LOW: 0.45}


class LLMField(BaseModel):
    value: str | None = Field(description="Value as written in the document, or null.")
    evidence: str | None = Field(description="Exact verbatim quote containing the value.")
    certainty: Certainty


class LLMStatus(BaseModel):
    value: ChangeOrderStatus
    evidence: str | None
    certainty: Certainty


class LLMLineItem(BaseModel):
    description: str
    category: LineItemCategory
    quantity: str | None
    unit: str | None
    unit_price: str | None
    amount: str = Field(description="Line amount as written, e.g. '$1,200.00' or '($500)'.")
    evidence: str
    certainty: Certainty


class LLMChangeOrder(BaseModel):
    change_order_number: LLMField
    project_name: LLMField
    project_number: LLMField
    date: LLMField
    contractor: LLMField
    owner: LLMField
    description: LLMField
    reason: LLMField
    line_items: list[LLMLineItem]
    subtotal: LLMField
    markup_amount: LLMField
    tax_amount: LLMField
    total: LLMField
    schedule_impact: LLMField
    status: LLMStatus
    requested_by: LLMField
    approved_by: LLMField
    approval_date: LLMField


# ------------------------------------------------------------------ extractor


class GeminiExtractor(Extractor):
    name = "llm"

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.gemini_model
        self.models = [self.model] + [m for m in self.settings.fallback_models if m != self.model]
        self.last_model: str | None = None  # the model that produced the last answer
        self.sleep = sleep
        if client is None:
            if self.settings.gemini_api_key is None:
                raise LLMExtractionError(
                    "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
                )
            client = genai.Client(
                api_key=self.settings.gemini_api_key.get_secret_value(),
                http_options=types.HttpOptions(timeout=self.settings.llm_timeout_seconds * 1000),
            )
        self.client = client

    def extract(self, document: Document) -> ExtractedChangeOrder:
        return to_extracted(self.ask(document), document)

    def extract_with_models(self, document: Document, models: list[str]) -> ExtractedChangeOrder:
        """Ask the given models (in order) instead of the configured ones. Used for the
        optional second opinion: one attempt per model, no retries, so a busy model
        (503) costs seconds, not minutes."""
        return to_extracted(self.ask(document, models, retries=0), document)

    # -------------------------------------------------------------- calling Gemini

    def ask(
        self, document: Document, models: list[str] | None = None, retries: int | None = None
    ) -> LLMChangeOrder:
        """Get the structured answer for a document (from cache or Gemini).
        retries: per model; None = the configured COX_LLM_MAX_RETRIES."""
        models = models or self.models
        key = cache_key(models[0], document)
        if (cached := self.read_cache(key)) is not None:
            self.last_model = cached.model
            return cached.response

        contents = build_contents(document)
        errors_seen: list[str] = []
        for model in models:
            try:
                response = self.call_with_retries(model, contents, retries)
            except ModelUnavailableError as error:
                errors_seen.append(str(error))
                logger.warning("%s; trying the next model", error)
                continue
            self.last_model = model
            self.write_cache(key, CachedAnswer(model=model, response=response))
            return response

        raise LLMExtractionError("All Gemini models failed: " + " | ".join(errors_seen))

    def call_with_retries(
        self, model: str, contents: list[Any], retries: int | None = None
    ) -> LLMChangeOrder:
        attempts = (self.settings.llm_max_retries if retries is None else retries) + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                return self.call_once(model, contents)
            except errors.APIError as error:
                if error.code not in RETRYABLE_STATUS:
                    raise LLMExtractionError(f"Gemini API error {error.code}: {error}") from error
                server_delay = retry_delay_seconds(error)
                if server_delay is not None and server_delay > MAX_SERVER_WAIT_SECONDS:
                    # e.g. the free tier's daily request quota is used up. Retrying only
                    # burns more requests, so give up on this model straight away.
                    raise ModelUnavailableError(
                        f"{model} quota exhausted (retry in {server_delay / 3600:.1f}h)"
                    ) from error
                last_error = error
            except (ValidationError, ValueError) as error:  # malformed / incomplete JSON
                server_delay = None
                last_error = error

            if attempt < attempts - 1:
                # Use the server's suggested wait if it gave one; otherwise exponential
                # backoff with jitter: ~2s, 4s, 8s... (with the default base).
                delay = server_delay or round(
                    self.settings.llm_retry_base_seconds * 2**attempt * random.uniform(1, 1.5), 1
                )
                logger.warning(
                    "%s attempt %d failed (%s); retrying in %ss",
                    model,
                    attempt + 1,
                    last_error,
                    delay,
                )
                self.sleep(delay)

        raise ModelUnavailableError(f"{model} failed after {attempts} attempts: {last_error}")

    def call_once(self, model: str, contents: list[Any]) -> LLMChangeOrder:
        response = self.client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=LLMChangeOrder,
                temperature=self.settings.llm_temperature,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, LLMChangeOrder):
            return parsed
        if not response.text:
            raise ValueError("Gemini returned an empty response")
        return LLMChangeOrder.model_validate_json(response.text)

    # -------------------------------------------------------------- cache

    def cache_path(self, key: str) -> Path | None:
        directory = self.settings.llm_cache_dir
        return directory / f"{key}.json" if directory else None

    def read_cache(self, key: str) -> "CachedAnswer | None":
        path = self.cache_path(key)
        if path is None or not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        try:
            return CachedAnswer.model_validate_json(raw)
        except ValidationError:
            pass
        try:  # older cache format: just the response, from the main model
            return CachedAnswer(model=self.model, response=LLMChangeOrder.model_validate_json(raw))
        except ValidationError:
            return None  # stale or corrupt entry: ask again

    def write_cache(self, key: str, answer: "CachedAnswer") -> None:
        path = self.cache_path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(answer.model_dump_json(indent=2), encoding="utf-8")


class CachedAnswer(BaseModel):
    model: str
    response: LLMChangeOrder


def retry_delay_seconds(error: errors.APIError) -> float | None:
    """The wait the server suggests in a 429 (RetryInfo.retryDelay, e.g. '42646s')."""
    details = error.details if isinstance(error.details, dict) else {}
    for detail in details.get("error", {}).get("details", []) or []:
        if str(detail.get("@type", "")).endswith("RetryInfo"):
            try:
                return float(str(detail.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                return None
    return None


# ------------------------------------------------------------------ request building


def document_text(document: Document) -> str:
    parts = []
    for page in document.pages:
        body = page.text
        if page.is_scanned:
            stamped = page.text.strip()
            body = "[scanned image - see attached]" + (
                f"\n(text layer of the scan, e.g. a stamped footer:)\n{stamped}" if stamped else ""
            )
        kind = " - ATTACHMENT (backup document)" if page.is_attachment else ""
        parts.append(f"[Page {page.number}{kind}]\n{body}")
    return "\n\n".join(parts)


def build_contents(document: Document) -> list[Any]:
    scanned = [page.number for page in document.pages if page.is_scanned]
    notes = []
    if scanned:
        notes.append(SCAN_NOTE.format(pages=", ".join(f"page {n}" for n in scanned)))
    if document.attachment_pages:
        pages = ", ".join(str(n) for n in document.attachment_pages)
        notes.append(ATTACHMENT_NOTE.format(pages=pages))
    prompt = USER_PROMPT.format(
        scan_note="\n".join(notes),
        source_file=document.source_file,
        document_text=document_text(document),
    )
    contents: list[Any] = [prompt]
    for image in document.scanned_images:
        contents.append(types.Part.from_bytes(data=image, mime_type="image/png"))
    return contents


def cache_key(model: str, document: Document) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([model, PROMPT_VERSION, SYSTEM_INSTRUCTION]).encode())
    digest.update(document_text(document).encode())
    for image in document.scanned_images:
        digest.update(image)
    return digest.hexdigest()


# ------------------------------------------------------------------ response -> our schema


def read_from_scan(evidence: str | None, document: Document | None) -> bool:
    """True if the value must have come from a scanned page image: the document has
    scanned pages and the quote is not on any text page."""
    if document is None or not document.needs_vision:
        return False
    if not evidence:
        return True
    text = squash(document.text)
    return any(squash(line) not in text for line in evidence.splitlines() if line.strip())


def to_extracted(
    response: LLMChangeOrder, document: Document | None = None
) -> ExtractedChangeOrder:
    """Normalize Gemini's raw strings with our own parsers.

    The 'read from a scanned image' note (one more interpretation, -0.1) is added
    only to values whose quote is not on a text page of the document.
    """

    def scan_notes(evidence: str | None) -> list[str]:
        return [SCAN_NOTE_TEXT] if read_from_scan(evidence, document) else []

    def text(field: LLMField) -> ExtractedField[str]:
        return make_text_field(
            field.value,
            field.evidence,
            FieldSource.LLM,
            confidence(field.certainty),
            scan_notes(field.evidence),
        )

    def parsed(field: LLMField, parser: Callable[[str], Any]) -> ExtractedField:
        result = parser(field.value) if field.value else None
        if result is None:
            return ExtractedField()
        notes = [*result.notes, *scan_notes(field.evidence)]
        return make_field(
            result.value, field.evidence, FieldSource.LLM, confidence(field.certainty), notes
        )

    def money(field: LLMField) -> ExtractedField:
        """Like parsed(), but a credit stated only in the quote keeps its sign: value
        '$14,000.00' with evidence 'Decrease of this Change Order: $14,000.00' -> -14,000."""
        extracted = parsed(field, parse_money)
        if extracted.value is not None and extracted.value > 0 and credit_quote(field):
            return extracted.model_copy(
                update={"value": -extracted.value, "notes": [*extracted.notes, SIGN_FROM_QUOTE]}
            )
        return extracted

    status = response.status
    status_field: ExtractedField[ChangeOrderStatus] = ExtractedField[ChangeOrderStatus]()
    if status.value != ChangeOrderStatus.UNKNOWN:
        status_field = make_field(
            status.value,
            status.evidence,
            FieldSource.LLM,
            confidence(status.certainty),
            scan_notes(status.evidence),
        )

    return ExtractedChangeOrder(
        change_order_number=text(response.change_order_number),
        project_name=text(response.project_name),
        project_number=text(response.project_number),
        date=parsed(response.date, parse_date),
        contractor=text(response.contractor),
        owner=text(response.owner),
        description=text(response.description),
        reason=text(response.reason),
        line_items=line_items_field(response.line_items, document),
        subtotal=money(response.subtotal),
        markup_amount=money(response.markup_amount),
        tax_amount=money(response.tax_amount),
        total=money(response.total),
        schedule_impact_days=parsed(response.schedule_impact, parse_days),
        status=status_field,
        requested_by=text(response.requested_by),
        approved_by=text(response.approved_by),
        approval_date=parsed(response.approval_date, parse_date),
    )


SIGN_FROM_QUOTE = "made negative: the quote states a credit (e.g. 'Decrease')"
INCREASE_WORDS = re.compile(r"\b(?:increase[ds]?|add(?:ed|ition)?)\b", re.I)


def credit_quote(field: LLMField) -> bool:
    """The quote says credit/decrease (and not also increase/add, as forms that print
    '[Increase] [Decrease]' do), while the value itself carries no sign."""
    evidence, value = field.evidence or "", field.value or ""
    return (
        CREDIT_WORDS.search(evidence) is not None
        and INCREASE_WORDS.search(evidence) is None
        and CREDIT_WORDS.search(value) is None
    )


def line_items_field(
    raw_items: list[LLMLineItem], document: Document | None = None
) -> ExtractedField[list[LineItem]]:
    items: list[LineItem] = []
    evidence: list[str] = []
    notes: list[str] = []
    lowest = Certainty.HIGH

    for raw in raw_items:
        amount = parse_money(raw.amount)
        if amount is None:
            notes.append(f"skipped line item with unreadable amount: {raw.amount!r}")
            continue
        quantity = parse_money(raw.quantity)
        unit_price = parse_money(raw.unit_price)
        items.append(
            LineItem(
                description=" ".join(raw.description.split()),
                category=raw.category,
                quantity=quantity.value if quantity else None,
                unit=raw.unit or None,
                unit_price=unit_price.value if unit_price else None,
                amount=amount.value,
            )
        )
        evidence.append(raw.evidence)
        if confidence(raw.certainty) < confidence(lowest):
            lowest = raw.certainty

    if items and read_from_scan("\n".join(evidence), document):
        notes.append(SCAN_NOTE_TEXT)
    return make_field(
        items or None, "\n".join(evidence) or None, FieldSource.LLM, confidence(lowest), notes
    )


def confidence(certainty: Certainty) -> float:
    return CERTAINTY_CONFIDENCE[certainty]
