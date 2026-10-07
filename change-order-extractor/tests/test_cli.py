"""Tests for the command-line interface (rules only: no API calls)."""

import json
from pathlib import Path

from typer.testing import CliRunner

from change_order_extractor.interfaces.cli import app
from change_order_extractor.interfaces.display import (
    evidence_text,
    format_value,
    page_range,
    shorten,
)
from change_order_extractor.schemas import ExtractionResult, LineItem

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, [str(a) for a in args], env={"COLUMNS": "160"})


def test_help_lists_extract():
    result = invoke("--help")
    assert result.exit_code == 0
    assert "extract" in result.output


def test_extract_one_file_shows_table_and_saves_json(tmp_path):
    result = invoke("extract", SAMPLES / "co_001_clean.txt", "--no-llm", "--out", tmp_path)

    assert result.exit_code == 0, result.output
    assert "co_001_clean.txt" in result.output
    assert "$3,685.00" in result.output
    assert "Line items" in result.output
    saved = ExtractionResult.model_validate_json((tmp_path / "co_001_clean.json").read_text())
    assert saved.fields.total.value == 3685.00


def test_extract_folder_shows_summary(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    for name in ("co_001_clean.txt", "co_005_wrong_math.txt"):
        (folder / name).write_bytes((SAMPLES / name).read_bytes())

    result = invoke("extract", folder, "--no-llm", "--out", tmp_path / "out")

    assert result.exit_code == 0, result.output
    assert "Summary" in result.output
    assert "2 processed" in result.output
    assert "REVIEW" in result.output  # the wrong-math sample
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "co_001_clean.json",
        "co_005_wrong_math.json",
    ]


def test_json_output_is_machine_readable(tmp_path):
    result = invoke("extract", SAMPLES / "co_004_credit.txt", "--no-llm", "--json", "--no-save")

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["fields"]["total"]["value"] == -6250.0
    assert data["extractors_used"] == ["rules"]


def test_plain_output_has_only_values(tmp_path):
    result = invoke(
        "extract", SAMPLES / "co_004_credit.txt", "--no-llm", "--plain", "--out", tmp_path
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "co_004_credit.json").read_text())
    assert data["total"] == -6250.0
    assert data["date"] == "2025-01-22"
    assert "confidence" not in json.dumps(data)


def test_no_save_writes_nothing(tmp_path):
    result = invoke(
        "extract", SAMPLES / "co_001_clean.txt", "--no-llm", "--no-save", "--out", tmp_path
    )
    assert result.exit_code == 0
    assert list(tmp_path.iterdir()) == []


def test_unreadable_file_exits_with_error(tmp_path):
    result = invoke("extract", tmp_path / "missing.pdf", "--no-llm", "--out", tmp_path)
    assert result.exit_code == 1
    assert "Could not read missing.pdf" in result.output


def test_empty_folder_exits_with_error(tmp_path):
    result = invoke("extract", tmp_path, "--no-llm")
    assert result.exit_code == 1
    assert "No supported files" in result.output


def test_formatting_helpers():
    assert format_value("total", -6250.0) == "-$6,250.00"
    assert format_value("schedule_impact_days", 1) == "1 day"
    assert format_value("line_items", [LineItem(description="a", amount=10)]) == "1 items ($10.00)"
    assert format_value("owner", None) == "-"
    assert shorten("line one\nline two", 12) == "line one ..."


def test_evidence_shows_its_page():
    assert evidence_text("TOTAL: $11,129.00", 2) == "(p.2) TOTAL: $11,129.00"
    assert evidence_text("TOTAL: $11,129.00", None) == "TOTAL: $11,129.00"
    assert evidence_text(None, 3) == ""


def test_page_range():
    assert page_range([2, 3, 4, 5, 6, 7, 8]) == "2-8"
    assert page_range([2, 3, 5]) == "2-3, 5"
    assert page_range([4]) == "4"
