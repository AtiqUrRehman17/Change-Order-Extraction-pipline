"""End-to-end tests for the pipeline (Gemini replaced by fakes: offline, no quota)."""

import datetime as dt
import json
from pathlib import Path

import pytest

from change_order_extractor.config import Settings
from change_order_extractor.extraction import Extractor, LLMExtractionError, RuleBasedExtractor
from change_order_extractor.ingestion import IngestionError
from change_order_extractor.pipeline import Pipeline, find_input_files, save_result
from change_order_extractor.schemas import (
    ChangeOrder,
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    ExtractionResult,
    FieldSource,
    SourceType,
)

DATA = Path(__file__).resolve().parents[1] / "data"
SAMPLES = DATA / "samples"
TODAY = dt.date(2026, 10, 6)
SETTINGS = Settings(GEMINI_API_KEY=None, _env_file=None)


class AgreeingLLM(Extractor):
    """Pretends to be Gemini by returning what the rules found (perfect agreement)."""

    name = "llm"
    last_model = "fake-gemini"

    def extract(self, document: Document) -> ExtractedChangeOrder:
        found = RuleBasedExtractor().extract(document)
        for name in ExtractedChangeOrder.model_fields:
            field = getattr(found, name)
            if field.value is not None:
                field.source = FieldSource.LLM
        return found


class TruthLLM(Extractor):
    """Returns the ground truth for a sample, as if Gemini read it perfectly (no quotes)."""

    name = "llm"
    last_model = "fake-gemini"

    def __init__(self, sample_id: str) -> None:
        text = (DATA / "ground_truth" / f"{sample_id}.json").read_text(encoding="utf-8")
        self.truth = ChangeOrder.model_validate_json(text)

    def extract(self, document: Document) -> ExtractedChangeOrder:
        fields = ExtractedChangeOrder.from_change_order(self.truth)
        for name in ExtractedChangeOrder.model_fields:
            field = getattr(fields, name)
            if field.value is not None:
                field.source, field.confidence = FieldSource.LLM, 0.85
                field.evidence = str(field.value) if name != "line_items" else None
        return fields


class BrokenLLM(Extractor):
    name = "llm"

    def extract(self, document: Document) -> ExtractedChangeOrder:
        raise LLMExtractionError("All Gemini models failed: 503")


def pipeline(llm: Extractor | None = None, use_llm: bool = True) -> Pipeline:
    return Pipeline(SETTINGS, use_llm=use_llm, llm_extractor=llm, today=TODAY)


# ------------------------------------------------------------------ modes


def test_rules_only_mode():
    result = pipeline(use_llm=False).run(SAMPLES / "co_001_clean.txt")

    assert result.extractors_used == ["rules"]
    assert result.model is None
    assert result.warnings == []
    assert result.fields.total.value == 3685.00
    assert result.fields.total.source == FieldSource.RULES
    assert result.document.source_type == SourceType.TEXT
    assert result.processing_seconds is not None


def test_agreeing_extractors_give_high_confidence():
    rules_only = pipeline(use_llm=False).run(SAMPLES / "co_001_clean.txt")
    hybrid = pipeline(AgreeingLLM()).run(SAMPLES / "co_001_clean.txt")

    assert hybrid.extractors_used == ["rules", "llm"]
    assert hybrid.model == "fake-gemini"
    assert hybrid.fields.total.source == FieldSource.BOTH
    assert hybrid.overall_confidence > rules_only.overall_confidence
    assert hybrid.overall_confidence >= 0.9
    assert not hybrid.needs_review


def test_missing_api_key_falls_back_to_rules():
    result = Pipeline(SETTINGS, today=TODAY).run(SAMPLES / "co_001_clean.txt")

    assert result.extractors_used == ["rules"]
    assert any("GEMINI_API_KEY" in warning for warning in result.warnings)
    assert result.fields.total.value == 3685.00


def test_gemini_failure_falls_back_to_rules():
    result = pipeline(BrokenLLM()).run(SAMPLES / "co_001_clean.txt")

    assert result.extractors_used == ["rules"]
    assert any("Gemini failed" in warning for warning in result.warnings)
    assert result.needs_review
    assert "Gemini was unavailable, so values were not cross-checked" in result.review_reasons
    assert result.fields.total.value == 3685.00


# ------------------------------------------------------------------ documents


def test_scanned_document_is_read_by_the_llm():
    result = pipeline(TruthLLM("co_008_scanned")).run(SAMPLES / "co_008_scanned.pdf")

    assert result.fields.total.value == 6708.80
    assert result.fields.total.source == FieldSource.LLM
    assert result.document.source_type == SourceType.PDF_SCANNED
    assert "evidence_unverifiable" in [issue.code for issue in result.validation.issues]


def test_scanned_document_without_llm_says_why_it_is_empty():
    result = pipeline(use_llm=False).run(SAMPLES / "co_008_scanned.pdf")

    assert result.fields.total.value is None
    assert result.needs_review
    assert "Scanned pages could not be read without Gemini." in result.warnings


def test_broken_document_is_flagged_with_reasons():
    result = pipeline(AgreeingLLM()).run(SAMPLES / "co_005_wrong_math.txt")

    assert not result.validation.passed
    assert result.needs_review
    assert any(reason.startswith("total_mismatch") for reason in result.review_reasons)
    assert result.fields.total.confidence < 0.75


# ------------------------------------------------------------------ batches and output


def test_find_input_files(tmp_path):
    for name in ("b.txt", "a.pdf", "notes.docx", "c.md"):
        (tmp_path / name).write_text("x")
    (tmp_path / "sub").mkdir()

    assert [p.name for p in find_input_files(tmp_path)] == ["a.pdf", "b.txt", "c.md"]
    assert find_input_files(tmp_path / "b.txt") == [tmp_path / "b.txt"]


def test_run_many_keeps_going_after_a_bad_file(tmp_path):
    bad = tmp_path / "empty.txt"
    bad.write_text("")
    paths = [SAMPLES / "co_001_clean.txt", bad, SAMPLES / "co_004_credit.txt"]

    results = list(pipeline(use_llm=False).run_many(paths))

    assert [type(r).__name__ for _, r in results] == [
        "ExtractionResult",
        "IngestionError",
        "ExtractionResult",
    ]


def test_run_raises_for_unreadable_file(tmp_path):
    with pytest.raises(IngestionError):
        pipeline(use_llm=False).run(tmp_path / "missing.pdf")


def test_save_result_round_trips(tmp_path):
    result = pipeline(use_llm=False).run(SAMPLES / "co_002_table.pdf")
    path = save_result(result, tmp_path / "out")

    assert path.name == "co_002_table.json"
    loaded = ExtractionResult.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded == result
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["fields"]["total"]["value"] == 5372.80
    assert data["fields"]["date"]["value"] == "2024-08-14"


@pytest.mark.parametrize(
    "entry", json.loads((SAMPLES / "manifest.json").read_text()), ids=lambda e: e["id"]
)
def test_every_sample_runs_end_to_end(entry):
    """With a perfect LLM, every broken sample's problem is caught and flagged."""
    result = pipeline(TruthLLM(entry["id"])).run(SAMPLES / entry["file"])

    assert 0.0 <= result.overall_confidence <= 1.0
    for issue in entry["expected_issues"]:
        assert issue in [i.code for i in result.validation.errors]
    if entry["expected_issues"]:
        assert result.needs_review


# ------------------------------------------------------------------ second opinion


class LiteModelLLM(Extractor):
    """A weak model that misreads the total; a stronger model then reads it right."""

    name = "llm"

    def __init__(self, answered_by: str = "lite-model", stronger_fails: bool = False) -> None:
        self.answered_by = answered_by
        self.stronger_fails = stronger_fails
        self.asked: list[list[str]] = []
        self.last_model: str | None = None

    def extract(self, document: Document) -> ExtractedChangeOrder:
        self.last_model = self.answered_by
        found = AgreeingLLM().extract(document)
        found.total = ExtractedField[float](
            value=3865.0, confidence=0.85, evidence="TOTAL $3,865.00", source=FieldSource.LLM
        )
        return found

    def extract_with_models(self, document: Document, models: list[str]) -> ExtractedChangeOrder:
        self.asked.append(models)
        if self.stronger_fails:
            raise LLMExtractionError("All Gemini models failed: 503")
        self.last_model = models[0]
        return AgreeingLLM().extract(document)


def test_conflict_from_a_weak_model_gets_a_second_opinion():
    llm = LiteModelLLM()
    result = pipeline(llm).run(SAMPLES / "co_001_clean.txt")

    assert llm.asked == [SETTINGS.escalation_models]
    assert result.model == SETTINGS.escalation_models[0]
    assert result.fields.total.source == FieldSource.BOTH
    assert any("second opinion" in warning for warning in result.warnings)


def test_unavailable_second_opinion_keeps_the_first_answer():
    llm = LiteModelLLM(stronger_fails=True)
    result = pipeline(llm).run(SAMPLES / "co_001_clean.txt")

    assert result.model == "lite-model"
    assert result.fields.total.value == 3685.00  # the rules' quote is in the document
    assert result.fields.total.source == FieldSource.CONFLICT
    assert any("a stronger model was unavailable" in warning for warning in result.warnings)


def test_no_second_opinion_when_a_strong_model_already_answered():
    llm = LiteModelLLM(answered_by=SETTINGS.escalation_models[0])
    pipeline(llm).run(SAMPLES / "co_001_clean.txt")
    assert llm.asked == []


def test_evidence_pages_are_recorded():
    result = pipeline(use_llm=False).run(SAMPLES / "co_009_multi_page.pdf")

    assert result.fields.subtotal.page == 1
    assert result.fields.total.page == 2  # 'TOTAL THIS CHANGE ORDER' is on page 2
