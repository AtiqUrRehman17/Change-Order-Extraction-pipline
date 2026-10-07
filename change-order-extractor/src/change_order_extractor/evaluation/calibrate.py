"""Fit the confidence calibration from the evaluation documents.

    1. run every mode with calibration switched off (raw scores; Gemini answers come
       from the cache, so this is free after one evaluation run)
    2. compare every returned value with the ground truth -> (kind, raw score, right?)
    3. fit the calibration table (scoring/calibration.py) on all of it
    4. check it honestly with leave-one-document-out cross-validation: each document
       is calibrated by a table fitted without it, and the calibration error (ECE) is
       compared with the raw scores' error

`cox calibrate` runs this, saves the table and writes calibration_report.md.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from change_order_extractor.config import Settings
from change_order_extractor.evaluation.compare import compare_document
from change_order_extractor.evaluation.dataset import Case
from change_order_extractor.evaluation.metrics import CALIBRATION_EDGES
from change_order_extractor.evaluation.runner import make_pipeline
from change_order_extractor.ingestion import IngestionError
from change_order_extractor.scoring.calibration import (
    GROUPS,
    PRIOR_STRENGTH,
    Calibration,
    Sample,
    cross_validated,
    fit,
    group_for,
)


def collect_samples(
    cases: list[Case],
    modes: list[str],
    settings: Settings,
    *,
    today: dt.date | None = None,
    progress: Callable[[str, Case], None] | None = None,
) -> list[Sample]:
    """Every returned value of every mode, with its raw (uncalibrated) score."""
    samples: list[Sample] = []
    for mode in modes:
        pipeline = make_pipeline(mode, settings, today, calibrate=False)
        for case in cases:
            if progress:
                progress(mode, case)
            try:
                result = pipeline.run(case.path)
            except IngestionError:
                continue
            cross_checked = {"rules", "llm"} <= set(result.extractors_used)
            for compared in compare_document(case.truth, result.fields, case.unscored):
                group = group_for(compared.source, cross_checked)
                if compared.has_prediction and group is not None:
                    samples.append(
                        Sample(group, compared.confidence, compared.is_correct, case.name)
                    )
    return samples


def ece(pairs: list[tuple[float, bool]]) -> float | None:
    """Expected calibration error over (score, right?) pairs, same bins as the report."""
    if not pairs:
        return None
    total = 0.0
    edges = CALIBRATION_EDGES
    for low, high in zip(edges, edges[1:], strict=False):
        last = high == edges[-1]
        inside = [(s, c) for s, c in pairs if low <= s < high or (last and s == high)]
        if inside:
            confidence = sum(s for s, _ in inside) / len(inside)
            accuracy = sum(c for _, c in inside) / len(inside)
            total += len(inside) * abs(confidence - accuracy)
    return total / len(pairs)


@dataclass
class GroupCheck:
    group: str
    values: int
    right: int
    raw_ece: float | None
    calibrated_ece: float | None  # cross-validated


@dataclass
class CalibrationResult:
    table: Calibration
    samples: list[Sample]
    checks: list[GroupCheck]
    raw_ece: float | None
    calibrated_ece: float | None  # cross-validated


def calibrate(
    samples: list[Sample], *, prior_strength: float = PRIOR_STRENGTH
) -> CalibrationResult:
    documents = len({s.document for s in samples})
    table = fit(
        samples,
        prior_strength=prior_strength,
        fitted_on=f"{len(samples)} values from {documents} evaluation documents",
    )
    validated = cross_validated(samples, prior_strength=prior_strength)

    checks = []
    for group in GROUPS:
        in_group = [(s, c) for s, c in validated if s.group == group]
        if not in_group:
            continue
        checks.append(
            GroupCheck(
                group=group,
                values=len(in_group),
                right=sum(s.correct for s, _ in in_group),
                raw_ece=ece([(s.raw, s.correct) for s, _ in in_group]),
                calibrated_ece=ece([(c, s.correct) for s, c in in_group]),
            )
        )
    return CalibrationResult(
        table=table,
        samples=samples,
        checks=checks,
        raw_ece=ece([(s.raw, s.correct) for s in samples]),
        calibrated_ece=ece([(c, s.correct) for s, c in validated]),
    )


def calibration_report(result: CalibrationResult) -> str:
    def num(value: float | None) -> str:
        return "-" if value is None else f"{value:.3f}"

    lines = [
        "# Confidence calibration",
        "",
        f"Fitted {result.table.fitted_at} on {result.table.fitted_on}. Prior strength "
        f"{result.table.prior_strength:g} (each block is shrunk toward its raw score as if "
        f"that many more values had been seen).",
        "",
        "## Does it help? (leave-one-document-out cross-validation)",
        "",
        "Each document is calibrated with a table fitted on the *other* documents only, "
        "so these numbers are not just memorized. ECE = average gap between confidence "
        "and accuracy (lower is better).",
        "",
        "| Kind of value | Values | Right | ECE raw | ECE calibrated |",
        "|---|---|---|---|---|",
    ]
    for check in result.checks:
        lines.append(
            f"| {check.group} | {check.values} | {check.right} | {num(check.raw_ece)} | "
            f"{num(check.calibrated_ece)} |"
        )
    lines += [
        f"| **all** | {len(result.samples)} | {sum(s.correct for s in result.samples)} | "
        f"**{num(result.raw_ece)}** | **{num(result.calibrated_ece)}** |",
        "",
        "## The fitted table",
        "",
        "Raw scores between two points are interpolated.",
        "",
        "| Kind of value | Raw score | Calibrated | Values | Right |",
        "|---|---|---|---|---|",
    ]
    for group, points in result.table.groups.items():
        for point in points:
            lines.append(
                f"| {group} | {point.raw:.2f} | {point.calibrated:.2f} | {point.count} | "
                f"{point.correct} |"
            )
    lines += [
        "",
        "Caveat: about 20 documents, most of them generated samples. The table is only as "
        "good as that data; refit it (`cox calibrate --mode all`) when documents are added.",
    ]
    return "\n".join(lines) + "\n"
