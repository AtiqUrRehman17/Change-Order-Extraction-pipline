"""Business rules: totals add up, dates are logical, required fields exist.

Each check looks at the extracted values (and, for evidence checks, the source
document) and returns a list of ValidationIssues:

    error    the data is wrong or incomplete (math doesn't add up, required field
             missing, evidence not in the document)
    warning  suspicious, worth a look, but may be fine

Validation never changes values. It reports; confidence scoring and the human
reviewer decide what to do about it.
"""

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from change_order_extractor.extraction.line_items import money_tokens
from change_order_extractor.extraction.rule_based import (
    SIGNATURE_LINE,
    dates_on_line,
    issue_date,
    owner_signature,
)
from change_order_extractor.normalization import parse_date, parse_days
from change_order_extractor.normalization.money import CREDIT_WORDS
from change_order_extractor.schemas import (
    ChangeOrderStatus,
    Document,
    ExtractedChangeOrder,
    Severity,
    ValidationIssue,
    ValidationReport,
)

ERROR, WARNING = Severity.ERROR, Severity.WARNING

MONEY_TOLERANCE = 0.05  # dollars; allows for rounding on printed amounts
MAX_SCHEDULE_DAYS = 365
# Evidence that isn't found exactly but shares this much of its text with the
# document (longest common run / evidence length) counts as a near match.
NEAR_MATCH = 0.8

REQUIRED = [
    ("change_order_number", ERROR),
    ("date", ERROR),
    ("total", ERROR),
    ("description", WARNING),
    ("project_name", WARNING),
    ("contractor", WARNING),
]

PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
NUMBER = re.compile(r"\d(?:[\d,.]*\d)?")


@dataclass
class Context:
    """What every check can look at."""

    fields: ExtractedChangeOrder
    document: Document | None
    today: dt.date
    tolerance: float
    values: dict = field(init=False)

    def __post_init__(self) -> None:
        self.values = {
            name: getattr(self.fields, name).value for name in type(self.fields).model_fields
        }


Check = Callable[[Context], list[ValidationIssue]]


def issue(code: str, message: str, severity: Severity, *fields: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity=severity, fields=list(fields))


def money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def close(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


# ------------------------------------------------------------------ required fields


def check_required(ctx: Context) -> list[ValidationIssue]:
    issues = []
    for name, severity in REQUIRED:
        if ctx.values[name] in (None, ""):
            label = name.replace("_", " ")
            issues.append(issue(f"missing_{name}", f"No {label} found.", severity, name))
    return issues


# ------------------------------------------------------------------ math


def items_sum(ctx: Context) -> float | None:
    items = ctx.values["line_items"]
    return sum(item.amount for item in items) if items else None


def check_subtotal(ctx: Context) -> list[ValidationIssue]:
    subtotal, total_items = ctx.values["subtotal"], items_sum(ctx)
    if subtotal is None or total_items is None or close(subtotal, total_items, ctx.tolerance):
        return []
    return [
        issue(
            "subtotal_mismatch",
            f"Line items add up to {money(total_items)}, but the subtotal says {money(subtotal)} "
            f"(difference {money(subtotal - total_items)}).",
            ERROR,
            "subtotal",
            "line_items",
        )
    ]


def check_total(ctx: Context) -> list[ValidationIssue]:
    total = ctx.values["total"]
    base = ctx.values["subtotal"] if ctx.values["subtotal"] is not None else items_sum(ctx)
    if total is None or base is None:
        return []  # nothing to check against (e.g. lump sum)

    markup = ctx.values["markup_amount"] or 0.0
    tax = ctx.values["tax_amount"] or 0.0
    expected = base + markup + tax
    if close(expected, total, ctx.tolerance):
        return []

    parts = ["line items" if ctx.values["subtotal"] is None else "subtotal"]
    parts += [name for name, amount in (("markup", markup), ("tax", tax)) if amount]
    involved = ["total", "subtotal" if ctx.values["subtotal"] is not None else "line_items"]
    involved += [n for n, a in (("markup_amount", markup), ("tax_amount", tax)) if a]
    return [
        issue(
            "total_mismatch",
            f"{' + '.join(parts).capitalize()} = {money(expected)}, but the total says "
            f"{money(total)} (difference {money(total - expected)}).",
            ERROR,
            *involved,
        )
    ]


def check_markup_percent(ctx: Context) -> list[ValidationIssue]:
    """'Markup 10%  $335.00' should be 10% of the subtotal."""
    markup_field = ctx.fields.markup_amount
    base = ctx.values["subtotal"] if ctx.values["subtotal"] is not None else items_sum(ctx)
    if markup_field.value is None or base is None or not markup_field.evidence:
        return []
    match = PERCENT.search(markup_field.evidence)
    if not match:
        return []

    rate = float(match.group(1)) / 100
    expected = round(base * rate, 2)
    if close(expected, markup_field.value, ctx.tolerance):
        return []
    return [
        issue(
            "markup_percent_mismatch",
            f"Markup of {match.group(0)} on {money(base)} should be {money(expected)}, "
            f"but it says {money(markup_field.value)}.",
            WARNING,
            "markup_amount",
        )
    ]


def check_line_item_math(ctx: Context) -> list[ValidationIssue]:
    """quantity x unit price should equal the line amount."""
    issues = []
    for item in ctx.values["line_items"] or []:
        if item.quantity is None or item.unit_price is None:
            continue
        expected = round(item.quantity * item.unit_price, 2)
        if not close(expected, item.amount, ctx.tolerance):
            issues.append(
                issue(
                    "line_item_math",
                    f"'{item.description}': {item.quantity:g} x {money(item.unit_price)} = "
                    f"{money(expected)}, but the line says {money(item.amount)}.",
                    WARNING,
                    "line_items",
                )
            )
    return issues


# ------------------------------------------------------------------ dates


def check_dates(ctx: Context) -> list[ValidationIssue]:
    issues = []
    issued, approved = ctx.values["date"], ctx.values["approval_date"]

    if issued and approved and approved < issued:
        issues.append(
            issue(
                "approval_before_date",
                f"Approved on {approved.isoformat()}, before the change order date "
                f"{issued.isoformat()}.",
                ERROR,
                "date",
                "approval_date",
            )
        )
    for name in ("date", "approval_date"):
        value = ctx.values[name]
        if value and value > ctx.today:
            issues.append(
                issue(
                    f"{name}_in_future",
                    f"{name.replace('_', ' ').capitalize()} {value.isoformat()} is in the future.",
                    WARNING,
                    name,
                )
            )
    return issues


def change_order_column_dates(document: Document) -> set[dt.date]:
    """Dates written in a form's 'CHANGE ORDER (INFORMATION)' header column."""
    return {
        parsed.value
        for page in document.main_pages
        for block in page.columns
        if "CHANGE ORDER" in block.label
        for line in block.lines
        for parsed in dates_on_line(line)
    }


def issue_labelled_dates(document: Document) -> set[dt.date]:
    """Dates written after an issue-date label ('Date of Issuance: 11/19/2024')."""
    return {
        parsed.value
        for line in document.main_text.splitlines()
        if (parsed := issue_date(line)) is not None
    }


def check_date_ambiguity(ctx: Context) -> list[ValidationIssue]:
    """Several different 'Date:' values on the change order (e.g. a contract date next
    to the change order date): the extracted date may be the wrong one, even when both
    extractors agree, because they read the same misleading layout. Not raised when
    the date is the one in the form's CHANGE ORDER column, or the one labelled as the
    issue date (the layout or the label settles it)."""
    if ctx.document is None or ctx.values["date"] is None:
        return []
    if ctx.values["date"] in change_order_column_dates(ctx.document):
        return []  # the form's CHANGE ORDER column states this date: the layout settles it
    if ctx.values["date"] in issue_labelled_dates(ctx.document):
        return []  # labelled as the issue date ('Date of Issuance:'): the label settles it
    labelled = {
        parsed.value
        for line in ctx.document.main_text.splitlines()
        if not SIGNATURE_LINE.search(line)
        for parsed in dates_on_line(line)
    }
    if len(labelled) < 2:
        return []
    listed = ", ".join(sorted(day.isoformat() for day in labelled))
    return [
        issue(
            "ambiguous_date",
            f"The change order shows several dates labelled 'Date:' ({listed}); "
            f"confirm that {ctx.values['date'].isoformat()} is the change order's date.",
            WARNING,
            "date",
        )
    ]


# ------------------------------------------------------------------ consistency


def check_approval(ctx: Context) -> list[ValidationIssue]:
    status = ctx.values["status"]
    approver = ctx.values["approved_by"] or ctx.values["approval_date"]
    involved = ["status", "approved_by", "approval_date"]

    if status == ChangeOrderStatus.APPROVED and not ctx.values["approved_by"]:
        return [
            issue("approval_incomplete", "Marked approved, but no approver.", WARNING, *involved)
        ]
    if approver and status in (ChangeOrderStatus.REJECTED, ChangeOrderStatus.PENDING):
        return [
            issue(
                "status_conflict",
                f"Status is {status.value}, but approval details are filled in.",
                WARNING,
                *involved,
            )
        ]
    return []


def check_owner_signature(ctx: Context) -> list[ValidationIssue]:
    """An approval must match the OWNER column of the form's signature area: a printed
    name with a blank date does not show the owner signed, and a date found elsewhere
    (e.g. under the architect's signature) is not the owner's approval date."""
    owner = owner_signature(ctx.document) if ctx.document is not None else None
    if owner is None:
        return []
    status, approval_date = ctx.values["status"], ctx.values["approval_date"]
    owner_date = parse_date(owner.date) if owner.date else None
    involved = ["status", "approved_by", "approval_date"]

    if owner_date is None and (status == ChangeOrderStatus.APPROVED or approval_date):
        return [
            issue(
                "approval_not_confirmed",
                "The owner's signature date on the form is blank, so the approval can't "
                "be confirmed from the document.",
                WARNING,
                *involved,
            )
        ]
    if owner_date is not None and approval_date and approval_date != owner_date.value:
        return [
            issue(
                "approval_not_confirmed",
                f"Approval date {approval_date.isoformat()} differs from the owner's "
                f"signature date on the form ({owner_date.value.isoformat()}).",
                WARNING,
                *involved,
            )
        ]
    return []


def check_schedule(ctx: Context) -> list[ValidationIssue]:
    days = ctx.values["schedule_impact_days"]
    if days is None:
        return []
    if days < 0:
        return [
            issue(
                "schedule_reduction",
                f"Schedule is reduced by {-days} days; please confirm.",
                WARNING,
                "schedule_impact_days",
            )
        ]
    if days > MAX_SCHEDULE_DAYS:
        return [
            issue(
                "schedule_unusually_long",
                f"Schedule impact of {days} days is unusually long.",
                WARNING,
                "schedule_impact_days",
            )
        ]
    return []


# ------------------------------------------------------------------ evidence


def squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def evidence_pieces(evidence: str) -> list[str]:
    """Multi-line evidence is checked line by line; table rows ('a | b') cell by cell."""
    pieces = []
    for line in evidence.splitlines():
        cells = line.split(" | ") if " | " in line else [line]
        pieces += [squash(cell) for cell in cells if cell.strip()]
    return pieces


def coverage(piece: str, text: str) -> float:
    """How much of `piece` appears, in order, in the part of `text` it best matches.

    1.0 = exact quote. A quote with one word dropped ('TOTAL CHANGE ORDER $3,685.00'
    for 'TOTAL CHANGE ORDER AMOUNT $3,685.00') still scores high; an invented quote
    shares only scattered characters and scores low.
    """
    if piece in text:
        return 1.0
    anchor = SequenceMatcher(None, text, piece, autojunk=False).find_longest_match(
        0, len(text), 0, len(piece)
    )
    start = max(0, anchor.a - anchor.b - len(piece) // 2)
    window = text[start : anchor.a + 2 * len(piece)]
    blocks = SequenceMatcher(None, window, piece, autojunk=False).get_matching_blocks()
    return min(sum(block.size for block in blocks) / len(piece), 0.99)


def check_evidence(ctx: Context) -> list[ValidationIssue]:
    """Anti-hallucination: every value's evidence must really be in the document."""
    document = ctx.document
    if document is None:
        return []

    text = squash(document.text)
    issues = []
    unverifiable: list[str] = []

    for name in type(ctx.fields).model_fields:
        extracted = getattr(ctx.fields, name)
        if extracted.value is None:
            continue
        if not extracted.evidence:
            issues.append(
                issue("evidence_missing", f"No evidence quote for {name}.", WARNING, name)
            )
            continue

        pieces = evidence_pieces(extracted.evidence)
        if document.needs_vision:
            # Partly or fully scanned: a quote found on a text page is verified; one that
            # is not may come from a page image, so it can't be checked either way.
            if not (text and all(coverage(piece, text) == 1.0 for piece in pieces)):
                unverifiable.append(name)
            continue
        worst = min(coverage(piece, text) for piece in pieces)
        # Wording may be slightly off, but numbers must be exact: '$3,865.00' for
        # '$3,685.00' looks 93% similar and is completely wrong.
        missing_numbers = [n for p in pieces for n in NUMBER.findall(p) if n not in text]
        if worst < NEAR_MATCH or missing_numbers:
            issues.append(
                issue(
                    "evidence_not_found",
                    f"The quote for {name} does not appear in the document; the value may "
                    f"be made up: {extracted.evidence!r}",
                    ERROR,
                    name,
                )
            )
        elif worst < 1.0:
            issues.append(
                issue(
                    "evidence_inexact",
                    f"The quote for {name} only roughly matches the document.",
                    WARNING,
                    name,
                )
            )

    if unverifiable:  # only when some value really depends on a scanned page
        issues.append(
            issue(
                "evidence_unverifiable",
                f"The quotes for {', '.join(unverifiable)} are not on any text page; they "
                f"may come from scanned pages, so they could not be checked.",
                WARNING,
            )
        )
    return issues


def check_value_in_evidence(ctx: Context) -> list[ValidationIssue]:
    """The quote should actually contain the value (e.g. total 3685 <- 'TOTAL $3,685.00').

    A credit written in words ('Decrease of this Change Order: $14,000.00') contains
    the negative value: the word gives the sign, not the digits.
    """
    issues = []
    for name in ("subtotal", "markup_amount", "tax_amount", "total"):
        extracted = getattr(ctx.fields, name)
        if extracted.value is None or not extracted.evidence:
            continue
        amounts = [token.parsed.value for token in money_tokens(extracted.evidence)]
        if extracted.value < 0 and CREDIT_WORDS.search(extracted.evidence):
            amounts += [-abs(amount) for amount in amounts]
        if amounts and not any(close(a, extracted.value, ctx.tolerance) for a in amounts):
            issues.append(value_mismatch(name, extracted.value, extracted.evidence))

    for name, parser in (
        ("date", parse_date),
        ("approval_date", parse_date),
        ("schedule_impact_days", parse_days),
    ):
        extracted = getattr(ctx.fields, name)
        if extracted.value is None or not extracted.evidence:
            continue
        parsed = parser(extracted.evidence)
        if parsed is not None and parsed.value != extracted.value:
            issues.append(value_mismatch(name, extracted.value, extracted.evidence))
    return issues


def value_mismatch(name: str, value: object, evidence: str) -> ValidationIssue:
    return issue(
        "value_not_in_evidence",
        f"{name} is {value}, but its quote says {evidence!r}.",
        WARNING,
        name,
    )


# ------------------------------------------------------------------ entry point

CHECKS: list[Check] = [
    check_required,
    check_subtotal,
    check_total,
    check_markup_percent,
    check_line_item_math,
    check_dates,
    check_date_ambiguity,
    check_approval,
    check_owner_signature,
    check_schedule,
    check_evidence,
    check_value_in_evidence,
]


def validate(
    fields: ExtractedChangeOrder,
    document: Document | None = None,
    *,
    today: dt.date | None = None,
    tolerance: float = MONEY_TOLERANCE,
) -> ValidationReport:
    """Run every check. Pass the (cleaned) document to also verify evidence quotes."""
    ctx = Context(fields, document, today or dt.date.today(), tolerance)
    issues = [found for check in CHECKS for found in check(ctx)]
    return ValidationReport.from_issues(issues)
