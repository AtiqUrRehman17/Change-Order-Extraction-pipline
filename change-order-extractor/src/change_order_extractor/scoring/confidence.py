"""Combines signals (extractor agreement, evidence, validation) into scores.

Every field's final confidence is built in four explainable steps, and each
step adds a note so a reviewer can see why a score is what it is:

    1. start      the extractor's preliminary confidence: how clearly the value was
                  labelled (rules) or how certain Gemini was, already lowered for
                  every normalization note (OCR fix, two-digit year, weeks->days...)
    2. agreement  both extractors agree      -> 1 - (1 - c)^2   (two independent sources;
                                                no boost if validation says the document
                                                is ambiguous there, e.g. several dates)
                  only one extractor found it -> c x 0.9
                  the extractors disagree     -> c x 0.6
    3. validation each error on the field     -> x 0.5
                  each warning on the field   -> x 0.85
    4. calibration the raw score from steps 1-3 is mapped to the accuracy measured on
                  the evaluation set for values of the same kind (both / rules only /
                  Gemini only / conflict) and a similar raw score (see calibration.py),
                  so the final number is a probability of being right

The document's overall confidence is a weighted average over the fields: the
money, CO number and date matter most. A missing required field counts as 0.

A document needs review for validation errors, review-level warnings, a low overall
score, a low or conflicting critical field, or any value more likely wrong than right
(calibrated score below 0.5; see VERY_LOW_CONFIDENCE).
"""

from dataclasses import dataclass

from change_order_extractor.schemas import (
    ChangeOrderStatus,
    ExtractedChangeOrder,
    ExtractedField,
    FieldSource,
    Severity,
    ValidationReport,
)
from change_order_extractor.scoring.calibration import Calibration, group_for

SINGLE_SOURCE_FACTOR = 0.9
CONFLICT_FACTOR = 0.6
ERROR_FACTOR = 0.5
WARNING_FACTOR = 0.85
MAX_CONFIDENCE = 0.99

# How much each field counts towards the document's overall confidence.
WEIGHTS = {
    "total": 3.0,
    "change_order_number": 2.0,
    "date": 2.0,
    "line_items": 2.0,
    "schedule_impact_days": 1.5,
    "project_name": 1.0,
    "contractor": 1.0,
    "owner": 1.0,
    "description": 1.0,
    "status": 1.0,
    "subtotal": 1.0,
    "markup_amount": 1.0,
    "tax_amount": 0.5,
    "project_number": 0.5,
    "reason": 0.5,
    "requested_by": 0.5,
    "approved_by": 0.5,
    "approval_date": 0.5,
}
# Missing one of these lowers the overall score (they count as 0).
REQUIRED = ("change_order_number", "date", "total")
# A low score on one of these alone is enough to send the document for review.
CRITICAL = ("total", "change_order_number", "date")
# Warnings meaning "the document itself is ambiguous here". Agreement between the
# extractors is no proof then (they can be misled the same way), so these fields
# get no agreement boost, and the warning becomes a review reason.
AMBIGUITY_CODES = {"ambiguous_date"}
# Warnings a human must look at even though they are not errors.
REVIEW_WARNING_CODES = AMBIGUITY_CODES | {"approval_not_confirmed"}
# Any value scoring below this is a review reason, critical or not: a likely wrong value
# must not pass silently just because the overall score is fine (e.g. a schedule read
# from the original contract time, or an approver Gemini guessed). With calibrated
# scores this means "more likely wrong than right". Without a calibration table it is
# used only when both extractors ran: raw rules-only scores of correct heuristic
# guesses (letterhead contractor, blank signature) are just as low.
VERY_LOW_CONFIDENCE = 0.5
# The same rule on raw (uncalibrated) scores: just below a single-source heuristic value.
RAW_VERY_LOW_CONFIDENCE = 0.45


@dataclass
class Scored:
    fields: ExtractedChangeOrder
    overall_confidence: float
    needs_review: bool
    review_reasons: list[str]


def score_field(
    name: str,
    field: ExtractedField,
    report: ValidationReport,
    *,
    cross_checked: bool = True,
    calibration: Calibration | None = None,
) -> ExtractedField:
    if field.value is None:
        return field.model_copy(update={"confidence": 0.0})

    confidence = field.confidence
    notes = list(field.notes)

    ambiguous = any(problem.code in AMBIGUITY_CODES for problem in report.issues_for(name))
    if field.source == FieldSource.BOTH and ambiguous:
        # Both read the same misleading layout: agreement is not independent evidence.
        notes.append("rules and Gemini agree, but the document is ambiguous here (no boost)")
    elif field.source == FieldSource.BOTH:
        confidence = 1 - (1 - confidence) ** 2
        notes.append("rules and Gemini agree")
    elif field.source == FieldSource.CONFLICT:
        confidence *= CONFLICT_FACTOR
    elif field.source in (FieldSource.RULES, FieldSource.LLM):
        confidence *= SINGLE_SOURCE_FACTOR
        notes.append(f"found by one extractor only ({field.source.value})")

    for problem in report.issues_for(name):
        if problem.severity == Severity.ERROR:
            confidence *= ERROR_FACTOR
            notes.append(f"validation error: {problem.code}")
        else:
            confidence *= WARNING_FACTOR
            notes.append(f"validation warning: {problem.code}")

    confidence = round(min(max(confidence, 0.0), MAX_CONFIDENCE), 2)

    if calibration is not None:
        group = group_for(field.source, cross_checked)
        calibrated = calibration.apply(group, confidence)
        if calibrated is not None:
            notes.append(
                f"calibrated {confidence:.2f} -> {calibrated:.2f} ({calibration.describe(group)})"
            )
            confidence = calibrated
    return field.model_copy(update={"confidence": confidence, "notes": notes})


def overall_confidence(fields: ExtractedChangeOrder) -> float:
    total_weight = weighted = 0.0
    for name, weight in WEIGHTS.items():
        field = getattr(fields, name)
        if field.value is None and name not in REQUIRED:
            continue  # optional and absent: says nothing about extraction quality
        total_weight += weight
        weighted += weight * field.confidence
    return round(weighted / total_weight, 2) if total_weight else 0.0


def score(
    fields: ExtractedChangeOrder,
    report: ValidationReport,
    *,
    review_threshold: float = 0.75,
    cross_checked: bool = True,
    calibration: Calibration | None = None,
) -> Scored:
    """Final confidence for every field, the overall score and the review decision.

    cross_checked: both extractors ran (False for a rules-only or Gemini-only result).
    calibration: the fitted table (None = keep the raw scores)."""
    scored = ExtractedChangeOrder.model_validate(
        {
            name: score_field(
                name,
                getattr(fields, name),
                report,
                cross_checked=cross_checked,
                calibration=calibration,
            ).model_dump()
            for name in ExtractedChangeOrder.model_fields
        }
    )
    overall = overall_confidence(scored)

    reasons = [f"{problem.code}: {problem.message}" for problem in report.errors]
    reasons += [
        f"{problem.code}: {problem.message}"
        for problem in report.warnings
        if problem.code in REVIEW_WARNING_CODES
    ]
    # Whether a change order is approved is a business decision: an uncertain
    # 'approved' must not pass silently.
    status = scored.status
    if status.value == ChangeOrderStatus.APPROVED and status.confidence < review_threshold:
        reasons.append(
            f"approval not confirmed: status 'approved' has low confidence "
            f"({status.confidence:.2f})"
        )
    if overall < review_threshold:
        reasons.append(f"overall confidence {overall:.2f} is below {review_threshold:.2f}")
    for name in CRITICAL:
        field = getattr(scored, name)
        if field.value is not None and field.confidence < review_threshold:
            reasons.append(f"low confidence in {name} ({field.confidence:.2f})")
        if field.source == FieldSource.CONFLICT:
            reasons.append(f"rules and Gemini disagree on {name}")
    calibrated = calibration is not None
    very_low = VERY_LOW_CONFIDENCE if calibrated else RAW_VERY_LOW_CONFIDENCE
    for name in ExtractedChangeOrder.model_fields:
        field = getattr(scored, name)
        low = field.value is not None and field.confidence < very_low
        if (calibrated or cross_checked) and low and name not in CRITICAL:  # own, higher bar
            reasons.append(f"very low confidence in {name} ({field.confidence:.2f})")

    reasons = list(dict.fromkeys(reasons))
    return Scored(scored, overall, bool(reasons), reasons)
