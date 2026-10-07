"""ChangeOrder and LineItem models.

These describe the *clean* data for a change order: just the values, no
confidence or evidence. They are used for ground-truth files and as the
final "plain" output of the pipeline.

Every field is optional because real documents often leave things out.
Whether a missing field is a problem is decided later, in validation.
"""

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class LineItemCategory(StrEnum):
    LABOR = "labor"
    MATERIAL = "material"
    EQUIPMENT = "equipment"
    SUBCONTRACT = "subcontract"
    MARKUP = "markup"
    TAX = "tax"
    OTHER = "other"


class ChangeOrderStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class LineItem(BaseModel):
    """One cost line, e.g. 'Labor ........ $1,200.00'."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(description="What the line is for, as written.")
    category: LineItemCategory = Field(default=LineItemCategory.OTHER)
    quantity: float | None = Field(default=None, description="e.g. 2 (windows), 16 (hours).")
    unit: str | None = Field(default=None, description="e.g. 'ea', 'hr', 'sf'.")
    unit_price: float | None = None
    amount: float = Field(description="Line total. Negative for credits.")


class ChangeOrder(BaseModel):
    """All the fields we extract from one change order."""

    model_config = ConfigDict(extra="forbid")

    # Identification
    change_order_number: str | None = Field(default=None, description="e.g. 'CO-014'.")
    project_name: str | None = None
    project_number: str | None = Field(default=None, description="Job / project number.")
    date: dt.date | None = Field(default=None, description="Date the change order was issued.")

    # Parties
    contractor: str | None = Field(default=None, description="Company issuing the change order.")
    owner: str | None = Field(default=None, description="Client / owner receiving it.")

    # What changed and why
    description: str | None = Field(default=None, description="Scope of the change.")
    reason: str | None = Field(
        default=None, description="Why it changed, e.g. 'owner request', 'unforeseen condition'."
    )

    # Money
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = Field(default=None, description="Sum before markup / tax.")
    markup_amount: float | None = None
    tax_amount: float | None = None
    total: float | None = Field(default=None, description="Final amount. Negative for a credit.")

    # Schedule
    schedule_impact_days: int | None = Field(
        default=None, description="Days added to the contract time. 0 = no impact."
    )

    # Approval
    status: ChangeOrderStatus = Field(default=ChangeOrderStatus.UNKNOWN)
    requested_by: str | None = None
    approved_by: str | None = None
    approval_date: dt.date | None = None
