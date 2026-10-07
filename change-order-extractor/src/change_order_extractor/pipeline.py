"""Connects all pipeline steps: read -> clean -> find attachments -> extract -> merge ->
(second opinion) -> locate evidence pages -> validate -> score.

    result = run("data/samples/co_001_clean.txt")
    print(result.fields.total.value, result.overall_confidence, result.needs_review)

The pipeline never fails just because the LLM does: with no API key, or when
Gemini is down or out of quota, it falls back to the rule-based extractor and
says so in `result.warnings` (and the lower confidence shows it).
"""

import datetime as dt
import logging
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from change_order_extractor.config import Settings, get_settings
from change_order_extractor.extraction import (
    Extractor,
    GeminiExtractor,
    LLMExtractionError,
    RuleBasedExtractor,
)
from change_order_extractor.extraction.fields import locate_pages
from change_order_extractor.ingestion import SUPPORTED_EXTENSIONS, IngestionError, load_document
from change_order_extractor.preprocessing import clean_document, mark_attachments
from change_order_extractor.schemas import (
    Document,
    DocumentInfo,
    ExtractedChangeOrder,
    ExtractionResult,
    FieldSource,
)
from change_order_extractor.scoring import Calibration, merge, score
from change_order_extractor.scoring.confidence import CRITICAL
from change_order_extractor.validation import validate

logger = logging.getLogger(__name__)


class Pipeline:
    """Reusable pipeline: create once, run on many documents."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        use_llm: bool = True,
        use_rules: bool = True,
        llm_extractor: Extractor | None = None,
        today: dt.date | None = None,
        calibrate: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        # calibrate=False keeps the raw scores (used when fitting a new calibration).
        path = self.settings.calibration_path if calibrate else None
        self.calibration = Calibration.load(path) if path else None
        # use_rules=False gives a Gemini-only pipeline (used by the evaluation to compare modes).
        self.rules = RuleBasedExtractor() if use_rules else None
        self.today = today
        self.setup_warnings: list[str] = []
        self.llm = llm_extractor if use_llm else None

        if use_llm and self.llm is None:
            try:
                self.llm = GeminiExtractor(self.settings)
            except LLMExtractionError as error:  # e.g. no API key
                self.setup_warnings.append(f"Gemini disabled: {error} Using rules only.")
                logger.warning("Gemini disabled: %s", error)

    # -------------------------------------------------------------- single document

    def run(self, path: str | Path) -> ExtractionResult:
        """Process one file. Raises IngestionError if the file can't be read."""
        started = time.perf_counter()
        document = load_document(path)
        return self.run_document(document, started=started)

    def run_document(self, document: Document, *, started: float | None = None) -> ExtractionResult:
        started = started if started is not None else time.perf_counter()
        warnings = list(self.setup_warnings)

        cleaned = mark_attachments(clean_document(document))
        rules_result = self.rules.extract(cleaned) if self.rules else ExtractedChangeOrder()
        extractors_used = [self.rules.name] if self.rules else []

        llm_result = None
        model = None
        if self.llm is not None:
            try:
                llm_result = self.llm.extract(cleaned)
                extractors_used.append(self.llm.name)
                model = getattr(self.llm, "last_model", None)
            except LLMExtractionError as error:
                warnings.append(f"Gemini failed; used rules only. ({error})")
                logger.warning("Gemini failed for %s: %s", document.source_file, error)

        if cleaned.needs_vision and llm_result is None:
            warnings.append("Scanned pages could not be read without Gemini.")

        merged = merge(rules_result, llm_result, cleaned)
        if llm_result is not None and (
            second := self.second_opinion(cleaned, merged, model, warnings)
        ):
            llm_result, model, asked_about = second
            merged = merge(
                rules_result, llm_result, cleaned, second_opinion_fields=frozenset(asked_about)
            )
        merged = locate_pages(merged, cleaned)

        report = validate(merged, cleaned, today=self.today)
        scored = score(
            merged,
            report,
            review_threshold=self.settings.review_threshold,
            cross_checked=llm_result is not None and self.rules is not None,
            calibration=self.calibration,
        )

        reasons = list(scored.review_reasons)
        if llm_result is None and self.llm is not None:
            reasons.append("Gemini was unavailable, so values were not cross-checked")

        return ExtractionResult(
            document=DocumentInfo.from_document(cleaned),
            fields=scored.fields,
            validation=report,
            overall_confidence=scored.overall_confidence,
            needs_review=bool(reasons),
            review_reasons=reasons,
            extractors_used=extractors_used,
            model=model,
            warnings=warnings,
            processing_seconds=round(time.perf_counter() - started, 3),
        )

    def second_opinion(
        self,
        document: Document,
        merged: ExtractedChangeOrder,
        answered_model: str | None,
        warnings: list[str],
    ) -> tuple[ExtractedChangeOrder, str | None, list[str]] | None:
        """Ask a stronger model when a weaker one disagrees with the rules on a critical
        field. Returns (new LLM result, model that answered, the conflicting fields), or
        None to keep the first. If the stronger model still disagrees with the rules on
        those fields, its answer wins (see merge)."""
        conflicts = [n for n in CRITICAL if getattr(merged, n).source == FieldSource.CONFLICT]
        strong = self.settings.escalation_models
        stronger = [m for m in strong if m != answered_model]
        ask = getattr(self.llm, "extract_with_models", None)
        if not conflicts or answered_model in strong or not stronger or ask is None:
            return None

        fields = ", ".join(conflicts)
        try:
            result = ask(document, stronger)
        except LLMExtractionError as error:
            warnings.append(
                f"{answered_model} disagreed with the rules on {fields}; a stronger model "
                f"was unavailable, so its answer was kept."
            )
            logger.warning("Second opinion failed for %s: %s", document.source_file, error)
            return None
        new_model = getattr(self.llm, "last_model", None)
        warnings.append(
            f"{answered_model} disagreed with the rules on {fields}; "
            f"used a second opinion from {new_model}."
        )
        return result, new_model, conflicts

    # -------------------------------------------------------------- many documents

    def run_many(
        self, paths: Iterable[Path]
    ) -> Iterator[tuple[Path, ExtractionResult | IngestionError]]:
        """Process files one by one; a file that can't be read doesn't stop the rest."""
        for path in paths:
            try:
                yield path, self.run(path)
            except IngestionError as error:
                logger.info("Skipping %s: %s", path, error)  # the caller reports it
                yield path, error


# ------------------------------------------------------------------ helpers


def run(path: str | Path, *, use_llm: bool = True) -> ExtractionResult:
    """Process one file with default settings."""
    return Pipeline(use_llm=use_llm).run(path)


def find_input_files(path: str | Path) -> list[Path]:
    """A file -> [file]. A folder -> its supported files (not recursive), sorted."""
    path = Path(path)
    if path.is_dir():
        return sorted(
            p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    return [path]


def save_result(result: ExtractionResult, out_dir: str | Path) -> Path:
    """Write `<out_dir>/<input name>.json` and return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{Path(result.document.source_file).stem}.json"
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
