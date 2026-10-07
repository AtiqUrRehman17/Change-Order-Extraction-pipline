"""Money lines for the rule-based extractor: line items and summary amounts.

A "money line" ends with an amount:   'Labor  $1,200.00'
A summary line has a known label:     'Subtotal', 'Markup 10%', 'Sales Tax', 'TOTAL'
Everything else that ends with money is a line item.

Real PDF tables (from pdfplumber) are read column by column, which is more
reliable than guessing from text, so they are used first when present.
"""

import re
from dataclasses import dataclass

from change_order_extractor.normalization import Normalized, fix_ocr_dollar, parse_money
from change_order_extractor.schemas import LineItem, LineItemCategory, Page

# ------------------------------------------------------------------ money tokens

# One amount, allowing OCR look-alikes (O/o/l/I) inside the digits:
#   $1,200.00   (8,400.00)   ($8,400.00)   -$500   2,915.OO   1,350
MONEY_TOKEN = re.compile(
    r"(?<![\w/.\-])"  # not glued to a word, date or id
    r"\(?-?\$?-?\(?"
    r"\d[\dOolI]*(?:,[\dOolI]{3})*(?:\.[\dOolI]{1,2})?"
    r"\)?"
    r"(?![\w%/]|[.,]\d)"  # not a percentage, date or longer number
)


@dataclass(frozen=True)
class MoneyToken:
    start: int
    end: int
    text: str
    parsed: Normalized[float]


def money_tokens(line: str) -> list[MoneyToken]:
    """Amounts in a line. Plain integers ('2291', '12') are ignored: a money
    amount needs a '$', a thousands comma or cents. OCR's 'S5,000.00' counts as
    '$5,000.00' (same length, so token positions still match the line)."""
    tokens = []
    scanned, _ = fix_ocr_dollar(line)
    for match in MONEY_TOKEN.finditer(scanned):
        text = match.group()
        if not any(mark in text for mark in "$,.") or text.endswith("."):
            continue
        parsed = parse_money(text)
        if parsed is not None:
            tokens.append(MoneyToken(match.start(), match.end(), text, parsed))
    return tokens


# ------------------------------------------------------------------ line classification

SUBTOTAL = re.compile(r"\bsub\s*-?\s*t[o0]tal\b", re.I)
TAX = re.compile(r"\btax\b", re.I)
MARKUP = re.compile(
    r"\bmark\s*-?\s*up\b|\boverhead\s*(?:&|and)\s*profit\b|\bOH\s*&\s*P\b|^fee\b|\bprofit\b", re.I
)
TOTAL = re.compile(r"\b(?:grand\s+)?t[o0]tal\b", re.I)
TOTAL_ALT = re.compile(
    r"\bnet\s+credit\b|\bnet\s+(?:add|change)\b(?!\s+by\s+previously)|\blump\s+sum\b|^price\s*:",
    re.I,
)
# AIA G701 forms state the CO amount in a sentence, among other contract sums.
AIA_TOTAL = re.compile(
    r"(?P<dir>increased|decreased)\s+by\s+this\s+change\s+order\s+in\s+the\s+amount\s+of\W*",
    re.I,
)
# EJCDC C-941 forms: '[Increase] [Decrease] of this Change Order:' starts the left
# (contract price) column; the amount is the first thing on one of the next lines,
# because the contract-times column sits next to it.
EJCDC_TOTAL = re.compile(r"^\s*(?P<dir>increase|decrease)\s+of\s+this\s+change\s+order\s*:", re.I)
EJCDC_AMOUNT_LINES = 3
# Lines that end with money but are not line items.
NOT_AN_ITEM = re.compile(
    r"contract\s+sum|previously|amount\s+of|original|\bpage\b|\bdate\b|\bjob\b|\bproject\b",
    re.I,
)

BULLET = re.compile(r"^(?:[-*•]\s*|\d{1,2}[.)]\s+)")
QUANTITY = re.compile(
    r"(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]{1,5})\.?\s*@\s*\$?(?P<price>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*/\s*[A-Za-z]+)?"
)

CATEGORY_KEYWORDS: list[tuple[LineItemCategory, re.Pattern[str]]] = [
    (LineItemCategory.SUBCONTRACT, re.compile(r"\bsub(?:contract(?:or)?)?\b", re.I)),
    (
        LineItemCategory.EQUIPMENT,
        re.compile(r"equipment|rental|crane|lift|excavator|dumpster|\bequip\b", re.I),
    ),
    (LineItemCategory.LABOR, re.compile(r"\blab[o0]u?r\b|\blab\b|\binstall", re.I)),
    (LineItemCategory.MATERIAL, re.compile(r"\bmat(?:erial)?[s1l]?\b|materia[l1]s?", re.I)),
]


def categorize(description: str) -> LineItemCategory:
    for category, pattern in CATEGORY_KEYWORDS:
        if pattern.search(description):
            return category
    return LineItemCategory.OTHER


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class Found:
    """A value the rules found, with the exact text it came from."""

    value: object
    evidence: str
    notes: tuple[str, ...] = ()


@dataclass
class MoneySection:
    line_items: list[LineItem]
    line_item_evidence: list[str]
    subtotal: Found | None = None
    markup_amount: Found | None = None
    tax_amount: Found | None = None
    total: Found | None = None


def amount_after(line: str, position: int) -> MoneyToken | None:
    for token in money_tokens(line):
        if token.start >= position:
            return token
    return None


def extract_money(text: str, pages: list[Page]) -> MoneySection:
    items, evidence = items_from_tables(pages)
    table_descriptions = [item.description.lower() for item in items]
    section = MoneySection(line_items=items, line_item_evidence=evidence)

    for line in text.splitlines():
        if summary := classify_summary(line):
            name, found = summary
            if getattr(section, name) is None:
                setattr(section, name, found)
            continue

        if any(desc in line.lower() for desc in table_descriptions):
            continue  # already read from the table
        if parsed := parse_item_line(line):
            section.line_items.append(parsed)
            section.line_item_evidence.append(line)

    # On AIA-style forms the sentence "...increased by this Change Order in the amount
    # of $X" IS the change order total. It wins over any 'TOTAL:' line, which may belong
    # to a cost breakdown rather than to the change order itself.
    if aia_total := find_aia_total(text):
        section.total = aia_total
    elif ejcdc_total := find_ejcdc_total(text):
        section.total = ejcdc_total

    return section


def find_ejcdc_total(text: str) -> Found | None:
    """'Decrease of this Change Order:' ... '$14,000.00' (EJCDC) -> -14,000."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = EJCDC_TOTAL.match(line)
        if match is None:
            continue
        for amount_line in lines[index + 1 : index + 1 + EJCDC_AMOUNT_LINES]:
            tokens = money_tokens(amount_line)
            if tokens and not amount_line[: tokens[0].start].strip(" $"):
                value = abs(tokens[0].parsed.value)
                if match["dir"].lower() == "decrease":
                    value = -value
                evidence = f"{line.strip()}\n{amount_line.strip()}"
                return Found(value, evidence, tokens[0].parsed.notes)
        return None
    return None


def find_aia_total(text: str) -> Found | None:
    match = AIA_TOTAL.search(text)
    if match is None:
        return None
    token = amount_after(text[match.end() :], 0)
    if token is None:
        return None
    value = token.parsed.value
    if match["dir"].lower() == "decreased":
        value = -abs(value)
    evidence = text[match.start() : match.end() + token.end].strip()
    return Found(value, evidence, token.parsed.notes)


def classify_summary(line: str) -> tuple[str, Found] | None:
    """('subtotal' | 'markup_amount' | 'tax_amount' | 'total', Found) or None."""
    stripped = BULLET.sub("", line.strip())
    checks = [
        ("subtotal", SUBTOTAL),
        ("tax_amount", TAX),
        ("markup_amount", MARKUP),
        ("total", TOTAL),
        ("total", TOTAL_ALT),
    ]
    for name, pattern in checks:
        match = pattern.search(stripped)
        if match is None:
            continue
        token = amount_after(stripped, match.end())
        if token is None:
            return None
        return name, Found(token.parsed.value, line.strip(), token.parsed.notes)
    return None


def parse_item_line(line: str) -> LineItem | None:
    """'Labor  $1,200.00'  /  '1. Acoustic panels 48 EA @ $145.00 $6,960.00'."""
    tokens = money_tokens(line)
    if not tokens:
        return None
    last = tokens[-1]
    if line[last.end :].strip(" .") != "":
        return None  # amount must end the line

    description = line[: last.start].strip()
    if NOT_AN_ITEM.search(description):
        return None

    quantity = unit = unit_price = None
    if qty := QUANTITY.search(description):
        quantity = float(qty["qty"])
        unit = qty["unit"]
        unit_price = float(qty["price"].replace(",", ""))
        description = description[: qty.start()]

    description = BULLET.sub("", description).strip(" :=@$-")
    if sum(c.isalpha() for c in description) < 2:
        return None

    return LineItem(
        description=description,
        category=categorize(description),
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
        amount=last.parsed.value,
    )


# ------------------------------------------------------------------ tables

HEADER_ALIASES = {
    "description": ("description", "item description", "desc", "work description", "scope"),
    "amount": ("amount", "total", "extended", "ext. price", "line total", "cost"),
    "quantity": ("qty", "quantity"),
    "unit": ("unit", "uom", "units"),
    "unit_price": ("unit price", "rate", "price", "unit cost"),
}


def items_from_tables(pages: list[Page]) -> tuple[list[LineItem], list[str]]:
    items: list[LineItem] = []
    evidence: list[str] = []
    for page in pages:
        for table in page.tables:
            if len(table) < 2:
                continue
            columns = map_columns(table[0])
            if "description" not in columns or "amount" not in columns:
                continue
            for row in table[1:]:
                if item := item_from_row(row, columns):
                    items.append(item)
                    evidence.append(" | ".join(cell or "" for cell in row))
    return items, evidence


def map_columns(header: list[str | None]) -> dict[str, int]:
    names = [(cell or "").strip().lower() for cell in header]
    columns: dict[str, int] = {}
    for field_name, aliases in HEADER_ALIASES.items():
        for index, name in enumerate(names):
            if name in aliases and index not in columns.values():
                columns[field_name] = index
                break
    return columns


def item_from_row(row: list[str | None], columns: dict[str, int]) -> LineItem | None:
    def cell(name: str) -> str | None:
        index = columns.get(name)
        return row[index] if index is not None and index < len(row) else None

    description = (cell("description") or "").strip()
    amount = parse_money(cell("amount"))
    if not description or amount is None:
        return None

    quantity = parse_money(cell("quantity"))
    unit_price = parse_money(cell("unit_price"))
    return LineItem(
        description=description,
        category=categorize(description),
        quantity=quantity.value if quantity else None,
        unit=(cell("unit") or "").strip() or None,
        unit_price=unit_price.value if unit_price else None,
        amount=amount.value,
    )
