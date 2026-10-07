"""Step 13: EVALUATE - measure the pipeline against documents with known answers.

cases, skipped = load_cases([Path("data/samples"), Path("tests/fixtures/real")])
reports = evaluate(cases, ["rules", "hybrid"], settings)
Path("eval_report.md").write_text(markdown_report(reports))
"""

from change_order_extractor.evaluation.compare import (
    FieldResult,
    ItemMatch,
    Outcome,
    compare_document,
    compare_field,
    match_items,
    word_f1,
)
from change_order_extractor.evaluation.dataset import (
    DEFAULT_FOLDERS,
    Case,
    TruthError,
    TruthFile,
    load_cases,
)
from change_order_extractor.evaluation.metrics import DocResult, ModeReport
from change_order_extractor.evaluation.report import json_summary, markdown_report
from change_order_extractor.evaluation.runner import MODES, evaluate, evaluate_mode

__all__ = [
    "DEFAULT_FOLDERS",
    "MODES",
    "Case",
    "DocResult",
    "FieldResult",
    "ItemMatch",
    "ModeReport",
    "Outcome",
    "TruthError",
    "TruthFile",
    "compare_document",
    "compare_field",
    "evaluate",
    "evaluate_mode",
    "json_summary",
    "load_cases",
    "markdown_report",
    "match_items",
    "word_f1",
]
