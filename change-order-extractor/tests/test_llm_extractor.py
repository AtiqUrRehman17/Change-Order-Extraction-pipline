"""Tests for the Gemini extractor.

All tests use a fake client, so they run offline and spend no API quota.
A live smoke test runs only when COX_LIVE_TESTS=1.
"""

import datetime as dt
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors

from change_order_extractor.config import Settings
from change_order_extractor.extraction.llm_extractor import (
    SCAN_NOTE_TEXT,
    CachedAnswer,
    Certainty,
    GeminiExtractor,
    LLMChangeOrder,
    LLMExtractionError,
    LLMField,
    LLMLineItem,
    LLMStatus,
    build_contents,
    cache_key,
    retry_delay_seconds,
    to_extracted,
)
from change_order_extractor.ingestion import load_document
from change_order_extractor.preprocessing import clean_document
from change_order_extractor.schemas import (
    ChangeOrderStatus,
    Document,
    FieldSource,
    LineItemCategory,
    Page,
    SourceType,
)

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
HIGH = Certainty.HIGH

# ------------------------------------------------------------------ helpers


def field(value=None, evidence=None, certainty=HIGH) -> LLMField:
    if value is not None and evidence is None:
        evidence = value
    return LLMField(value=value, evidence=evidence, certainty=certainty)


def answer(**overrides) -> LLMChangeOrder:
    data = {
        name: field()
        for name in LLMChangeOrder.model_fields
        if name not in ("line_items", "status")
    }
    data["line_items"] = []
    data["status"] = LLMStatus(value=ChangeOrderStatus.UNKNOWN, evidence=None, certainty=HIGH)
    data.update(overrides)
    return LLMChangeOrder(**data)


def text_document(text: str = "CHANGE ORDER CO-1\nTOTAL $100.00") -> Document:
    return Document(
        source_file="co.txt", source_type=SourceType.TEXT, pages=[Page(number=1, text=text)]
    )


def api_error(code: int, retry_delay: str | None = None) -> errors.APIError:
    details = []
    if retry_delay:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    body = {"error": {"code": code, "message": "boom", "status": "X", "details": details}}
    return (errors.ServerError if code >= 500 else errors.ClientError)(code, body)


class FakeModels:
    """Plays back a script of results: an LLMChangeOrder, a raw JSON string, or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, str):
            return SimpleNamespace(parsed=None, text=result)
        return SimpleNamespace(parsed=result, text=result.model_dump_json())


def make_extractor(script, tmp_path=None, **settings) -> tuple[GeminiExtractor, FakeModels]:
    models = FakeModels(script)
    config = Settings(
        GEMINI_API_KEY="test",
        COX_GEMINI_MODEL="main-model",
        COX_GEMINI_FALLBACK_MODELS="backup-model",
        COX_LLM_MAX_RETRIES=2,
        COX_LLM_CACHE_DIR=str(tmp_path) if tmp_path else "",
        **settings,
    )
    sleeps: list[float] = []
    extractor = GeminiExtractor(
        settings=config, client=SimpleNamespace(models=models), sleep=sleeps.append
    )
    extractor.sleeps = sleeps  # type: ignore[attr-defined]
    return extractor, models


# ------------------------------------------------------------------ response conversion


def test_values_are_normalized_with_our_parsers():
    result = to_extracted(
        answer(
            change_order_number=field("CO-014", "CHANGE ORDER # CO-014"),
            date=field("Mar 5th, 2025", "Date: Mar 5th, 2025"),
            total=field("($6,250.00)", "NET CREDIT ($6,250.00)"),
            schedule_impact=field("2 weeks", "Time Extension: 2 weeks", Certainty.MEDIUM),
            status=LLMStatus(value=ChangeOrderStatus.APPROVED, evidence="APPROVED", certainty=HIGH),
        )
    )

    assert result.change_order_number.value == "CO-014"
    assert result.change_order_number.evidence == "CHANGE ORDER # CO-014"
    assert result.change_order_number.source == FieldSource.LLM
    assert result.date.value == dt.date(2025, 3, 5)
    assert result.total.value == -6250.00
    assert result.schedule_impact_days.value == 14
    assert "converted weeks to days (x7)" in result.schedule_impact_days.notes
    assert result.status.value == ChangeOrderStatus.APPROVED


def test_certainty_and_notes_set_preliminary_confidence():
    result = to_extracted(
        answer(
            project_name=field("Riverside", certainty=Certainty.HIGH),
            owner=field("Greenfield", certainty=Certainty.LOW),
            schedule_impact=field("2 weeks", certainty=Certainty.HIGH),
        )
    )
    assert result.project_name.confidence == 0.85
    assert result.owner.confidence == 0.45
    assert result.schedule_impact_days.confidence == 0.75  # one interpretation note


def test_missing_and_unknown_values_stay_empty():
    result = to_extracted(answer(total=field("N/A")))

    assert result.total.value is None
    assert result.status.value is None
    assert result.line_items.value is None
    assert result.to_change_order().status == ChangeOrderStatus.UNKNOWN


def test_line_items_are_parsed_and_bad_ones_skipped():
    items = [
        LLMLineItem(
            description="Acoustic panels",
            category=LineItemCategory.MATERIAL,
            quantity="48",
            unit="EA",
            unit_price="$145.00",
            amount="$6,960.00",
            evidence="1. Acoustic panels 48 EA @ $145.00 $6,960.00",
            certainty=HIGH,
        ),
        LLMLineItem(
            description="Mystery",
            category=LineItemCategory.OTHER,
            quantity=None,
            unit=None,
            unit_price=None,
            amount="TBD",
            evidence="Mystery TBD",
            certainty=HIGH,
        ),
    ]
    result = to_extracted(answer(line_items=items))
    [item] = result.line_items.value

    assert (item.quantity, item.unit, item.unit_price, item.amount) == (48, "EA", 145.00, 6960.00)
    assert item.category == LineItemCategory.MATERIAL
    assert any("unreadable amount" in note for note in result.line_items.notes)


def test_scanned_documents_are_noted():
    document = clean_document(load_document(SAMPLES / "co_008_scanned.pdf"))
    result = to_extracted(answer(total=field("$100.00")), document)
    assert SCAN_NOTE_TEXT in result.total.notes


def test_scan_note_only_for_values_not_on_a_text_page():
    """Mixed PDF: a quote found on a text page was not read from the scanned image."""
    document = Document(
        source_file="mixed.pdf",
        source_type=SourceType.PDF_MIXED,
        pages=[
            Page(number=1, text="OWNER: City of Marshall\nTOTAL $100.00"),
            Page(number=2, is_scanned=True),
        ],
    )
    result = to_extracted(
        answer(
            owner=field("City of Marshall", "OWNER: City of Marshall"),
            approved_by=field("Scott VanDerMillen", "Scott VanDerMillen, Owner"),
        ),
        document,
    )
    assert SCAN_NOTE_TEXT not in result.owner.notes
    assert result.owner.confidence == 0.85
    assert SCAN_NOTE_TEXT in result.approved_by.notes


def test_text_documents_get_no_scan_note():
    result = to_extracted(answer(total=field("$100.00")), text_document("TOTAL $100.00"))
    assert result.total.notes == []


def test_ocr_letters_fixed_in_names():
    result = to_extracted(answer(project_name=field("Sunset Ridge C1ubhouse")))
    assert result.project_name.value == "Sunset Ridge Clubhouse"


# ------------------------------------------------------------------ request building


def test_scanned_pages_are_sent_as_images():
    document = clean_document(load_document(SAMPLES / "co_008_scanned.pdf"))
    contents = build_contents(document)

    assert "scanned images" in contents[0]
    assert len(contents) == 2
    assert contents[1].inline_data.mime_type == "image/png"


def test_text_documents_are_sent_as_text_only():
    contents = build_contents(text_document("CHANGE ORDER CO-9"))
    assert contents == [contents[0]]
    assert "[Page 1]\nCHANGE ORDER CO-9" in contents[0]


def test_cache_key_changes_with_document_and_model():
    a, b = text_document("one"), text_document("two")
    assert cache_key("m", a) == cache_key("m", text_document("one"))
    assert cache_key("m", a) != cache_key("m", b)
    assert cache_key("m", a) != cache_key("other", a)


# ------------------------------------------------------------------ calling Gemini


def test_successful_call():
    extractor, models = make_extractor([answer(total=field("$100.00"))])
    result = extractor.extract(text_document())

    assert result.total.value == 100.00
    assert models.calls == ["main-model"]
    assert extractor.last_model == "main-model"


def test_retries_on_overload_then_succeeds():
    extractor, models = make_extractor([api_error(503), answer(total=field("$1.00"))])
    result = extractor.extract(text_document())

    assert result.total.value == 1.00
    assert models.calls == ["main-model", "main-model"]
    assert len(extractor.sleeps) == 1


def test_retries_on_malformed_json():
    extractor, models = make_extractor(["{not json", answer()])
    extractor.extract(text_document())
    assert len(models.calls) == 2


def test_uses_server_suggested_short_wait():
    extractor, _ = make_extractor([api_error(429, "7s"), answer()])
    extractor.extract(text_document())
    assert extractor.sleeps == [7.0]


def test_quota_exhausted_skips_straight_to_fallback():
    extractor, models = make_extractor([api_error(429, "42646s"), answer(total=field("$5.00"))])
    result = extractor.extract(text_document())

    assert models.calls == ["main-model", "backup-model"]  # no retries on the dead model
    assert extractor.sleeps == []
    assert extractor.last_model == "backup-model"
    assert result.total.value == 5.00


def test_falls_back_after_retries_run_out():
    script = [api_error(503)] * 3 + [answer()]
    extractor, models = make_extractor(script)
    extractor.extract(text_document())

    assert models.calls == ["main-model"] * 3 + ["backup-model"]


def test_all_models_failing_raises():
    extractor, _ = make_extractor([api_error(503)] * 6)
    with pytest.raises(LLMExtractionError, match="All Gemini models failed"):
        extractor.extract(text_document())


def test_non_retryable_error_fails_fast():
    extractor, models = make_extractor([api_error(400)])
    with pytest.raises(LLMExtractionError, match="400"):
        extractor.extract(text_document())
    assert models.calls == ["main-model"]


def test_missing_api_key():
    settings = Settings(GEMINI_API_KEY=None, _env_file=None)
    with pytest.raises(LLMExtractionError, match="GEMINI_API_KEY"):
        GeminiExtractor(settings=settings)


def test_retry_delay_parsing():
    assert retry_delay_seconds(api_error(429, "42646s")) == 42646.0
    assert retry_delay_seconds(api_error(503)) is None


# ------------------------------------------------------------------ cache


def test_cache_avoids_a_second_call(tmp_path):
    extractor, models = make_extractor([answer(total=field("$9.00"))], tmp_path)
    first = extractor.extract(text_document())
    second = extractor.extract(text_document())

    assert first == second
    assert models.calls == ["main-model"]


def test_cache_remembers_which_model_answered(tmp_path):
    extractor, _ = make_extractor([api_error(429, "99999s"), answer()], tmp_path)
    extractor.extract(text_document())

    fresh, models = make_extractor([], tmp_path)
    fresh.extract(text_document())
    assert models.calls == []
    assert fresh.last_model == "backup-model"


def test_reads_old_cache_format(tmp_path):
    extractor, models = make_extractor([], tmp_path)
    document = text_document()
    path = tmp_path / f"{cache_key('main-model', document)}.json"
    path.write_text(answer(total=field("$3.00")).model_dump_json(), encoding="utf-8")

    assert extractor.extract(document).total.value == 3.00
    assert models.calls == []


def test_corrupt_cache_is_ignored(tmp_path):
    extractor, models = make_extractor([answer()], tmp_path)
    document = text_document()
    (tmp_path / f"{cache_key('main-model', document)}.json").write_text("garbage")

    extractor.extract(document)
    assert models.calls == ["main-model"]
    assert CachedAnswer.model_validate_json(
        (tmp_path / f"{cache_key('main-model', document)}.json").read_text()
    )


def test_settings_parsing():
    settings = Settings(COX_LLM_CACHE_DIR="", COX_GEMINI_FALLBACK_MODELS=" a, b ,", _env_file=None)
    assert settings.llm_cache_dir is None
    assert settings.fallback_models == ["a", "b"]


# ------------------------------------------------------------------ live (opt-in)


@pytest.mark.skipif(os.environ.get("COX_LIVE_TESTS") != "1", reason="set COX_LIVE_TESTS=1")
def test_live_gemini_on_clean_sample():
    document = clean_document(load_document(SAMPLES / "co_001_clean.txt"))
    result = GeminiExtractor().extract(document)

    assert result.change_order_number.value == "CO-014"
    assert result.total.value == 3685.00
    assert result.total.evidence and result.total.evidence in document.text


# ------------------------------------------------------------------ attachments and second opinions


def test_attachment_pages_are_labelled_for_gemini():
    document = Document(
        source_file="co.pdf",
        source_type=SourceType.PDF_TEXT,
        pages=[
            Page(number=1, text="CHANGE ORDER 4"),
            Page(number=2, text="DATE: May 1\nTO: Owner", is_attachment=True),
        ],
    )
    prompt = build_contents(document)[0]

    assert "[Page 1]\nCHANGE ORDER 4" in prompt
    assert "[Page 2 - ATTACHMENT (backup document)]" in prompt
    assert "Pages 2 were detected as ATTACHMENTS" in prompt


def test_extract_with_models_asks_the_given_models():
    extractor, models = make_extractor([answer(total=field("$5.00"))])
    result = extractor.extract_with_models(text_document(), ["strong-model"])

    assert models.calls == ["strong-model"]
    assert extractor.last_model == "strong-model"
    assert result.total.value == 5.00


def test_second_opinion_makes_one_attempt_per_model():
    """The second opinion is optional: a busy stronger model is skipped at once instead
    of being retried (retries took minutes when Google was overloaded)."""
    extractor, models = make_extractor([api_error(503), api_error(503)])
    with pytest.raises(LLMExtractionError):
        extractor.extract_with_models(text_document(), ["strong-a", "strong-b"])
    assert models.calls == ["strong-a", "strong-b"]
    assert extractor.sleeps == []


@pytest.mark.parametrize("value", ["none", "None", "off", ""])
def test_second_opinion_can_be_switched_off(value):
    settings = Settings(COX_GEMINI_ESCALATION_MODELS=value, _env_file=None)
    assert settings.escalation_models == []


# ------------------------------------------------------------------ credits stated in the quote


def test_credit_word_in_the_quote_gives_the_sign():
    """A lite model wrote '$14,000.00' but quoted 'Decrease of this Change Order: ...'."""
    result = to_extracted(
        answer(total=field("$14,000.00", "Decrease of this Change Order: $14,000.00"))
    )
    assert result.total.value == -14000.00
    assert any("made negative" in note for note in result.total.notes)


@pytest.mark.parametrize(
    ("value", "evidence", "expected"),
    [
        ("$500.00", "Increase of this Change Order: $500.00", 500.0),
        ("$500.00", "[Increase] [Decrease] of this Change Order: $500.00", 500.0),  # unclear
        ("-$500.00", "Decrease of this Change Order: $500.00", -500.0),  # already negative
        ("$500.00", "TOTAL $500.00", 500.0),
    ],
)
def test_sign_from_the_quote_only_when_it_is_clear(value, evidence, expected):
    assert to_extracted(answer(total=field(value, evidence))).total.value == expected
