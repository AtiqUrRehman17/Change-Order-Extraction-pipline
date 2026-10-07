"""Reads side-by-side labelled columns and signature areas of PDF forms by word position.

Plain text extraction flattens a form's columns into one line, so the value
under each label is lost:

    PROJECT: (Name and address)   CONTRACT INFORMATION:     CHANGE ORDER INFORMATION:
    777.01 Marshall Municipal     Contract For: General     Change Order Number: 10
    Marshall, MN                  Date: November 18, 2019   Date: November 5, 2020

pdfplumber gives every word's x position, so we can find a header row with
two or more upper-case labels ending in ':' and read the lines below it,
column by column, until a gap or the next header row.
"""

import re
from dataclasses import dataclass
from statistics import median

from pdfplumber.page import Page as PlumberPage

from change_order_extractor.normalization import parse_date, strip_form_instructions
from change_order_extractor.schemas import ColumnBlock, SignatureBlock

LINE_TOLERANCE = 2.5  # words whose tops differ less than this are on the same line
MIN_LABEL_DISTANCE = 40  # labels closer than this (points) are not separate columns
GAP_FACTOR = 1.6  # a vertical gap this many times the usual line spacing ends a block
MAX_BLOCK_LINES = 6


@dataclass
class Line:
    top: float
    words: list[dict]


@dataclass
class Label:
    text: str
    x0: float  # where the label starts (column start)
    x1: float  # where the label ends (value may follow on the same line)


def group_lines(words: list[dict]) -> list[Line]:
    lines: list[Line] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and word["top"] - lines[-1].top <= LINE_TOLERANCE:
            lines[-1].words.append(word)
        else:
            lines.append(Line(word["top"], [word]))
    for line in lines:
        line.words.sort(key=lambda w: w["x0"])
    return lines


def is_label_word(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def find_labels(line: Line) -> list[Label]:
    """Runs of upper-case words ending with ':' - 'OWNER:', 'CHANGE ORDER INFORMATION:'."""
    labels: list[Label] = []
    run: list[dict] = []
    for word in line.words:
        if not is_label_word(word["text"]):
            run = []
            continue
        run.append(word)
        if word["text"].endswith(":"):
            text = " ".join(w["text"] for w in run).rstrip(":").strip()
            if not labels or run[0]["x0"] - labels[-1].x0 >= MIN_LABEL_DISTANCE:
                labels.append(Label(text, run[0]["x0"], word["x1"]))
            run = []
    return labels


def words_between(line: Line, start: float, end: float) -> str:
    return " ".join(w["text"] for w in line.words if start - 1 <= w["x0"] < end - 1)


def read_columns(page: PlumberPage) -> list[ColumnBlock]:
    """Every labelled column block on the page (empty for ordinary layouts)."""
    lines = group_lines(page.extract_words())
    if len(lines) < 2:
        return []
    spacing = median(b.top - a.top for a, b in zip(lines, lines[1:], strict=False))

    blocks: list[ColumnBlock] = []
    index = 0
    while index < len(lines):
        labels = find_labels(lines[index])
        if len(labels) < 2:
            index += 1
            continue

        ends = [label.x0 for label in labels[1:]] + [float("inf")]
        collected: list[list[str]] = []
        # Text on the header line itself, after each label ('PROJECT: Elm Street ...').
        for label, end in zip(labels, ends, strict=True):
            rest = strip_form_instructions(words_between(lines[index], label.x1 + 1, end))
            collected.append([rest] if rest else [])

        previous = lines[index]
        next_index = index + 1
        while next_index < len(lines) and next_index - index <= MAX_BLOCK_LINES:
            line = lines[next_index]
            if line.top - previous.top > GAP_FACTOR * spacing or len(find_labels(line)) >= 2:
                break
            for column, (label, end) in enumerate(zip(labels, ends, strict=True)):
                text = strip_form_instructions(words_between(line, label.x0, end))
                if text:
                    collected[column].append(text)
            previous = line
            next_index += 1

        blocks += [
            ColumnBlock(label=label.text, lines=column_lines)
            for label, column_lines in zip(labels, collected, strict=True)
            if column_lines
        ]
        index = next_index
    return blocks


# ------------------------------------------------------------------ signature areas

ROLES = ("OWNER", "ARCHITECT", "CONTRACTOR", "ENGINEER")
# Printed captions under (or before) the values in a signature column.
CAPTIONS = {
    "SIGNATURE",
    "PRINTED NAME AND TITLE",
    "PRINTED NAME",
    "NAME AND TITLE",
    "NAME",
    "TITLE",
    "DATE",
    "BY",
}
SIGNATURE_GAP_FACTOR = 3.5  # signature areas are airy: allow bigger gaps than headers
MAX_SIGNATURE_LINES = 10
BY_PREFIX = re.compile(r"^\s*by\s*:?\s*", re.IGNORECASE)
TRAILING_DATE = re.compile(r"\s+\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*$")


def find_roles(line: Line) -> list[Label]:
    """Two or more role words without a colon: 'ARCHITECT ... CONTRACTOR ... OWNER'."""
    roles: list[Label] = []
    for word in line.words:
        if word["text"] in ROLES and (not roles or word["x0"] - roles[-1].x0 >= MIN_LABEL_DISTANCE):
            roles.append(Label(word["text"], word["x0"], word["x1"]))
    return roles if len(roles) >= 2 else []


def parse_signature_column(role: str, firm: str | None, texts: list[str]) -> SignatureBlock:
    name = date = None
    seen_caption = False
    for text in texts:
        caption = text.strip().upper().strip("()")
        if caption in CAPTIONS:
            seen_caption = True
            continue
        if by := BY_PREFIX.match(text):  # 'By: A. Feld  9/9/24'
            rest = text[by.end() :]
            if parse_date(rest) and (trailing := TRAILING_DATE.search(rest)):
                date = date or trailing.group().strip()
                rest = rest[: trailing.start()]
            name = name or (rest.strip(" _") or None)
            continue
        if len(text) <= 25 and parse_date(text):  # a date on its own line
            date = date or text.strip()
            continue
        if firm is None and name is None and not seen_caption:
            firm = text  # 'By:' layout: the firm comes right under the role
            continue
        name = name or text  # later lines continue the title ('Representative')
    return SignatureBlock(role=role, firm=firm, name=name, date=date)


def read_signatures(page: PlumberPage) -> list[SignatureBlock]:
    """Every signature-area column on the page (empty if the page has none)."""
    lines = group_lines(page.extract_words())
    if len(lines) < 2:
        return []
    spacing = median(b.top - a.top for a, b in zip(lines, lines[1:], strict=False))

    blocks: list[SignatureBlock] = []
    for index, line in enumerate(lines):
        roles = find_roles(line)
        if not roles:
            continue
        ends = [role.x0 for role in roles[1:]] + [float("inf")]
        spans = list(zip(roles, ends, strict=True))

        above = lines[index - 1] if index else None
        firms = [None] * len(roles)
        if above and line.top - above.top <= GAP_FACTOR * spacing:
            firms = [
                strip_form_instructions(words_between(above, role.x0, end)) or None
                for role, end in spans
            ]

        texts: list[list[str]] = [[] for _ in roles]
        previous = line
        for following in lines[index + 1 : index + 1 + MAX_SIGNATURE_LINES]:
            if following.top - previous.top > SIGNATURE_GAP_FACTOR * spacing or find_roles(
                following
            ):
                break
            for column, (role, end) in enumerate(spans):
                if text := words_between(following, role.x0, end).strip():
                    texts[column].append(text)
            previous = following

        blocks += [
            parse_signature_column(role.text, firm, column_texts)
            for role, firm, column_texts in zip(roles, firms, texts, strict=True)
        ]
    return blocks
