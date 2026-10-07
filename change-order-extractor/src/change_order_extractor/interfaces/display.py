"""Rich terminal output for extraction results."""

import datetime as dt
from enum import Enum
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from change_order_extractor.schemas import (
    ExtractedChangeOrder,
    ExtractionResult,
    FieldSource,
    Severity,
)

MONEY_FIELDS = {"subtotal", "markup_amount", "tax_amount", "total"}
HIGH, MEDIUM = 0.9, 0.75
EVIDENCE_WIDTH = 48


# ------------------------------------------------------------------ formatting


def money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def format_value(name: str, value: Any) -> str:
    if value is None:
        return "-"
    if name in MONEY_FIELDS:
        return money(value)
    if name == "line_items":
        return f"{len(value)} items ({money(sum(item.amount for item in value))})"
    if name == "schedule_impact_days":
        return f"{value} day" + ("" if abs(value) == 1 else "s")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def confidence_text(confidence: float, present: bool = True) -> Text:
    if not present:
        return Text("-", style="dim")
    style = "green" if confidence >= HIGH else "yellow" if confidence >= MEDIUM else "red"
    return Text(f"{confidence:.2f}", style=style)


def shorten(text: str | None, width: int = EVIDENCE_WIDTH) -> str:
    if not text:
        return ""
    text = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    return text if len(text) <= width else text[: width - 3] + "..."


def label(name: str) -> str:
    return name.replace("_", " ").replace(" days", "").capitalize()


def evidence_text(evidence: str | None, page: int | None) -> str:
    """'(p.2) TOTAL: $11,129.00' - the page tells a reviewer where to look."""
    text = shorten(evidence, EVIDENCE_WIDTH - (6 if page else 0))
    return f"(p.{page}) {text}" if page and text else text


def page_range(pages: list[int]) -> str:
    """[2, 3, 4, 7] -> '2-4, 7'."""
    parts, start = [], pages[0]
    for previous, current in zip(pages, [*pages[1:], None], strict=True):
        if current != previous + 1:
            parts.append(str(start) if start == previous else f"{start}-{previous}")
            start = current
    return ", ".join(parts)


# ------------------------------------------------------------------ single result


def fields_table(fields: ExtractedChangeOrder) -> Table:
    table = Table(title="Extracted fields", title_justify="left", expand=True)
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold")
    table.add_column("Conf.", justify="right", no_wrap=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Evidence", style="dim", overflow="fold")

    for name in ExtractedChangeOrder.model_fields:
        field = getattr(fields, name)
        present = field.value is not None
        source = field.source.value if field.source != FieldSource.NONE else ""
        if field.source == FieldSource.CONFLICT:
            source = Text(source, style="bold red")
        table.add_row(
            label(name),
            format_value(name, field.value) if present else Text("-", style="dim"),
            confidence_text(field.confidence, present),
            source,
            evidence_text(field.evidence, field.page),
        )
    return table


def line_items_table(fields: ExtractedChangeOrder) -> Table | None:
    items = fields.line_items.value
    if not items:
        return None
    table = Table(title="Line items", title_justify="left", expand=True)
    table.add_column("Description", overflow="fold")
    table.add_column("Category")
    table.add_column("Qty", justify="right")
    table.add_column("Unit price", justify="right")
    table.add_column("Amount", justify="right")
    for item in items:
        quantity = f"{item.quantity:g} {item.unit or ''}".strip() if item.quantity else ""
        table.add_row(
            item.description,
            item.category.value,
            quantity,
            money(item.unit_price) if item.unit_price is not None else "",
            money(item.amount),
        )
    return table


def issues_table(result: ExtractionResult) -> Table | None:
    if not result.validation.issues:
        return None
    table = Table(title="Validation", title_justify="left", expand=True, show_header=False)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Issue", overflow="fold")
    for issue in result.validation.issues:
        style = "red" if issue.severity == Severity.ERROR else "yellow"
        table.add_row(Text(issue.severity.value.upper(), style=style), issue.message)
    return table


def summary_panel(result: ExtractionResult) -> Panel:
    if result.needs_review:
        status = Text("NEEDS REVIEW", style="bold red")
        border = "red"
    else:
        status = Text("OK", style="bold green")
        border = "green"

    lines = [
        Text.assemble("Status: ", status),
        Text.assemble("Overall confidence: ", confidence_text(result.overall_confidence)),
        Text(
            f"Source: {result.document.source_type.value}, "
            f"{result.document.page_count or '?'} page(s)  |  "
            f"Extractors: {', '.join(result.extractors_used)}"
            + (f" ({result.model})" if result.model else "")
            + f"  |  {result.processing_seconds or 0:.2f}s"
        ),
    ]
    if attachments := result.document.attachment_pages:
        lines.append(
            Text(
                f"Attachments (backup documents, not read as the change order): "
                f"page{'s' if len(attachments) > 1 else ''} {page_range(attachments)}",
                style="dim",
            )
        )
    for reason in result.review_reasons:
        lines.append(Text(f"- {reason}", style="red"))
    for warning in result.warnings:
        lines.append(Text(f"! {warning}", style="yellow"))

    return Panel(Group(*lines), title=result.document.source_file, border_style=border)


def show_result(console: Console, result: ExtractionResult) -> None:
    console.print(summary_panel(result))
    console.print(fields_table(result.fields))
    for table in (line_items_table(result.fields), issues_table(result)):
        if table is not None:
            console.print(table)


# ------------------------------------------------------------------ many results


def batch_table(rows: list[tuple[str, ExtractionResult | Exception]]) -> Table:
    table = Table(title="Summary", title_justify="left", expand=True)
    table.add_column("File", overflow="fold")
    table.add_column("CO #")
    table.add_column("Total", justify="right")
    table.add_column("Conf.", justify="right")
    table.add_column("Status")
    table.add_column("Main reason", style="dim", overflow="fold")

    for name, outcome in rows:
        if isinstance(outcome, Exception):
            table.add_row(name, "", "", "", Text("FAILED", style="bold red"), str(outcome))
            continue
        fields = outcome.fields
        status = (
            Text("REVIEW", style="bold red") if outcome.needs_review else Text("OK", style="green")
        )
        table.add_row(
            name,
            format_value("change_order_number", fields.change_order_number.value),
            format_value("total", fields.total.value),
            confidence_text(outcome.overall_confidence),
            status,
            shorten(outcome.review_reasons[0] if outcome.review_reasons else "", 60),
        )
    return table
