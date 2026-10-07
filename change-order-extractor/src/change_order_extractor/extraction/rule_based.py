"""Regex-based extractor for clear patterns (CO numbers, money, dates).

Fast, free and deterministic. It reads labelled values ('Date: ...',
'Project: ...') and money lines. It is built to be *precise* rather than
complete: when a pattern is unclear it leaves the field empty for the LLM.

Expects a cleaned Document (see preprocessing). Only the change order itself
is read: pages marked as attachments (backup memos, quotes) are skipped, since
their totals and job numbers belong to other documents. On side-by-side forms
(AIA G701) the labelled columns read by position are used first. Scanned pages
have no text, so for those the rules find nothing and the LLM's vision does
the work.
"""

import re
from typing import TypeVar

from change_order_extractor.extraction import fields
from change_order_extractor.extraction.base import Extractor
from change_order_extractor.extraction.fields import OCR_LETTERS_NOTE
from change_order_extractor.extraction.line_items import Found, extract_money, money_tokens
from change_order_extractor.normalization import (
    Normalized,
    fix_ocr_letters,
    parse_date,
    parse_days,
    strip_form_instructions,
)
from change_order_extractor.schemas import (
    ChangeOrderStatus,
    ColumnBlock,
    Document,
    ExtractedChangeOrder,
    ExtractedField,
    FieldSource,
    LineItem,
    SignatureBlock,
)

T = TypeVar("T")

# Preliminary confidence (final scores are decided in the scoring step).
LABELED = 0.85  # value next to an explicit label, e.g. 'Date: 3/7/25'
HEURISTIC = 0.55  # educated guess, e.g. 'first line looks like a company name'

# ------------------------------------------------------------------ patterns

# Where a labelled value stops: a column gap, or the start of another label.
NEXT_LABEL = re.compile(
    r"\s{2,}"
    r"|\s+(?:date(?:\s+issued)?|owner|project|pr[o0]ject|job|by|contractor|architect|reason"
    r"|description|time|phone|email|status)\s*:"
    r"|\s+(?:j[o0]b|pr[o0]j(?:ect)?)\s*(?:no\b\.?|number|#)"
    r"|\s+(?:change\s+order|co)\s*(?:no\b\.?|number|#)"
    # the next column's label on forms like EJCDC C-941: 'Contractor's Project No.:',
    # 'Owner's Contract No.:', 'Contract Name:' (a possessive word and up to two more
    # capitalized words, or one capitalized word, then No./Number/Name + ':')
    r"|\s+(?-i:[A-Z])\w*['’]s\s+(?:(?-i:[A-Z])\w*\s+){0,2}(?:no\b\.?|number|#|name)\s*:"
    r"|\s+(?-i:[A-Z])\w*\s+(?:no\b\.?|number|#|name)\s*:"
    r"|\s+\((?=[A-Z]{1,5}-)",  # '(BH-1022)' after a project name
    re.I,
)

CO_NUMBER = re.compile(
    r"\b(?:ch\s*ange\s+[o0]rder(?:\s+request)?|c[o0])\b\s*"
    r"(?:no\b\.?|number|#)?\s*[:#]?\s*"
    r"(?P<value>[A-Z]{1,4}-\d[\w-]*|\d[\w-]*)",
    re.I,
)
PROJECT_NAME = re.compile(r"\bpr[o0]ject(?:\s+name)?\s*:\s*(?P<value>.+)", re.I)
PROJECT_NUMBER = re.compile(
    r"\b(?:j[o0]b|pr[o0]j(?:ect)?)\s*(?:no\b\.?|number|#)?\s*[:#]?\s*(?P<value>[A-Z0-9][\w-]*)",
    re.I,
)
PROJECT_NUMBER_IN_PARENS = re.compile(r"\((?P<value>[A-Z]{1,5}-[\w-]*\d[\w-]*)\)")
DATE_LABEL = re.compile(r"\bdate(?:\s+issued)?\s*:\s*(?P<value>.+)", re.I)
DATE_LABEL_START = re.compile(r"\bdate(?:\s+issued)?\s*:\s*", re.I)
# 'Date of Issuance: 11/19/2024  Effective Date: 11/18/2024' (EJCDC C-941): the issue
# date is the change order's own date; the effective date next to it is not.
ISSUE_DATE = re.compile(
    r"\b(?:date\s+of\s+issu(?:e|ance)|issu(?:e|ance)\s+date|date\s+issued)\s*:\s*", re.I
)
ISSUE_DATE_END = re.compile(r"\s{2}|\b(?:effective\s+)?date\b|\b[A-Za-z][\w']*\s*:", re.I)
# '777.01 Marshall Municipal Building' -> number '777.01', name 'Marshall Municipal Building'
LEADING_PROJECT_NUMBER = re.compile(r"^(?P<number>\d[\d.\-]*[A-Z]?)\s+(?P<name>[A-Za-z].+)$")
CONTRACTOR = re.compile(r"\bcontractor\s*:\s*(?P<value>.+)", re.I)
OWNER = re.compile(r"(?:\bowner|^to)\s*:\s*(?P<value>.+)", re.I)
REASON = re.compile(r"\breason(?:\s+for\s+change)?\s*:\s*(?P<value>.+)", re.I)
REQUESTED_BY = re.compile(r"\brequested\s+by\s*:\s*(?P<value>[^,\n]+)", re.I)
DESCRIPTION = re.compile(
    r"^(?:descripti[o0]n(?:\s+of\s+(?:change|work))?|scope(?:\s+of\s+work)?|"
    r"the\s+contract\s+is\s+changed\s+as\s+follows)\s*:\s*(?P<value>.*)",
    re.I,
)
NEXT_LINE_LABEL = re.compile(r"^[A-Za-z][\w &/#.()-]{0,40}:")
TABLE_HEADER = re.compile(r"\bqty\b|\bquantity\b|\bunit\s+price\b|\bamount\b", re.I)
SCHEDULE = re.compile(
    r"\b(?:contract\s+time|time\s+extension|time\s+requested|time|schedule(?:\s+impact)?)\b",
    re.I,
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
DAYS_ANYWHERE = re.compile(r"[+-]?\d+\s*(?:calendar\s+|working\s+)?days?\b", re.I)
# Contract-time totals ('Substantial Completion: 360 calendar days'), not changes.
CONTRACT_TIME_TOTAL = re.compile(
    r"substantial\s+completion|ready\s+for\s+final\s+payment|final\s+completion", re.I
)
# EJCDC C-941: 'Decrease of this Change Order: Increase of this Change Order:' - the
# second label heads the contract-times column; its 'Substantial Completion:' line
# below holds this change order's time change ('NA' = no change).
EJCDC_TIMES_LABEL = re.compile(
    r"of\s+this\s+change\s+order\s*:.*\b(?P<dir>increase|decrease)\s+of\s+this\s+change\s+order\s*:",
    re.I,
)
EJCDC_TIMES_LINES = 3
SUBSTANTIAL_COMPLETION = re.compile(
    r"substantial\s+completion\s*:\s*(?P<value>.*?)(?=\s+ready\s+for|\s*$)", re.I
)
NO_TIME_CHANGE = re.compile(r"^(?:n/?a|none|no\s+change|-+|0)?$", re.I)
EJCDC_NO_CHANGE_NOTE = "'NA' in the change order's contract-times column read as no change"

APPROVAL = re.compile(
    r"\b(?:approved|accepted)(?:\s+by)?(?:\s+owner)?\s*:\s*"
    r"(?P<name>(?!date\b)[A-Za-z][A-Za-z.' ]*?)(?=\s{2}|\s+date\b|,|\s+\d|$)",
    re.I,
)
REJECTED = re.compile(r"\b(?:rejected|declined|denied)\b", re.I)
PENDING = re.compile(r"\bpending\b", re.I)
UNSIGNED = re.compile(
    r"\b(?:owner\s+approval|accepted\s+by|approved\s+by|by)\s*:\s*(?:date\b|$)", re.I
)
SIGNATURE_LINE = re.compile(r"\b(?:approv|accept|declin|reject)\w*|\bby\s*:", re.I)

SEVERAL_DATES_NOTE = "several dates on this line; picked the first"

# Documents a change is based on: 'RFI 24', 'RFI #27 response', 'ASI-12', 'PR04', 'CCD 3'.
# Upper-case only, so ordinary words like 'pr' or 'asi' never match.
REFERENCE = re.compile(r"\b(?:RFI|ASI|PR|PCO|CCD|COR)\s*#?\s*-?\s*\d+[A-Z]?(?:\s+response)?\b")
REFERENCE_NOTE = "reason built from referenced documents (RFI / ASI / PR numbers)"

COMPANY_HINT = re.compile(
    r"\b(?:inc|llc|ltd|co|corp|company|builders?|construction|constructors|contractors?|"
    r"group|electric|mechanical|interiors|homes|industrial)\b",
    re.I,
)


# ------------------------------------------------------------------ helpers


def cut_value(value: str) -> str:
    """Trim a labelled value at the next column gap or label.

    Printed form instructions are not values: 'OWNER: (Name and address)' gives ''.
    """
    match = NEXT_LABEL.search(value)  # before stripping: it relies on the column gap
    if match:
        value = value[: match.start()]
    return strip_form_instructions(value.strip(" :;,-"))


def make_field(
    value: T | None,
    evidence: str | None,
    confidence: float = LABELED,
    notes: tuple[str, ...] | list[str] = (),
) -> ExtractedField[T]:
    return fields.make_field(value, evidence, FieldSource.RULES, confidence, notes)


def make_text_field(
    value: str | None,
    evidence: str | None,
    confidence: float = LABELED,
    notes: tuple[str, ...] | list[str] = (),
) -> ExtractedField[str]:
    return fields.make_text_field(value, evidence, FieldSource.RULES, confidence, notes)


def from_found(found: Found | None) -> ExtractedField:
    if found is None:
        return ExtractedField()
    return make_field(found.value, found.evidence, notes=found.notes)


def fix_item_descriptions(items: list[LineItem]) -> tuple[list[LineItem], list[str]]:
    fixed_items, notes = [], []
    for item in items:
        description, fixed = fix_ocr_letters(item.description)
        if fixed:
            item = item.model_copy(update={"description": description})
            notes = [OCR_LETTERS_NOTE]
        fixed_items.append(item)
    return fixed_items, notes


def dates_on_line(line: str) -> list:
    """Every labelled date on a line: 'Date: Nov 18, 2019  Date: Nov 5, 2020' -> 2 dates."""
    labels = list(DATE_LABEL_START.finditer(line))
    if not labels:
        return []
    ends = [label.start() for label in labels[1:]] + [len(line)]
    found = []
    for label, end in zip(labels, ends, strict=True):
        if parsed := parse_date(line[label.end() : end]):
            found.append(parsed)
    return found


def issue_date(line: str) -> Normalized | None:
    """The date after an issue-date label ('Date of Issuance:', 'Issue Date:'), stopping
    at the next label so an 'Effective Date:' on the same line is not read instead."""
    match = ISSUE_DATE.search(line)
    if match is None:
        return None
    value = line[match.end() :]
    if end := ISSUE_DATE_END.search(value):
        value = value[: end.start()]
    return parse_date(value)


def column(columns: dict[str, ColumnBlock], *labels: str) -> ColumnBlock | None:
    for label in labels:
        if label in columns:
            return columns[label]
    return None


def change_order_block(columns: dict[str, ColumnBlock]) -> ColumnBlock | None:
    """The 'CHANGE ORDER INFORMATION' / 'CHANGE ORDER NUMBER' column, if any."""
    return next((block for label, block in columns.items() if "CHANGE ORDER" in label), None)


def owner_signature(document: Document) -> SignatureBlock | None:
    """The OWNER column of the change order's signature area, if the form has one."""
    for page in document.main_pages:
        for block in page.signatures:
            if block.role == "OWNER":
                return block
    return None


def person_name(printed: str) -> str:
    """'Scott VanDerMillen, Owner Representative' -> 'Scott VanDerMillen'."""
    return printed.split(",")[0].strip()


def first_labeled(pattern: re.Pattern[str], lines: list[str]) -> tuple[str, str] | None:
    """(value, evidence) for the first line where `pattern` finds a non-empty value."""
    for line in lines:
        match = pattern.search(line)
        if match and (value := cut_value(match["value"])):
            return value, line
    return None


# ------------------------------------------------------------------ extractor


class RuleBasedExtractor(Extractor):
    name = "rules"

    def extract(self, document: Document) -> ExtractedChangeOrder:
        text = document.main_text  # attachments are other documents
        all_lines = text.splitlines()
        lines = [line for line in all_lines if line.strip()]
        columns: dict[str, ColumnBlock] = {}
        for page in document.main_pages:
            for block in page.columns:
                columns.setdefault(block.label, block)

        owner = owner_signature(document)

        money = extract_money(text, document.main_pages)
        items, item_notes = fix_item_descriptions(money.line_items)
        status, approved_by, approval_date = self.approval(lines, owner)

        return ExtractedChangeOrder(
            change_order_number=self.change_order_number(lines, columns),
            project_name=self.project_name(lines, columns),
            project_number=self.project_number(lines, columns),
            date=self.date(lines, columns),
            contractor=self.contractor(lines, columns),
            owner=self.owner(lines, columns),
            description=self.description(all_lines),
            reason=self.reason(lines),
            line_items=make_field(
                items or None, "\n".join(money.line_item_evidence) or None, notes=item_notes
            ),
            subtotal=from_found(money.subtotal),
            markup_amount=from_found(money.markup_amount),
            tax_amount=from_found(money.tax_amount),
            total=from_found(money.total),
            schedule_impact_days=self.schedule(lines),
            status=status,
            requested_by=self.labeled(REQUESTED_BY, lines),
            approved_by=approved_by,
            approval_date=approval_date,
        )

    # -------------------------------------------------------------- simple labels

    def labeled(self, pattern: re.Pattern[str], lines: list[str]) -> ExtractedField[str]:
        found = first_labeled(pattern, lines)
        return make_text_field(found[0], found[1]) if found else ExtractedField[str]()

    def change_order_number(
        self, lines: list[str], columns: dict[str, ColumnBlock]
    ) -> ExtractedField[str]:
        if block := change_order_block(columns):
            for line in block.lines:
                if match := CO_NUMBER.search(line):
                    return make_field(match["value"], line)
            first = block.lines[0]
            is_number_column = re.search(r"\b(?:NUMBER|NO)\b", block.label)
            if is_number_column and re.fullmatch(r"[\w-]*\d[\w-]*", first):
                return make_field(first, first)  # 'CHANGE ORDER NUMBER: 009'
        for line in lines:
            if match := CO_NUMBER.search(line):
                return make_field(match["value"], line)
        return ExtractedField[str]()

    def project_name(
        self, lines: list[str], columns: dict[str, ColumnBlock]
    ) -> ExtractedField[str]:
        if block := column(columns, "PROJECT", "PROJECT NAME"):
            first = block.lines[0]
            match = LEADING_PROJECT_NUMBER.match(first)
            return make_text_field(match["name"] if match else first, first)
        return self.labeled(PROJECT_NAME, lines)

    def project_number(
        self, lines: list[str], columns: dict[str, ColumnBlock]
    ) -> ExtractedField[str]:
        for line in lines:
            for match in PROJECT_NUMBER.finditer(line):
                if any(c.isdigit() for c in match["value"]):
                    return make_field(match["value"], line)
            if PROJECT_NAME.search(line) and (match := PROJECT_NUMBER_IN_PARENS.search(line)):
                return make_field(match["value"], line)
        block = column(columns, "PROJECT", "PROJECT NAME")
        if block and (match := LEADING_PROJECT_NUMBER.match(block.lines[0])):
            return make_field(match["number"], block.lines[0])
        return ExtractedField[str]()

    def contractor(self, lines: list[str], columns: dict[str, ColumnBlock]) -> ExtractedField[str]:
        if block := column(columns, "CONTRACTOR"):
            return make_text_field(block.lines[0], block.lines[0])
        if found := first_labeled(CONTRACTOR, lines):
            return make_text_field(found[0], found[1])
        # Letterhead: the first line is often the contractor's company name.
        first = lines[0].strip() if lines else ""
        if COMPANY_HINT.search(first) and not CO_NUMBER.search(first):
            return make_text_field(
                first, first, HEURISTIC, ["guessed from letterhead (first line)"]
            )
        return ExtractedField[str]()

    def owner(self, lines: list[str], columns: dict[str, ColumnBlock]) -> ExtractedField[str]:
        if block := column(columns, "OWNER"):
            return make_text_field(block.lines[0], block.lines[0])
        for line in lines:
            match = OWNER.search(line)
            if not match:
                continue
            value = cut_value(match["value"])
            if value and "@" not in value and "<" not in value:
                return make_text_field(value, line)
        return ExtractedField[str]()

    # -------------------------------------------------------------- dates

    def date(self, lines: list[str], columns: dict[str, ColumnBlock]) -> ExtractedField:
        # On side-by-side forms the change order's own column holds its date; the
        # contract's column next to it has a different 'Date:'.
        for block in filter(None, [change_order_block(columns), column(columns, "DATE")]):
            for line in block.lines:
                if (match := DATE_LABEL.search(line)) and (parsed := parse_date(match["value"])):
                    return make_field(parsed.value, line, notes=parsed.notes)
            if block.label == "DATE" and (parsed := parse_date(block.lines[0])):
                return make_field(parsed.value, block.lines[0], notes=parsed.notes)

        # An explicit issue-date label beats any other 'Date:' (e.g. 'Effective Date:').
        for line in lines:
            if parsed := issue_date(line):
                return make_field(parsed.value, line, notes=parsed.notes)

        for line in lines:
            if SIGNATURE_LINE.search(line):
                continue  # 'Approved by: ...  Date: ...' is the approval date
            found = dates_on_line(line)
            if found:
                first = found[0]
                several = len({parsed.value for parsed in found}) > 1
                notes = [*first.notes, SEVERAL_DATES_NOTE] if several else list(first.notes)
                return make_field(first.value, line, notes=notes)

        # Shorthand notes: a line that is just a date, near the top.
        for line in lines[:6]:
            if len(line.strip()) <= 12 and (parsed := parse_date(line)):
                notes = ["unlabelled date line", *parsed.notes]
                return make_field(parsed.value, line, HEURISTIC, notes)
        return ExtractedField()

    # -------------------------------------------------------------- description

    def description(self, all_lines: list[str]) -> ExtractedField[str]:
        """The labelled paragraph, continuing over following lines until it ends."""
        for index, line in enumerate(all_lines):
            match = DESCRIPTION.search(line.strip())
            if not match:
                continue
            parts = [match["value"].strip()] if match["value"].strip() else []
            evidence = [line]
            for following in all_lines[index + 1 :]:
                if self.ends_description(following):
                    break
                parts.append(following.strip())
                evidence.append(following)
            text = strip_form_instructions(" ".join(parts))
            if text:
                return make_text_field(text, "\n".join(evidence))
        return ExtractedField[str]()

    @staticmethod
    def ends_description(line: str) -> bool:
        stripped = line.strip()
        return (
            not stripped  # blank line ends the paragraph
            or NEXT_LINE_LABEL.match(stripped) is not None
            or bool(money_tokens(stripped))
            or TABLE_HEADER.search(stripped) is not None
            or stripped.isupper()
        )

    # -------------------------------------------------------------- reason

    def reason(self, lines: list[str]) -> ExtractedField[str]:
        """'Reason: ...' if labelled; otherwise the documents the change refers to."""
        labelled = self.labeled(REASON, lines)
        if labelled.value is not None:
            return labelled
        references: dict[str, str] = {}  # normalized reference -> as written
        evidence: list[str] = []
        for line in lines:
            found = [match.group() for match in REFERENCE.finditer(line)]
            new = [ref for ref in found if re.sub(r"\W", "", ref) not in references]
            for ref in new:
                references[re.sub(r"\W", "", ref)] = ref
            if new:
                evidence.append(line)
        if not references:
            return ExtractedField[str]()
        return make_text_field(
            "; ".join(references.values()), "\n".join(evidence), HEURISTIC, [REFERENCE_NOTE]
        )

    # -------------------------------------------------------------- schedule

    def schedule(self, lines: list[str]) -> ExtractedField[int]:
        if found := self.ejcdc_time_change(lines):
            return found
        for line in lines:
            # Parse the whole sentence: the 'No' in 'No schedule impact' comes before the
            # label, but other sentences on the line ('Total is $2,070.') must not leak in.
            for sentence in SENTENCE_END.split(line):
                if SCHEDULE.search(sentence) and (parsed := parse_days(sentence)):
                    return make_field(parsed.value, line, notes=parsed.notes)
        for line in lines:
            if CONTRACT_TIME_TOTAL.search(line):
                continue  # the contract's total time, not this change
            if DAYS_ANYWHERE.search(line) and (parsed := parse_days(line)):
                notes = ["unlabelled schedule line", *parsed.notes]
                return make_field(parsed.value, line, HEURISTIC, notes)
        return ExtractedField[int]()

    def ejcdc_time_change(self, lines: list[str]) -> ExtractedField[int] | None:
        """The 'Substantial Completion:' line under '[Increase] [Decrease] of this Change
        Order:' in the contract-times column: 'NA' -> 0, '30 days' -> 30."""
        for index, line in enumerate(lines):
            label = EJCDC_TIMES_LABEL.search(line)
            if label is None:
                continue
            for below in lines[index + 1 : index + 1 + EJCDC_TIMES_LINES]:
                match = SUBSTANTIAL_COMPLETION.search(below)
                if match is None:
                    continue
                evidence = f"{line.strip()}\n{below.strip()}"
                value = match["value"].strip()
                if NO_TIME_CHANGE.match(value):
                    return make_field(0, evidence, notes=[EJCDC_NO_CHANGE_NOTE])
                if parsed := parse_days(value):
                    days = (
                        -abs(parsed.value) if label["dir"].lower() == "decrease" else parsed.value
                    )
                    return make_field(days, evidence, notes=parsed.notes)
                return None
            return None
        return None

    # -------------------------------------------------------------- approval

    def approval(
        self, lines: list[str], owner: SignatureBlock | None = None
    ) -> tuple[ExtractedField[ChangeOrderStatus], ExtractedField[str], ExtractedField]:
        """(status, approved_by, approval_date)."""
        status = ExtractedField[ChangeOrderStatus]()
        approved_by = ExtractedField[str]()
        approval_date = ExtractedField()

        for line in lines:
            if REJECTED.search(line):
                return make_field(ChangeOrderStatus.REJECTED, line), approved_by, approval_date

        for line in lines:
            match = APPROVAL.search(line)
            if not match:
                continue
            status = make_field(ChangeOrderStatus.APPROVED, line)
            approved_by = make_text_field(match["name"].strip(), line)
            if parsed := parse_date(line[match.end() :]):
                approval_date = make_field(parsed.value, line, notes=parsed.notes)
            return status, approved_by, approval_date

        # The form's signature area: only the OWNER's column decides approval, and a
        # printed name without a date does not show the form was signed.
        if owner is not None:
            parsed = parse_date(owner.date) if owner.date else None
            evidence = owner.name or owner.firm
            if parsed is None:
                return make_field(ChangeOrderStatus.PENDING, evidence), approved_by, approval_date
            status = make_field(ChangeOrderStatus.APPROVED, owner.date)
            if owner.name:
                approved_by = make_text_field(person_name(owner.name), owner.name)
            approval_date = make_field(parsed.value, owner.date, notes=parsed.notes)
            return status, approved_by, approval_date

        for line in lines:
            if PENDING.search(line):
                return make_field(ChangeOrderStatus.PENDING, line), approved_by, approval_date
        for line in lines:
            if UNSIGNED.search(line):
                status = make_field(
                    ChangeOrderStatus.PENDING, line, HEURISTIC, ["approval signature is blank"]
                )
                return status, approved_by, approval_date

        return status, approved_by, approval_date
