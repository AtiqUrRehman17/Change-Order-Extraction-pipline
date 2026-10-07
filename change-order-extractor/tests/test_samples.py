"""Sanity checks for the sample dataset and its ground truth.

If these fail, the test data is wrong, not the pipeline.
"""

import json
from pathlib import Path

import pytest

from change_order_extractor.schemas import ChangeOrder

DATA = Path(__file__).resolve().parents[1] / "data"
MANIFEST = json.loads((DATA / "samples" / "manifest.json").read_text(encoding="utf-8"))


def load_truth(entry: dict) -> ChangeOrder:
    text = (DATA / "ground_truth" / entry["ground_truth"]).read_text(encoding="utf-8")
    return ChangeOrder.model_validate_json(text)


def test_manifest_is_not_empty():
    assert len(MANIFEST) >= 15


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_document_and_truth_exist(entry):
    assert (DATA / "samples" / entry["file"]).is_file()
    load_truth(entry)


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_ground_truth_math_is_consistent(entry):
    """Totals add up, unless the sample is *meant* to have wrong math."""
    co = load_truth(entry)
    if "total_mismatch" in entry["expected_issues"] or not co.line_items or co.total is None:
        return

    items = sum(i.amount for i in co.line_items)
    if co.subtotal is not None:
        assert items == pytest.approx(co.subtotal)
    expected_total = items + (co.markup_amount or 0) + (co.tax_amount or 0)
    assert expected_total == pytest.approx(co.total)


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_wrong_math_samples_really_are_wrong(entry):
    if "total_mismatch" not in entry["expected_issues"]:
        return
    co = load_truth(entry)
    expected_total = sum(i.amount for i in co.line_items) + (co.markup_amount or 0)
    assert expected_total != pytest.approx(co.total)


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["id"])
def test_bad_date_samples_really_are_bad(entry):
    co = load_truth(entry)
    approval_before = (
        co.approval_date is not None and co.date is not None and co.approval_date < co.date
    )
    assert approval_before == ("approval_before_date" in entry["expected_issues"])
