"""Run the pipeline on every evaluation case, in one or more modes.

    rules    rules only (free, offline)
    llm      Gemini only (the rule-based extractor is switched off)
    hybrid   rules + Gemini, merged - what `cox extract` does

Gemini answers are cached on disk, so 'llm' and 'hybrid' share one API call per
document, and re-running the evaluation costs no quota.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

from change_order_extractor.config import Settings
from change_order_extractor.evaluation.compare import ItemMatch, compare_document, match_items
from change_order_extractor.evaluation.dataset import Case
from change_order_extractor.evaluation.metrics import DocResult, ModeReport
from change_order_extractor.ingestion import IngestionError
from change_order_extractor.pipeline import Pipeline, save_result

MODES = ("rules", "llm", "hybrid")


def make_pipeline(
    mode: str, settings: Settings, today: dt.date | None = None, *, calibrate: bool = True
) -> Pipeline:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
    return Pipeline(
        settings,
        use_rules=mode != "llm",
        use_llm=mode != "rules",
        today=today,
        calibrate=calibrate,
    )


def evaluate_case(pipeline: Pipeline, case: Case) -> DocResult:
    result = pipeline.run(case.path)
    fields = compare_document(case.truth, result.fields, case.unscored)
    items = (
        ItemMatch()
        if "line_items" in case.unscored
        else match_items(case.truth.line_items, result.fields.line_items.value or [])
    )
    return DocResult(case, result, fields, items)


def evaluate_mode(
    mode: str,
    cases: list[Case],
    settings: Settings,
    *,
    today: dt.date | None = None,
    results_dir: Path | None = None,
    progress: Callable[[str, Case], None] | None = None,
) -> ModeReport:
    """Run one mode on every case. With results_dir, each extraction result is saved
    as JSON there (for looking at the errors in detail)."""
    pipeline = make_pipeline(mode, settings, today)
    docs: list[DocResult] = []
    failed: list[tuple[str, str]] = []
    for case in cases:
        if progress:
            progress(mode, case)
        try:
            doc = evaluate_case(pipeline, case)
        except IngestionError as error:
            failed.append((case.name, str(error)))
            continue
        docs.append(doc)
        if results_dir is not None:
            save_result(doc.result, results_dir / mode)
    return ModeReport(mode, docs, failed)


def evaluate(
    cases: list[Case],
    modes: list[str],
    settings: Settings,
    **kwargs,
) -> list[ModeReport]:
    return [evaluate_mode(mode, cases, settings, **kwargs) for mode in modes]
