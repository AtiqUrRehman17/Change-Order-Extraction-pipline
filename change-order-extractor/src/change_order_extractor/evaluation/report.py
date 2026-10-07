"""Write the evaluation results as a Markdown report and a JSON summary."""

import datetime as dt
import json
from typing import Any

from change_order_extractor.evaluation.compare import FieldResult
from change_order_extractor.evaluation.metrics import ModeReport, ReviewStats
from change_order_extractor.interfaces.display import format_value
from change_order_extractor.schemas import ChangeOrder

MODE_TITLES = {"rules": "Rules only", "llm": "Gemini only", "hybrid": "Hybrid"}


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return [*lines, ""]


def show(name: str, value: Any) -> str:
    text = format_value(name, value)
    if isinstance(value, list) and value:
        text += ": " + ", ".join(format_value("total", item.amount) for item in value)
    return text.replace("|", "/").replace("\n", " ")


# ------------------------------------------------------------------ sections


def summary_rows(reports: list[ModeReport]) -> list[list[str]]:
    def row(label: str, get) -> list[str]:
        return [label, *(get(r) for r in reports)]

    return [
        row("Documents evaluated", lambda r: str(len(r.docs))),
        row("Gemini answered", lambda r: "-" if r.mode == "rules" else str(r.gemini_answered)),
        row(
            "Documents fully correct",
            lambda r: f"{r.fully_correct}/{len(r.docs)}",
        ),
        row("Field accuracy", lambda r: pct(r.overall.accuracy)),
        row("Precision (returned values right)", lambda r: pct(r.overall.precision)),
        row("Recall (stated values found)", lambda r: pct(r.overall.recall)),
        row("Wrong values", lambda r: str(r.overall.wrong)),
        row("Missed values", lambda r: str(r.overall.missed)),
        row("Hallucinated values", lambda r: str(r.overall.hallucinated)),
        row(
            "Line items found (recall)",
            lambda r: f"{r.items.found}/{r.items.expected} ({pct(r.items.recall)})",
        ),
        row("Line items extra", lambda r: str(r.items.extra)),
        row("Calibration error (ECE, lower is better)", lambda r: num(r.ece)),
        row("Review: errors caught", lambda r: review_catch(r.review)),
        row("Review: false alarms", lambda r: review_false(r.review)),
        row("Not flagged and fully correct", lambda r: review_accept(r.review)),
        row("Avg seconds per document", lambda r: num(r.avg_seconds)),
    ]


def review_catch(stats: ReviewStats) -> str:
    return f"{stats.caught}/{stats.with_errors} ({pct(stats.catch_rate)})"


def review_false(stats: ReviewStats) -> str:
    correct = stats.documents - stats.with_errors
    return f"{stats.false_alarms}/{correct} ({pct(stats.false_alarm_rate)})"


def review_accept(stats: ReviewStats) -> str:
    return f"{stats.accepted_correct}/{stats.accepted} ({pct(stats.auto_accept_accuracy)})"


def field_rows(reports: list[ModeReport]) -> list[list[str]]:
    rows = []
    for name in ChangeOrder.model_fields:
        cells = []
        for report in reports:
            counts = report.fields.get(name)
            if counts is None:
                cells.append("-")
                continue
            right = counts.correct + counts.correct_empty
            cells.append(f"{right}/{counts.total} ({pct(counts.accuracy)})")
        rows.append([name, *cells])
    return rows


def mode_section(report: ModeReport) -> list[str]:
    title = MODE_TITLES.get(report.mode, report.mode)
    lines = [f"## {title} (`{report.mode}`)", ""]
    if report.models:
        lines += [f"Models that answered: {', '.join(report.models)}", ""]
    if report.failed:
        lines += ["Could not read:", *(f"- {name}: {error}" for name, error in report.failed), ""]

    lines += ["### Outcomes per field", ""]
    lines += table(
        [
            "Field",
            "Correct",
            "Correct empty",
            "Wrong",
            "Missed",
            "Hallucinated",
            "Precision",
            "Recall",
        ],
        [
            [
                name,
                str(c.correct),
                str(c.correct_empty),
                str(c.wrong),
                str(c.missed),
                str(c.hallucinated),
                pct(c.precision),
                pct(c.recall),
            ]
            for name, c in report.fields.items()
        ],
    )

    lines += ["### Confidence calibration", ""]
    lines += [
        "Every returned value, grouped by its confidence. A well-calibrated system has "
        "accuracy close to the average confidence in each row (gap > 0 = overconfident).",
        "",
    ]
    lines += table(
        ["Confidence", "Values", "Avg confidence", "Accuracy", "Gap"],
        [
            [
                f"{b.low:.1f}-{b.high:.1f}",
                str(b.count),
                num(b.avg_confidence),
                pct(b.accuracy),
                num(b.gap),
            ]
            for b in report.bins
        ],
    )
    lines += [f"Expected calibration error (ECE): **{num(report.ece, 3)}**", ""]

    lines += ["### Review flag", ""]
    lines += table(
        [
            "A document 'has errors' when…",
            "Flagged",
            "Errors caught",
            "False alarms",
            "Not flagged and correct",
        ],
        [
            [
                label,
                f"{s.flagged}/{s.documents}",
                review_catch(s),
                review_false(s),
                review_accept(s),
            ]
            for label, s in (
                ("any scored field is wrong", report.review),
                ("total, CO number or date is wrong", report.review_critical),
            )
        ],
    )

    if report.sources:
        lines += ["### Accuracy by merge source", ""]
        lines += table(
            ["Source", "Values", "Right", "Accuracy"],
            [
                [source, str(n), str(right), pct(right / n if n else None)]
                for source, (n, right) in report.sources.items()
            ],
        )

    if report.checks:
        lines += ["### Validation checks (errors and review warnings)", ""]
        lines += [
            "'Real problem' = the document itself has it. 'Caught our error' = the document is "
            "fine, but a field the check names is wrong in our output, so the alarm was still "
            "useful. 'False alarm' = nothing was wrong.",
            "",
        ]
        lines += table(
            [
                "Check",
                "Raised",
                "Real problem",
                "Caught our error",
                "False alarm",
                "Missed",
                "Useful",
            ],
            [
                [
                    code,
                    str(s.raised),
                    str(s.real),
                    str(s.caught_error),
                    str(s.false_alarms),
                    str(s.missed),
                    pct(s.useful_rate),
                ]
                for code, s in report.checks.items()
            ],
        )

    lines += ["### Every wrong field (for error analysis)", ""]
    rows = [error_row(doc.case.name, error) for doc in report.docs for error in doc.errors]
    if rows:
        lines += table(["Document", "Field", "Outcome", "Expected", "Got", "Conf.", "Source"], rows)
    else:
        lines += ["None.", ""]
    return lines


def error_row(document: str, error: FieldResult) -> list[str]:
    outcome = error.outcome.value
    if error.similarity is not None:
        outcome += f" (overlap {error.similarity:.2f})"
    return [
        document,
        error.name,
        outcome,
        show(error.name, error.expected),
        show(error.name, error.predicted),
        num(error.confidence) if error.has_prediction else "-",
        error.source.value,
    ]


# ------------------------------------------------------------------ outputs


def markdown_report(reports: list[ModeReport], *, skipped: list[str] | None = None) -> str:
    groups = sorted({doc.case.group for report in reports for doc in report.docs})
    lines = [
        "# Evaluation report",
        "",
        f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}. Document sets: {', '.join(groups) or '-'}.",
        "",
        "How values are compared: money within 1 cent; CO and project numbers after cleanup "
        "('CO-009' = '9'); names by word overlap >= 0.8; description and reason by word "
        "overlap >= 0.5; dates, days and status exactly; line items by amount. "
        "'Correct empty' means the document doesn't state the value and none was returned; "
        "'hallucinated' means a value was returned that the document doesn't state.",
        "",
    ]
    if skipped:
        lines += [f"Skipped (no ground truth): {', '.join(skipped)}", ""]

    header = ["Metric", *(MODE_TITLES.get(r.mode, r.mode) for r in reports)]
    lines += ["## Summary", "", *table(header, summary_rows(reports))]
    lines += ["## Accuracy per field", "", *table(["Field", *header[1:]], field_rows(reports))]
    for report in reports:
        lines += mode_section(report)
    return "\n".join(lines).rstrip() + "\n"


def json_summary(reports: list[ModeReport]) -> str:
    def counts(c) -> dict[str, Any]:
        return {
            "correct": c.correct,
            "correct_empty": c.correct_empty,
            "wrong": c.wrong,
            "missed": c.missed,
            "hallucinated": c.hallucinated,
            "accuracy": c.accuracy,
            "precision": c.precision,
            "recall": c.recall,
        }

    def review(s: ReviewStats) -> dict[str, Any]:
        return {
            "documents": s.documents,
            "flagged": s.flagged,
            "with_errors": s.with_errors,
            "catch_rate": s.catch_rate,
            "false_alarm_rate": s.false_alarm_rate,
            "auto_accept_accuracy": s.auto_accept_accuracy,
        }

    data = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "modes": {
            r.mode: {
                "documents": len(r.docs),
                "failed": [name for name, _ in r.failed],
                "gemini_answered": r.gemini_answered,
                "models": r.models,
                "fully_correct": r.fully_correct,
                "overall": counts(r.overall),
                "fields": {name: counts(c) for name, c in r.fields.items()},
                "line_items": {
                    "expected": r.items.expected,
                    "found": r.items.found,
                    "missed": r.items.missed,
                    "extra": r.items.extra,
                    "category_accuracy": r.items.category_accuracy,
                },
                "calibration": {
                    "ece": r.ece,
                    "bins": [
                        {
                            "low": b.low,
                            "high": b.high,
                            "count": b.count,
                            "avg_confidence": b.avg_confidence,
                            "accuracy": b.accuracy,
                        }
                        for b in r.bins
                    ],
                },
                "review": review(r.review),
                "review_critical": review(r.review_critical),
                "sources": {
                    s: {"values": n, "right": right} for s, (n, right) in r.sources.items()
                },
                "checks": {
                    code: {
                        "raised": s.raised,
                        "real": s.real,
                        "caught_error": s.caught_error,
                        "false_alarms": s.false_alarms,
                        "missed": s.missed,
                    }
                    for code, s in r.checks.items()
                },
                "errors": [
                    {
                        "document": doc.case.name,
                        "field": e.name,
                        "outcome": e.outcome.value,
                        "expected": show(e.name, e.expected),
                        "got": show(e.name, e.predicted),
                        "confidence": e.confidence,
                        "source": e.source.value,
                    }
                    for doc in r.docs
                    for e in doc.errors
                ],
                "avg_seconds": r.avg_seconds,
            }
            for r in reports
        },
    }
    return json.dumps(data, indent=2) + "\n"
