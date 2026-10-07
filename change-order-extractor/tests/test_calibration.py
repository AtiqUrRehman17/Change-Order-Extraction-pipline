"""Tests for the confidence calibration (fitting, applying, and the pipeline using it)."""

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from change_order_extractor.config import Settings
from change_order_extractor.interfaces.cli import app
from change_order_extractor.pipeline import Pipeline
from change_order_extractor.schemas import (
    ExtractedChangeOrder,
    FieldSource,
    ValidationReport,
)
from change_order_extractor.scoring import score
from change_order_extractor.scoring.calibration import (
    DEFAULT_CALIBRATION_FILE,
    Calibration,
    Point,
    Sample,
    cross_validated,
    fit,
    group_for,
    isotonic_blocks,
)

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
TODAY = dt.date(2026, 10, 6)


def samples(group: str, raw: float, right: int, wrong: int, document: str = "d") -> list[Sample]:
    return [Sample(group, raw, True, document)] * right + [
        Sample(group, raw, False, document)
    ] * wrong


# ------------------------------------------------------------------ groups


@pytest.mark.parametrize(
    ("source", "cross_checked", "group"),
    [
        (FieldSource.BOTH, True, "both"),
        (FieldSource.CONFLICT, True, "conflict"),
        (FieldSource.RULES, True, "rules_unconfirmed"),
        (FieldSource.LLM, True, "llm_unconfirmed"),
        (FieldSource.RULES, False, "rules_alone"),
        (FieldSource.LLM, False, "llm_alone"),
        (FieldSource.NONE, True, None),
    ],
)
def test_group_for(source, cross_checked, group):
    assert group_for(source, cross_checked) == group


# ------------------------------------------------------------------ fitting


def test_isotonic_blocks_never_decrease():
    # 0.5 is right 1/2, 0.6 is right 0/2 (a violation), 0.9 is right 2/2
    data = sorted([(0.5, True), (0.5, False), (0.6, False), (0.6, False), (0.9, True), (0.9, True)])
    blocks = isotonic_blocks(data)
    accuracies = [correct / count for _, count, correct in blocks]
    assert accuracies == sorted(accuracies)
    assert [count for _, count, _ in blocks] == [4, 2]  # 0.5 and 0.6 were pooled


def test_fit_measures_accuracy_and_shrinks_small_groups():
    table = fit(samples("rules_alone", 0.77, 162, 0) + samples("llm_unconfirmed", 0.6, 1, 2))
    rules = table.groups["rules_alone"][0]
    assert (rules.count, rules.correct) == (162, 162)
    assert rules.calibrated == pytest.approx((162 + 10 * 0.77) / 172, abs=1e-4)  # ~0.99
    small = table.groups["llm_unconfirmed"][0]
    # 1 of 3 right, but only 3 values: pulled toward the raw 0.6 -> (1 + 6) / 13
    assert small.calibrated == pytest.approx(7 / 13, abs=1e-4)


def test_apply_interpolates_and_scales_down_below_the_data():
    table = Calibration(
        groups={
            "both": [
                Point(raw=0.5, calibrated=0.6, count=10, correct=6),
                Point(raw=0.9, calibrated=0.98, count=10, correct=10),
            ]
        }
    )
    assert table.apply("both", 0.7) == pytest.approx(0.79)
    assert table.apply("both", 0.95) == 0.98  # above the data: the last point
    assert table.apply("both", 0.25) == 0.3  # below: scaled toward 0
    assert table.apply("both", 1.0) <= 0.99
    assert table.apply("conflict", 0.5) is None  # no data for this kind: keep raw


def test_cross_validation_leaves_each_document_out():
    data = samples("both", 0.9, 9, 1, "a") + samples("both", 0.9, 10, 0, "b")
    validated = cross_validated(data)
    assert len(validated) == len(data)
    # document 'a' is calibrated only from 'b' (10 of 10 right), so it gets a high score
    assert all(score > 0.9 for sample, score in validated if sample.document == "a")


def test_save_and_load(tmp_path):
    table = fit(samples("both", 0.9, 5, 0))
    path = tmp_path / "calibration.json"
    table.save(path)
    assert Calibration.load(path) == table
    assert Calibration.load(tmp_path / "missing.json") is None
    (tmp_path / "broken.json").write_text("{not json")
    assert Calibration.load(tmp_path / "broken.json") is None


# ------------------------------------------------------------------ scoring


def test_score_adds_an_explained_calibration_step():
    table = Calibration(
        groups={"rules_alone": [Point(raw=0.77, calibrated=0.99, count=162, correct=162)]}
    )
    fields = ExtractedChangeOrder.model_validate(
        {"total": {"value": 5.0, "confidence": 0.85, "source": FieldSource.RULES}}
    )
    scored = score(fields, ValidationReport(), cross_checked=False, calibration=table)
    assert scored.fields.total.confidence == 0.99
    assert any(
        "calibrated 0.77 -> 0.99 (rules_alone values: 162 of 162 right" in note
        for note in scored.fields.total.notes
    )


def test_more_likely_wrong_than_right_is_a_review_reason():
    table = Calibration(
        groups={
            "llm_unconfirmed": [Point(raw=0.6, calibrated=0.45, count=8, correct=2)],
            "both": [Point(raw=0.94, calibrated=0.99, count=199, correct=198)],
        }
    )
    both = {"confidence": 0.85, "source": FieldSource.BOTH}
    fields = ExtractedChangeOrder.model_validate(
        {
            "total": {"value": 5.0, **both},
            "change_order_number": {"value": "1", **both},
            "date": {"value": "2025-01-01", **both},
            "approved_by": {"value": "P. Shah", "confidence": 0.65, "source": FieldSource.LLM},
        }
    )
    scored = score(fields, ValidationReport(), calibration=table)
    # raw 0.65 x 0.9 (one source) = 0.59, just below the 0.6 point -> 0.45 x 0.59 / 0.6
    assert scored.review_reasons == ["very low confidence in approved_by (0.44)"]


# ------------------------------------------------------------------ settings and pipeline


@pytest.mark.parametrize("value", ["none", "off"])
def test_calibration_can_be_switched_off(value):
    assert Settings(COX_CALIBRATION_FILE=value, _env_file=None).calibration_path is None


def test_default_is_the_packaged_table():
    assert Settings(_env_file=None).calibration_path == DEFAULT_CALIBRATION_FILE
    assert Calibration.load(DEFAULT_CALIBRATION_FILE) is not None  # it is shipped


def test_pipeline_uses_the_calibration(tmp_path):
    path = tmp_path / "calibration.json"
    Calibration(
        groups={"rules_alone": [Point(raw=0.77, calibrated=0.97, count=5, correct=5)]}
    ).save(path)
    settings = Settings(GEMINI_API_KEY=None, COX_CALIBRATION_FILE=str(path), _env_file=None)
    raw = Settings(GEMINI_API_KEY=None, COX_CALIBRATION_FILE="none", _env_file=None)

    calibrated = Pipeline(settings, use_llm=False, today=TODAY).run(REAL / "lawrence_co8.pdf")
    uncalibrated = Pipeline(raw, use_llm=False, today=TODAY).run(REAL / "lawrence_co8.pdf")
    assert uncalibrated.fields.change_order_number.confidence == 0.77
    assert calibrated.fields.change_order_number.confidence == 0.97


def test_cli_calibrate_writes_table_and_report(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "calibrate",
            str(REAL),
            "--mode",
            "rules",
            "--out",
            str(tmp_path / "calibration.json"),
            "--report",
            str(tmp_path / "calibration_report.md"),
        ],
        env={"COLUMNS": "160"},
    )
    assert result.exit_code == 0, result.output
    table = json.loads((tmp_path / "calibration.json").read_text())
    assert "rules_alone" in table["groups"]
    report = (tmp_path / "calibration_report.md").read_text()
    assert "leave-one-document-out" in report
