"""Tests for the evaluation harness (offline: rules mode and a fake Gemini)."""

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from change_order_extractor.config import Settings
from change_order_extractor.evaluation import (
    Case,
    DocResult,
    ModeReport,
    Outcome,
    TruthError,
    compare_document,
    compare_field,
    evaluate_mode,
    load_cases,
    markdown_report,
    match_items,
    word_f1,
)
from change_order_extractor.evaluation.compare import FieldResult
from change_order_extractor.evaluation.dataset import read_truth
from change_order_extractor.evaluation.metrics import calibration, check_stats, review_stats
from change_order_extractor.extraction import Extractor
from change_order_extractor.interfaces.cli import app
from change_order_extractor.pipeline import Pipeline
from change_order_extractor.schemas import (
    ChangeOrder,
    ChangeOrderStatus,
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    ExtractionResult,
    FieldSource,
    LineItem,
    ValidationIssue,
    ValidationReport,
)

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
SETTINGS = Settings(GEMINI_API_KEY=None, _env_file=None)
TODAY = dt.date(2026, 10, 6)


def field(value, confidence=0.9, source=FieldSource.BOTH) -> ExtractedField:
    return ExtractedField(value=value, confidence=confidence, source=source)


# ------------------------------------------------------------------ comparing values


def test_word_overlap():
    assert word_f1("Brennan Construction of MN, Inc.", "Brennan Construction of MN Inc") == 1.0
    assert word_f1("City of Marshall", "Board of Sumter County") < 0.5
    assert word_f1("abc", "") == 0.0


@pytest.mark.parametrize(
    ("name", "expected", "predicted", "outcome"),
    [
        ("total", 10670.0, 10670.004, Outcome.CORRECT),
        ("total", 10670.0, -10670.0, Outcome.WRONG),
        ("change_order_number", "9", "CO-009", Outcome.CORRECT),
        ("contractor", "Mark Cook Builders", "MARK COOK BUILDERS", Outcome.CORRECT),
        ("owner", "City of Marshall", "Engan Associates", Outcome.WRONG),
        ("date", dt.date(2020, 11, 5), dt.date(2019, 11, 18), Outcome.WRONG),
        ("total", 500.0, None, Outcome.MISSED),
        ("tax_amount", None, 12.0, Outcome.HALLUCINATED),
        ("tax_amount", None, None, Outcome.CORRECT_EMPTY),
        ("status", ChangeOrderStatus.UNKNOWN, None, Outcome.CORRECT_EMPTY),
        ("status", ChangeOrderStatus.APPROVED, ChangeOrderStatus.PENDING, Outcome.WRONG),
        ("line_items", [], None, Outcome.CORRECT_EMPTY),
    ],
)
def test_compare_field(name, expected, predicted, outcome):
    assert compare_field(name, expected, field(predicted)).outcome == outcome


def test_line_items_are_paired_by_amount():
    truth = [
        LineItem(description="Steel slip connections", amount=9700),
        LineItem(description="MCB Supervision", amount=0),
    ]
    found = [
        LineItem(description="MCB Supervision 0 hrs 62.00", amount=0),
        LineItem(description="Junk line", amount=55),
    ]
    match = match_items(truth, found)
    assert [t.amount for t, _ in match.pairs] == [0]
    assert [i.amount for i in match.missed] == [9700]
    assert [i.amount for i in match.extra] == [55]
    assert not match.all_found
    assert compare_field("line_items", truth, field(found)).outcome == Outcome.WRONG
    assert compare_field("line_items", truth, field(list(reversed(truth)))).is_correct


def test_unscored_fields_are_left_out():
    truth = ChangeOrder(total=10.0, description="x")
    results = compare_document(truth, ExtractedChangeOrder(), frozenset({"description"}))
    assert "description" not in {r.name for r in results}
    assert len(results) == len(ChangeOrder.model_fields) - 1


# ------------------------------------------------------------------ metrics


def doc(results: list[FieldResult], needs_review: bool) -> DocResult:
    case = Case("x.txt", Path("x.txt"), "test", ChangeOrder())
    result = ExtractionResult.model_validate(
        {
            "document": {"source_file": "x.txt", "source_type": "text"},
            "fields": {},
            "needs_review": needs_review,
        }
    )
    return DocResult(case, result, results, match_items([], []))


def outcome(name: str, outcome: Outcome, confidence: float) -> FieldResult:
    return FieldResult(name, outcome, None, None, confidence, FieldSource.BOTH)


def test_calibration_bins_and_ece():
    docs = [
        doc(
            [
                outcome("total", Outcome.CORRECT, 0.95),
                outcome("date", Outcome.WRONG, 0.95),  # overconfident
                outcome("owner", Outcome.CORRECT, 0.6),
                outcome("reason", Outcome.MISSED, 0.0),  # no value: not in calibration
            ],
            needs_review=True,
        )
    ]
    bins, ece = calibration(docs)
    top = bins[-1]
    assert (top.count, top.accuracy, round(top.gap, 2)) == (2, 0.5, 0.45)
    assert sum(b.count for b in bins) == 3
    assert ece == pytest.approx((2 * 0.45 + 1 * 0.4) / 3)


def test_review_stats():
    right = [outcome("total", Outcome.CORRECT, 0.9)]
    wrong_total = [outcome("total", Outcome.WRONG, 0.9)]
    wrong_owner = [outcome("owner", Outcome.WRONG, 0.9)]
    docs = [
        doc(right, needs_review=False),  # accepted, correct
        doc(right, needs_review=True),  # false alarm
        doc(wrong_total, needs_review=True),  # caught
        doc(wrong_owner, needs_review=False),  # slipped through
    ]
    stats = review_stats(docs)
    assert (stats.caught, stats.with_errors, stats.false_alarms) == (1, 2, 1)
    assert stats.auto_accept_accuracy == 0.5
    critical = review_stats(docs, critical_only=True)
    assert (critical.caught, critical.with_errors) == (1, 1)


def test_validation_alarms_are_judged():
    def with_issues(results, issues, expected=()):
        item = doc(results, needs_review=True)
        item.result.validation = ValidationReport.from_issues(
            [ValidationIssue(code=code, message=code, fields=fields) for code, fields in issues]
        )
        item.case = Case("x.txt", Path("x.txt"), "test", ChangeOrder(), frozenset(expected))
        return item

    docs = [
        # the document really has wrong math
        with_issues([], [("total_mismatch", ["total"])], expected=["total_mismatch"]),
        # not a document problem, but we missed the total: still a useful alarm
        with_issues([outcome("total", Outcome.MISSED, 0.0)], [("missing_total", ["total"])]),
        # values are right, the alarm is false
        with_issues(
            [outcome("subtotal", Outcome.CORRECT, 0.9)], [("subtotal_mismatch", ["subtotal"])]
        ),
    ]
    stats = check_stats(docs)
    assert (stats["total_mismatch"].real, stats["missing_total"].caught_error) == (1, 1)
    assert stats["subtotal_mismatch"].false_alarms == 1
    assert stats["subtotal_mismatch"].useful_rate == 0.0


# ------------------------------------------------------------------ dataset


def test_load_cases_plain_and_wrapped(tmp_path):
    docs = tmp_path / "docs"
    (docs / "ground_truth").mkdir(parents=True)
    (docs / "a.txt").write_text("CO 1", encoding="utf-8")
    (docs / "b.txt").write_text("CO 2", encoding="utf-8")
    (docs / "no_truth.txt").write_text("CO 3", encoding="utf-8")
    (docs / "ground_truth" / "a.json").write_text(ChangeOrder(total=5.0).model_dump_json())
    (docs / "ground_truth" / "b.json").write_text(
        json.dumps({"truth": {"total": 7.0}, "expected_issues": ["x"], "unscored": ["owner"]})
    )
    (docs / "manifest.json").write_text(json.dumps([{"file": "a.txt", "expected_issues": ["y"]}]))

    cases, skipped = load_cases([docs])

    assert [c.name for c in cases] == ["a.txt", "b.txt"]
    assert cases[0].expected_issues == {"y"} and cases[0].truth.total == 5.0
    assert cases[1].expected_issues == {"x"} and cases[1].unscored == {"owner"}
    assert [p.name for p in skipped] == ["no_truth.txt"]


def test_bad_truth_file_is_reported(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"truth": {"total": 1}, "unscored": ["not_a_field"]}))
    with pytest.raises(TruthError):
        read_truth(path)


def test_real_ground_truth_files_load():
    cases, _ = load_cases([REAL])
    assert {c.name for c in cases} == {
        "hidalgo_tx_co1.pdf",
        "lawrence_co8.pdf",
        "marshall_mn_co10.pdf",
        "sumter_county_fl_notice9.pdf",
    }


# ------------------------------------------------------------------ running


@pytest.fixture(scope="module")
def rules_report() -> ModeReport:
    cases, _ = load_cases([REAL])
    return evaluate_mode("rules", cases, SETTINGS, today=TODAY)


def test_rules_mode_on_real_documents(rules_report):
    marshall = next(d for d in rules_report.docs if d.case.name.startswith("marshall"))
    assert not marshall.errors  # every scored Marshall field is right today
    assert rules_report.overall.total > 30
    assert rules_report.gemini_answered == 0


def test_report_mentions_every_section(rules_report):
    text = markdown_report([rules_report])
    for heading in (
        "## Summary",
        "Accuracy per field",
        "calibration",
        "Review flag",
        "Every wrong field",
    ):
        assert heading in text
    assert "sumter_county_fl_notice9.pdf" in text  # its known gaps are listed


class TruthOnlyLLM(Extractor):
    """A fake Gemini that knows the answer for any document."""

    name = "llm"
    last_model = "fake-gemini"

    def __init__(self, truth: ChangeOrder) -> None:
        self.truth = truth

    def extract(self, document: Document) -> ExtractedChangeOrder:
        fields = ExtractedChangeOrder.from_change_order(self.truth)
        for name in ExtractedChangeOrder.model_fields:
            found = getattr(fields, name)
            if found.value is not None:
                found.source, found.confidence = FieldSource.LLM, 0.85
        return fields


def test_gemini_only_pipeline_skips_the_rules():
    case = next(c for c in load_cases([REAL])[0] if c.name.startswith("marshall"))
    pipeline = Pipeline(
        SETTINGS, use_rules=False, llm_extractor=TruthOnlyLLM(case.truth), today=TODAY
    )
    result = pipeline.run(case.path)
    assert result.extractors_used == ["llm"]
    assert result.fields.total.source == FieldSource.LLM
    assert all(r.is_correct for r in compare_document(case.truth, result.fields, case.unscored))


def test_cli_evaluate_writes_report(tmp_path):
    docs = tmp_path / "real"
    shutil.copytree(REAL, docs)
    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            str(docs),
            "--out",
            str(tmp_path / "reports"),
            "--results",
            str(tmp_path / "results"),
        ],
        env={"COLUMNS": "160"},
    )
    assert result.exit_code == 0, result.output
    assert "Field accuracy" in result.output
    assert (tmp_path / "reports" / "eval_report.md").is_file()
    summary = json.loads((tmp_path / "reports" / "eval_summary.json").read_text())
    assert summary["modes"]["rules"]["documents"] == 4
    assert len(list((tmp_path / "results" / "rules").glob("*.json"))) == 4


def test_cli_rejects_unknown_mode(tmp_path):
    result = CliRunner().invoke(app, ["evaluate", str(REAL), "--mode", "magic"])
    assert result.exit_code == 2
