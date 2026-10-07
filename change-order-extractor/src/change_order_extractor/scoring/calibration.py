"""Calibration: turn the explainable raw score into a measured probability.

The raw score (start -> agreement -> validation, see confidence.py) ranks values
well, but its numbers are hand-picked. On the evaluation set, values found by the
rules alone scored 0.77 and were right 162 times out of 162, while values only
Gemini found scored 0.42-0.65 and were right about 4 times in 5. Calibration maps a
raw score to the accuracy measured for values of the same kind and a similar score,
so "0.9" means "right about 9 times in 10".

Kinds of value (groups), by how they were found:

    both               rules and Gemini agreed
    conflict           they disagreed and a winner was picked
    rules_unconfirmed  only the rules found it; Gemini ran but did not
    llm_unconfirmed    only Gemini found it; the rules ran but did not
    rules_alone        rules-only run (no Gemini)
    llm_alone          Gemini-only run (no rules)

Fitting, per group: isotonic regression (pool adjacent violators) of right/wrong on
the raw score, so a higher raw score never gets a lower calibrated one. Each block is
shrunk toward its raw score as if PRIOR_STRENGTH more values had been seen at that
score, so a block of three values cannot jump to 100%. A group with no data keeps
its raw score.

The fitted table is stored as JSON (calibration.json next to this file) and written
by `cox calibrate`.
"""

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from change_order_extractor.schemas import FieldSource

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_FILE = Path(__file__).with_name("calibration.json")
PRIOR_STRENGTH = 10.0
MAX_CALIBRATED = 0.99

GROUPS = (
    "both",
    "conflict",
    "rules_unconfirmed",
    "llm_unconfirmed",
    "rules_alone",
    "llm_alone",
)


def group_for(source: FieldSource, cross_checked: bool) -> str | None:
    """The kind of value, or None for an empty field."""
    if source == FieldSource.BOTH:
        return "both"
    if source == FieldSource.CONFLICT:
        return "conflict"
    if source in (FieldSource.RULES, FieldSource.LLM):
        return f"{source.value}_{'unconfirmed' if cross_checked else 'alone'}"
    return None


@dataclass(frozen=True)
class Sample:
    """One returned value from an evaluation run, before calibration."""

    group: str
    raw: float
    correct: bool
    document: str = ""


class Point(BaseModel):
    """Values of one group with raw scores around `raw` were right `calibrated` of the
    time (after shrinking toward the raw score)."""

    model_config = ConfigDict(extra="forbid")

    raw: float
    calibrated: float
    count: int
    correct: int


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    fitted_at: str | None = None
    fitted_on: str | None = Field(default=None, description="What data it was fitted on.")
    prior_strength: float = PRIOR_STRENGTH
    groups: dict[str, list[Point]] = Field(default_factory=dict)

    def apply(self, group: str | None, raw: float) -> float | None:
        """Calibrated score, or None if this group has no fitted data."""
        points = self.groups.get(group or "")
        if not points:
            return None
        if raw <= points[0].raw:
            # below the lowest fitted score: scale down towards 0 (a validation error
            # can push a raw score far below anything seen in the data)
            value = points[0].calibrated * (raw / points[0].raw if points[0].raw else 1.0)
        elif raw >= points[-1].raw:
            value = points[-1].calibrated
        else:
            for low, high in zip(points, points[1:], strict=False):
                if low.raw <= raw <= high.raw:
                    share = (raw - low.raw) / (high.raw - low.raw) if high.raw > low.raw else 0
                    value = low.calibrated + share * (high.calibrated - low.calibrated)
                    break
        return round(min(max(value, 0.0), MAX_CALIBRATED), 2)

    def describe(self, group: str | None) -> str:
        points = self.groups.get(group or "", [])
        right = sum(p.correct for p in points)
        total = sum(p.count for p in points)
        return f"{group} values: {right} of {total} right in evaluation"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Calibration | None":
        """The table at `path`, or None (no calibration) if it is missing or broken."""
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError as error:
            logger.warning("Ignoring unreadable calibration file %s: %s", path, error)
            return None


# ------------------------------------------------------------------ fitting


def isotonic_blocks(points: list[tuple[float, bool]]) -> list[tuple[float, int, int]]:
    """Pool adjacent violators. Input sorted by raw score; output blocks of
    (sum of raw scores, count, number right) whose accuracy never decreases."""
    by_score: dict[float, list[int]] = defaultdict(lambda: [0, 0])
    for raw, correct in points:
        by_score[raw][0] += 1
        by_score[raw][1] += correct
    blocks: list[list[float]] = []  # [sum_raw, count, correct]
    for raw in sorted(by_score):
        count, correct = by_score[raw]
        blocks.append([raw * count, count, correct])
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][1] > blocks[-1][2] / blocks[-1][1]:
            last = blocks.pop()
            blocks[-1] = [a + b for a, b in zip(blocks[-1], last, strict=True)]
    return [(s, int(n), int(c)) for s, n, c in blocks]


def fit(
    samples: list[Sample], *, prior_strength: float = PRIOR_STRENGTH, fitted_on: str = ""
) -> Calibration:
    groups: dict[str, list[Point]] = {}
    for group in GROUPS:
        data = sorted((s.raw, s.correct) for s in samples if s.group == group)
        if not data:
            continue
        points: list[Point] = []
        for sum_raw, count, correct in isotonic_blocks(data):
            raw = sum_raw / count
            calibrated = (correct + prior_strength * raw) / (count + prior_strength)
            if points:  # shrinking can break the order slightly: keep it monotone
                calibrated = max(calibrated, points[-1].calibrated)
            points.append(
                Point(
                    raw=round(raw, 4),
                    calibrated=round(min(calibrated, MAX_CALIBRATED), 4),
                    count=count,
                    correct=correct,
                )
            )
        groups[group] = points
    return Calibration(
        fitted_at=dt.datetime.now().isoformat(timespec="seconds"),
        fitted_on=fitted_on or None,
        prior_strength=prior_strength,
        groups=groups,
    )


def cross_validated(
    samples: list[Sample], *, prior_strength: float = PRIOR_STRENGTH
) -> list[tuple[Sample, float]]:
    """Leave one document out: each document's values are calibrated by a table fitted
    on the other documents only, so the measured improvement is not just memorized."""
    results = []
    for document in sorted({s.document for s in samples}):
        table = fit([s for s in samples if s.document != document], prior_strength=prior_strength)
        for sample in (s for s in samples if s.document == document):
            calibrated = table.apply(sample.group, sample.raw)
            results.append((sample, sample.raw if calibrated is None else calibrated))
    return results
