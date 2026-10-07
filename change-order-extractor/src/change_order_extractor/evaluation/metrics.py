"""Turn per-field comparisons into numbers.

field accuracy      right values (incl. correctly empty) / fields scored
precision           when we return a value, how often is it right?
recall              of the values the documents state, how many did we get right?
line items          true items found / missed / extra, and categories right
calibration         do confidence scores match accuracy? (bins + ECE)
review flag         does NEEDS REVIEW catch the documents with errors?
by merge source     accuracy of 'both' / 'rules' / 'llm' / 'conflict' values
validation checks   are the errors we raise real problems (false alarms)?
"""

from collections import defaultdict
from dataclasses import dataclass, field

from change_order_extractor.evaluation.compare import FieldResult, ItemMatch, Outcome
from change_order_extractor.evaluation.dataset import Case
from change_order_extractor.schemas import ExtractionResult, Severity
from change_order_extractor.scoring.confidence import CRITICAL, REVIEW_WARNING_CODES

CALIBRATION_EDGES = (0.0, 0.5, 0.7, 0.9, 1.0)


def ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


# ------------------------------------------------------------------ one document


@dataclass
class DocResult:
    case: Case
    result: ExtractionResult
    fields: list[FieldResult]
    items: ItemMatch

    @property
    def errors(self) -> list[FieldResult]:
        return [f for f in self.fields if not f.is_correct]

    @property
    def critical_errors(self) -> list[FieldResult]:
        return [f for f in self.errors if f.name in CRITICAL]

    @property
    def gemini_answered(self) -> bool:
        return "llm" in self.result.extractors_used


# ------------------------------------------------------------------ field accuracy


@dataclass
class Counts:
    correct: int = 0
    correct_empty: int = 0
    wrong: int = 0
    missed: int = 0
    hallucinated: int = 0

    def add(self, outcome: Outcome) -> None:
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)

    @property
    def total(self) -> int:
        return self.correct + self.correct_empty + self.wrong + self.missed + self.hallucinated

    @property
    def accuracy(self) -> float | None:
        return ratio(self.correct + self.correct_empty, self.total)

    @property
    def precision(self) -> float | None:
        return ratio(self.correct, self.correct + self.wrong + self.hallucinated)

    @property
    def recall(self) -> float | None:
        return ratio(self.correct, self.correct + self.wrong + self.missed)


def field_counts(docs: list[DocResult]) -> tuple[dict[str, Counts], Counts]:
    """(counts per field, counts over all fields)."""
    per_field: dict[str, Counts] = defaultdict(Counts)
    overall = Counts()
    for doc in docs:
        for result in doc.fields:
            per_field[result.name].add(result.outcome)
            overall.add(result.outcome)
    return dict(per_field), overall


# ------------------------------------------------------------------ line items


@dataclass
class ItemStats:
    expected: int = 0
    found: int = 0
    missed: int = 0
    extra: int = 0
    category_correct: int = 0

    @property
    def recall(self) -> float | None:
        return ratio(self.found, self.expected)

    @property
    def precision(self) -> float | None:
        return ratio(self.found, self.found + self.extra)

    @property
    def category_accuracy(self) -> float | None:
        return ratio(self.category_correct, self.found)


def item_stats(docs: list[DocResult]) -> ItemStats:
    stats = ItemStats()
    for doc in docs:
        if "line_items" in doc.case.unscored:
            continue
        stats.expected += len(doc.case.truth.line_items)
        stats.found += len(doc.items.pairs)
        stats.missed += len(doc.items.missed)
        stats.extra += len(doc.items.extra)
        stats.category_correct += doc.items.category_correct
    return stats


# ------------------------------------------------------------------ calibration


@dataclass
class Bin:
    low: float
    high: float
    count: int = 0
    confidence_sum: float = 0.0
    correct: int = 0

    @property
    def avg_confidence(self) -> float | None:
        return self.confidence_sum / self.count if self.count else None

    @property
    def accuracy(self) -> float | None:
        return ratio(self.correct, self.count)

    @property
    def gap(self) -> float | None:
        """Positive = overconfident (scores higher than the real accuracy)."""
        if not self.count:
            return None
        return self.avg_confidence - self.accuracy


def calibration(docs: list[DocResult]) -> tuple[list[Bin], float | None]:
    """Confidence bins over every returned value, and the expected calibration error
    (the average |confidence - accuracy| gap, weighted by how many values are in each bin)."""
    edges = CALIBRATION_EDGES
    bins = [Bin(low, high) for low, high in zip(edges, edges[1:], strict=False)]
    for doc in docs:
        for result in doc.fields:
            if not result.has_prediction:
                continue
            index = next(
                i for i, b in enumerate(bins) if result.confidence < b.high or b is bins[-1]
            )
            bins[index].count += 1
            bins[index].confidence_sum += result.confidence
            bins[index].correct += result.is_correct
    values = sum(b.count for b in bins)
    if not values:
        return bins, None
    ece = sum(b.count / values * abs(b.gap) for b in bins if b.count)
    return bins, ece


# ------------------------------------------------------------------ review flag


@dataclass
class ReviewStats:
    """How well NEEDS REVIEW separates documents with errors from correct ones."""

    documents: int = 0
    flagged: int = 0
    with_errors: int = 0
    caught: int = 0  # had errors and was flagged
    false_alarms: int = 0  # fully correct but flagged
    accepted: int = 0  # not flagged
    accepted_correct: int = 0  # not flagged and fully correct

    @property
    def catch_rate(self) -> float | None:
        return ratio(self.caught, self.with_errors)

    @property
    def false_alarm_rate(self) -> float | None:
        return ratio(self.false_alarms, self.documents - self.with_errors)

    @property
    def auto_accept_accuracy(self) -> float | None:
        return ratio(self.accepted_correct, self.accepted)


def review_stats(docs: list[DocResult], *, critical_only: bool = False) -> ReviewStats:
    """critical_only: a document 'has errors' only if total / CO number / date is wrong."""
    stats = ReviewStats()
    for doc in docs:
        has_errors = bool(doc.critical_errors if critical_only else doc.errors)
        flagged = doc.result.needs_review
        stats.documents += 1
        stats.flagged += flagged
        stats.with_errors += has_errors
        stats.caught += has_errors and flagged
        stats.false_alarms += flagged and not has_errors
        stats.accepted += not flagged
        stats.accepted_correct += not flagged and not has_errors
    return stats


# ------------------------------------------------------------------ merge source


def source_accuracy(docs: list[DocResult]) -> dict[str, tuple[int, int]]:
    """{source: (values returned, values right)} - is 'both' really safer than 'conflict'?"""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for doc in docs:
        for result in doc.fields:
            if result.has_prediction:
                counts[result.source.value][0] += 1
                counts[result.source.value][1] += result.is_correct
    return {source: (n, right) for source, (n, right) in sorted(counts.items())}


# ------------------------------------------------------------------ validation checks


@dataclass
class CheckStats:
    """An alarm is useful when the document really has the problem, or when it points
    at a field our output got wrong (e.g. 'missing_total' because a scan wasn't read)."""

    raised: int = 0
    real: int = 0  # the document really has this problem
    caught_error: int = 0  # not a document problem, but a field it names is wrong in our output
    false_alarms: int = 0  # the document and our values are fine there
    missed: int = 0  # the document has this problem, but it wasn't raised

    @property
    def useful_rate(self) -> float | None:
        return ratio(self.real + self.caught_error, self.raised)


def judged_issues(result: ExtractionResult) -> dict[str, set[str]]:
    """{code: fields involved} for the issues that send a document to review:
    errors and review-level warnings."""
    issues: dict[str, set[str]] = defaultdict(set)
    for issue in result.validation.issues:
        if issue.severity == Severity.ERROR or issue.code in REVIEW_WARNING_CODES:
            issues[issue.code].update(issue.fields)
    return issues


def check_stats(docs: list[DocResult]) -> dict[str, CheckStats]:
    stats: dict[str, CheckStats] = defaultdict(CheckStats)
    for doc in docs:
        raised, expected = judged_issues(doc.result), doc.case.expected_issues
        wrong_fields = {error.name for error in doc.errors}
        for code, fields in raised.items():
            stats[code].raised += 1
            if code in expected:
                stats[code].real += 1
            elif fields & wrong_fields:
                stats[code].caught_error += 1
            else:
                stats[code].false_alarms += 1
        for code in expected - raised.keys():
            stats[code].missed += 1
    return dict(sorted(stats.items()))


# ------------------------------------------------------------------ one mode


@dataclass
class ModeReport:
    mode: str
    docs: list[DocResult]
    failed: list[tuple[str, str]] = field(default_factory=list)  # (file, error)

    def __post_init__(self) -> None:
        self.fields, self.overall = field_counts(self.docs)
        self.items = item_stats(self.docs)
        self.bins, self.ece = calibration(self.docs)
        self.review = review_stats(self.docs)
        self.review_critical = review_stats(self.docs, critical_only=True)
        self.sources = source_accuracy(self.docs)
        self.checks = check_stats(self.docs)

    @property
    def fully_correct(self) -> int:
        return sum(not doc.errors for doc in self.docs)

    @property
    def gemini_answered(self) -> int:
        return sum(doc.gemini_answered for doc in self.docs)

    @property
    def avg_seconds(self) -> float | None:
        seconds = [doc.result.processing_seconds or 0.0 for doc in self.docs]
        return sum(seconds) / len(seconds) if seconds else None

    @property
    def models(self) -> list[str]:
        return sorted({doc.result.model for doc in self.docs if doc.result.model})
