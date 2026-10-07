"""Evaluation cases: a document plus its correct answers (ground truth).

A document `<folder>/<name>.pdf|.txt` is matched with a truth file named
`<name>.json`, looked up in `<folder>/ground_truth/` and then in the sibling
`ground_truth/` folder:

    data/samples/co_001_clean.txt         -> data/ground_truth/co_001_clean.json
    tests/fixtures/real/marshall_mn_co10  -> tests/fixtures/real/ground_truth/marshall_mn_co10.json

A truth file is either a plain ChangeOrder JSON (the generated samples; their
expected validation issues are in the folder's manifest.json) or a wrapped one:

    {"truth": {...ChangeOrder...},
     "expected_issues": ["total_mismatch"],   validation codes that SHOULD be raised
     "unscored": ["description"],             fields with no single right answer
     "notes": "how the values were read"}

Documents without a truth file are skipped (and listed in the report).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from change_order_extractor.ingestion import SUPPORTED_EXTENSIONS
from change_order_extractor.schemas import ChangeOrder

DEFAULT_FOLDERS = (Path("data/samples"), Path("tests/fixtures/real"))


class TruthFile(BaseModel):
    """The wrapped ground-truth format (used for the real documents)."""

    model_config = ConfigDict(extra="forbid")

    truth: ChangeOrder
    expected_issues: list[str] = Field(default_factory=list)
    unscored: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("unscored")
    @classmethod
    def known_fields(cls, names: list[str]) -> list[str]:
        unknown = set(names) - set(ChangeOrder.model_fields)
        if unknown:
            raise ValueError(f"unknown fields in 'unscored': {sorted(unknown)}")
        return names


@dataclass(frozen=True)
class Case:
    name: str  # file name, e.g. 'co_001_clean.txt'
    path: Path
    group: str  # the folder it came from, e.g. 'samples' / 'real'
    truth: ChangeOrder
    expected_issues: frozenset[str] = field(default_factory=frozenset)
    unscored: frozenset[str] = field(default_factory=frozenset)


class TruthError(ValueError):
    """A truth file exists but can't be read."""


def find_truth(document: Path) -> Path | None:
    for folder in (document.parent / "ground_truth", document.parent.parent / "ground_truth"):
        candidate = folder / f"{document.stem}.json"
        if candidate.is_file():
            return candidate
    return None


def manifest_issues(folder: Path) -> dict[str, list[str]]:
    """{file name: expected issue codes} from the samples' manifest.json, if any."""
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return {}
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    return {entry["file"]: entry.get("expected_issues", []) for entry in entries}


def read_truth(path: Path) -> TruthFile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "truth" in data:
            return TruthFile.model_validate(data)
        return TruthFile(truth=ChangeOrder.model_validate(data))
    except (ValueError, ValidationError) as error:
        raise TruthError(f"{path}: {error}") from error


def load_cases(folders: list[Path]) -> tuple[list[Case], list[Path]]:
    """(cases with a truth file, documents skipped because they have none)."""
    cases: list[Case] = []
    skipped: list[Path] = []
    for folder in folders:
        issues = manifest_issues(folder)
        documents = sorted(
            p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        for document in documents:
            truth_path = find_truth(document)
            if truth_path is None:
                skipped.append(document)
                continue
            truth = read_truth(truth_path)
            expected = set(truth.expected_issues) | set(issues.get(document.name, []))
            cases.append(
                Case(
                    name=document.name,
                    path=document,
                    group=folder.name,
                    truth=truth.truth,
                    expected_issues=frozenset(expected),
                    unscored=frozenset(truth.unscored),
                )
            )
    return cases, skipped
