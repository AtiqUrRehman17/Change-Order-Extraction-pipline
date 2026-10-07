"""Sample change orders: the document text plus the correct answer (ground truth).

Each sample is written by hand to look like something a real contractor would
send: clean forms, tables, emails, shorthand notes, OCR noise, AIA-style forms
full of distracting numbers, credits, wrong math and missing fields.

Ground-truth rules:
  * Values are what the document *says*, normalized (dates -> ISO, money -> numbers).
    If the document's math is wrong, the ground truth keeps the wrong total; catching
    it is validation's job (listed in `expected_issues`).
  * Markup and tax go in `markup_amount` / `tax_amount`, not in `line_items`.
  * Fields the document doesn't contain are None.
"""

import datetime as dt
from dataclasses import dataclass, field
from textwrap import dedent

from change_order_extractor.schemas import (
    ChangeOrder,
    ChangeOrderStatus,
    LineItem,
    LineItemCategory,
)

LABOR = LineItemCategory.LABOR
MATERIAL = LineItemCategory.MATERIAL
EQUIPMENT = LineItemCategory.EQUIPMENT
SUBCONTRACT = LineItemCategory.SUBCONTRACT
OTHER = LineItemCategory.OTHER

APPROVED = ChangeOrderStatus.APPROVED
PENDING = ChangeOrderStatus.PENDING
REJECTED = ChangeOrderStatus.REJECTED


@dataclass
class Sample:
    id: str
    format: str  # "txt", "pdf" (text PDF) or "pdf_scan" (image-only PDF)
    style: str  # short human description
    text: str
    truth: ChangeOrder
    expected_issues: list[str] = field(default_factory=list)
    table: list[list[str]] | None = None  # for PDFs that render line items as a real table


def item(
    description: str,
    category: LineItemCategory,
    amount: float,
    quantity: float | None = None,
    unit: str | None = None,
    unit_price: float | None = None,
) -> LineItem:
    return LineItem(
        description=description,
        category=category,
        amount=amount,
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
    )


def doc(text: str) -> str:
    return dedent(text).strip("\n") + "\n"


SAMPLES: list[Sample] = [
    # ------------------------------------------------------------------ 001
    Sample(
        id="co_001_clean",
        format="txt",
        style="Clean, well-labelled form",
        text=doc("""
            ACME BUILDERS INC.
            CHANGE  ORDER #   CO-014
            Project: Riverside Medical Clinic     Job No. 2291
            Date: Mar 5th, 2025

            To: Greenfield Properties LLC

            Reason: Owner request
            Description of change:
            Owner requested 2 additional windows on north wall (2nd floor).
            Includes framing, glazing & trim.

              Labor ........ $1,200.00
              Materials .... $2,150.00
              Subtotal ..... $3,350.00
              Markup 10% ... $335.00
              TOTAL ........ $3,685.00

            Contract time extended by: five (5) working days

            Approved by: __J. Martinez____   Date: 3/7/25
        """),
        truth=ChangeOrder(
            change_order_number="CO-014",
            project_name="Riverside Medical Clinic",
            project_number="2291",
            date=dt.date(2025, 3, 5),
            contractor="ACME Builders Inc.",
            owner="Greenfield Properties LLC",
            description=(
                "Owner requested 2 additional windows on north wall (2nd floor). "
                "Includes framing, glazing & trim."
            ),
            reason="Owner request",
            line_items=[item("Labor", LABOR, 1200.00), item("Materials", MATERIAL, 2150.00)],
            subtotal=3350.00,
            markup_amount=335.00,
            total=3685.00,
            schedule_impact_days=5,
            status=APPROVED,
            approved_by="J. Martinez",
            approval_date=dt.date(2025, 3, 7),
        ),
    ),
    # ------------------------------------------------------------------ 002
    Sample(
        id="co_002_table",
        format="pdf",
        style="Formal PDF form with a line-item table (qty / unit / unit price)",
        text=doc("""
            SUMMIT RIDGE CONSTRUCTION LLC
            1450 Industrial Pkwy, Maple Falls, OR 97001

            CHANGE ORDER

            Change Order No.: 7                     Date: 08/14/2024
            Project: Maple Falls Public Library Renovation
            Project No.: MF-2024-017
            Owner: City of Maple Falls

            Reason for Change: Unforeseen condition
            Description: Replace damaged subfloor discovered during demolition
            in the east reading room.

            {{TABLE}}

                                              Subtotal:      $4,672.00
                                     Overhead & Profit 15%:    $700.80
                                      TOTAL CHANGE ORDER:    $5,372.80

            Contract Time Adjustment: 3 calendar days

            Requested by: R. Okafor, Project Manager

            Owner Approval: ______________________   Date: __________
        """),
        table=[
            ["Item", "Description", "Qty", "Unit", "Unit Price", "Amount"],
            ["1", "Remove damaged subfloor", "450", "SF", "$2.50", "$1,125.00"],
            ["2", '3/4" plywood subfloor', "450", "SF", "$3.20", "$1,440.00"],
            ["3", "Install subfloor - labor", "24", "HR", "$68.00", "$1,632.00"],
            ["4", "Dumpster rental", "1", "EA", "$475.00", "$475.00"],
        ],
        truth=ChangeOrder(
            change_order_number="7",
            project_name="Maple Falls Public Library Renovation",
            project_number="MF-2024-017",
            date=dt.date(2024, 8, 14),
            contractor="Summit Ridge Construction LLC",
            owner="City of Maple Falls",
            description=(
                "Replace damaged subfloor discovered during demolition in the east reading room."
            ),
            reason="Unforeseen condition",
            line_items=[
                item("Remove damaged subfloor", LABOR, 1125.00, 450, "SF", 2.50),
                item('3/4" plywood subfloor', MATERIAL, 1440.00, 450, "SF", 3.20),
                item("Install subfloor - labor", LABOR, 1632.00, 24, "HR", 68.00),
                item("Dumpster rental", EQUIPMENT, 475.00, 1, "EA", 475.00),
            ],
            subtotal=4672.00,
            markup_amount=700.80,
            total=5372.80,
            schedule_impact_days=3,
            status=PENDING,
            requested_by="R. Okafor",
        ),
    ),
    # ------------------------------------------------------------------ 003
    Sample(
        id="co_003_email",
        format="txt",
        style="Informal email, prices inside sentences",
        text=doc("""
            From: Dave Kim <dkim@kimelectric.com>
            To: Sarah Lopez <slopez@harborviewdev.com>
            Date: Mon, Nov 4, 2024 9:12 AM
            Subject: CO #3 - Harbor View Apartments - extra outlets

            Hi Sarah,

            Per our walk on 11/2, the owner wants 12 additional duplex outlets in
            units 4A-4D. Pricing below:

            - material: $540
            - labor: 18 hrs @ $85/hr = $1,530

            Total for this change is $2,070. No schedule impact on our end.

            Let me know if it's approved and we'll get it on the board.

            Thanks,
            Dave Kim
            Kim Electric
        """),
        truth=ChangeOrder(
            change_order_number="3",
            project_name="Harbor View Apartments",
            date=dt.date(2024, 11, 4),
            contractor="Kim Electric",
            description="Add 12 additional duplex outlets in units 4A-4D.",
            reason="Owner request",
            line_items=[
                item("Material", MATERIAL, 540.00),
                item("Labor", LABOR, 1530.00, 18, "hr", 85.00),
            ],
            total=2070.00,
            schedule_impact_days=0,
            status=PENDING,
        ),
    ),
    # ------------------------------------------------------------------ 004
    Sample(
        id="co_004_credit",
        format="txt",
        style="Deductive change order (credit, negative amounts in parentheses)",
        text=doc("""
            BRIGHTLINE GENERAL CONTRACTORS
            DEDUCTIVE CHANGE ORDER  No. 0005

            Project:    Oakwood Elementary School Addition
            Project #:  OES-22-104
            Date:       January 22, 2025
            To:         Oakwood Unified School District

            Reason: Value engineering
            Description: Delete specified epoxy flooring in Corridor 200 and
            replace with sealed concrete finish per owner value-engineering request.

            Epoxy flooring - Corridor 200 (DELETED) ......... ($8,400.00)
            Sealed concrete finish (ADD) ....................  $2,150.00
                                                              ------------
            NET CREDIT TO OWNER .............................. ($6,250.00)

            Contract Time: No change

            APPROVED: Linda Chen, Facilities Director      01/30/2025
        """),
        truth=ChangeOrder(
            change_order_number="0005",
            project_name="Oakwood Elementary School Addition",
            project_number="OES-22-104",
            date=dt.date(2025, 1, 22),
            contractor="BrightLine General Contractors",
            owner="Oakwood Unified School District",
            description=(
                "Delete specified epoxy flooring in Corridor 200 and replace with sealed "
                "concrete finish per owner value-engineering request."
            ),
            reason="Value engineering",
            line_items=[
                item("Epoxy flooring - Corridor 200 (DELETED)", SUBCONTRACT, -8400.00),
                item("Sealed concrete finish (ADD)", SUBCONTRACT, 2150.00),
            ],
            total=-6250.00,
            schedule_impact_days=0,
            status=APPROVED,
            approved_by="Linda Chen",
            approval_date=dt.date(2025, 1, 30),
        ),
    ),
    # ------------------------------------------------------------------ 005
    Sample(
        id="co_005_wrong_math",
        format="txt",
        style="Looks clean, but the total is $100 too high",
        text=doc("""
            IRONCLAD BUILDERS
            Change Order: CO-021
            Date: 06/10/2025
            Project: Lakeside Office Park - Bldg C
            Owner: Lakeside Holdings LLC

            Scope: Upgrade lobby lighting to LED fixtures (24 fixtures).

              Labor                      2,400.00
              Material                   5,850.00
              Equipment (lift rental)      600.00
              Subtotal                   8,850.00
              Markup 10%                   885.00
              TOTAL                      9,835.00

            Schedule: add 2 days
            Status: Pending owner signature
        """),
        truth=ChangeOrder(
            change_order_number="CO-021",
            project_name="Lakeside Office Park - Bldg C",
            date=dt.date(2025, 6, 10),
            contractor="Ironclad Builders",
            owner="Lakeside Holdings LLC",
            description="Upgrade lobby lighting to LED fixtures (24 fixtures).",
            line_items=[
                item("Labor", LABOR, 2400.00),
                item("Material", MATERIAL, 5850.00),
                item("Equipment (lift rental)", EQUIPMENT, 600.00),
            ],
            subtotal=8850.00,
            markup_amount=885.00,
            total=9835.00,
            schedule_impact_days=2,
            status=PENDING,
        ),
        expected_issues=["total_mismatch"],
    ),
    # ------------------------------------------------------------------ 006
    Sample(
        id="co_006_missing_fields",
        format="txt",
        style="Very short request: no CO number, no date",
        text=doc("""
            Change request - Pinecrest Residence

            Add 4 recessed lights in kitchen + dimmer switch.
            Price: $1,180

            Contractor: Hartwell Homes
        """),
        truth=ChangeOrder(
            project_name="Pinecrest Residence",
            contractor="Hartwell Homes",
            description="Add 4 recessed lights in kitchen + dimmer switch.",
            total=1180.00,
        ),
        expected_issues=["missing_change_order_number", "missing_date"],
    ),
    # ------------------------------------------------------------------ 007
    Sample(
        id="co_007_shorthand",
        format="txt",
        style="Field shorthand notes, abbreviations, wrong total, unsure schedule",
        text=doc("""
            CO 15 - riverside clinic
            3/18/25
            add'l outlets in rm 204, 206 per owner
            lab 800  mat 450
            total 1,350
            +2 days?
        """),
        truth=ChangeOrder(
            change_order_number="15",
            project_name="Riverside Clinic",
            date=dt.date(2025, 3, 18),
            description="Additional outlets in rooms 204 and 206.",
            reason="Owner request",
            line_items=[item("Labor", LABOR, 800.00), item("Material", MATERIAL, 450.00)],
            total=1350.00,
            schedule_impact_days=2,
        ),
        expected_issues=["total_mismatch"],
    ),
    # ------------------------------------------------------------------ 008
    Sample(
        id="co_008_scanned",
        format="pdf_scan",
        style="Scanned paper form (image only, no text layer)",
        text=doc("""
            DELTA MECHANICAL INC.
            CHANGE ORDER

            CO No.: 4                Date: 2/18/2025
            Project: Westgate Retail Center      Job #: 1187
            Owner: Westgate Partners LP

            Reason: Design change (Architect ASI-12)
            Description: Relocate rooftop unit RTU-3 curb 6 ft east
            to clear new skylight opening.

              Labor ......................... $3,200.00
              Crane rental .................. $1,850.00
              Materials ..................... $  940.00
              Subtotal ...................... $5,990.00
              Markup 12% .................... $  718.80
              TOTAL ......................... $6,708.80

            Time extension: 2 days

            Approved by: M. Alvarez         Date: 2/21/2025
        """),
        truth=ChangeOrder(
            change_order_number="4",
            project_name="Westgate Retail Center",
            project_number="1187",
            date=dt.date(2025, 2, 18),
            contractor="Delta Mechanical Inc.",
            owner="Westgate Partners LP",
            description="Relocate rooftop unit RTU-3 curb 6 ft east to clear new skylight opening.",
            reason="Design change (Architect ASI-12)",
            line_items=[
                item("Labor", LABOR, 3200.00),
                item("Crane rental", EQUIPMENT, 1850.00),
                item("Materials", MATERIAL, 940.00),
            ],
            subtotal=5990.00,
            markup_amount=718.80,
            total=6708.80,
            schedule_impact_days=2,
            status=APPROVED,
            approved_by="M. Alvarez",
            approval_date=dt.date(2025, 2, 21),
        ),
    ),
    # ------------------------------------------------------------------ 009
    Sample(
        id="co_009_multi_page",
        format="pdf",
        style="Two-page PDF with sales tax; signatures on page 2",
        text=doc("""
            GRANITE PEAK BUILDERS
            CHANGE ORDER 12
            Date: 2025-04-03
            Project: Northside Community Center       Project No. 23-0456
            Owner: Northside Parks & Recreation Dept.

            Reason: Owner request
            Description of Work: Add acoustic wall panels to the multipurpose
            room and upgrade AV low-voltage wiring.

            1. Acoustic panels           48 EA @ $145.00     $6,960.00
            2. Panel installation labor  40 HR @ $72.00      $2,880.00
            3. AV wiring (Clearwave AV, subcontractor)       $3,450.00
            4. Scissor lift rental        1 WK @ $520.00       $520.00

                                       Subtotal             $13,810.00
                          Overhead & Profit 10%              $1,381.00
                   Sales Tax 8.25% (materials only)            $574.20
            {{PAGEBREAK}}
            CHANGE ORDER 12 - page 2 of 2

                                    TOTAL THIS CHANGE ORDER  $15,765.20

            Schedule Impact: 5 working days

            CONTRACTOR: Granite Peak Builders   By: S. Brooks   Date: 4/3/2025
            OWNER:      Northside Parks & Rec   By: ___________ Date: _________
        """),
        truth=ChangeOrder(
            change_order_number="12",
            project_name="Northside Community Center",
            project_number="23-0456",
            date=dt.date(2025, 4, 3),
            contractor="Granite Peak Builders",
            owner="Northside Parks & Recreation Dept.",
            description=(
                "Add acoustic wall panels to the multipurpose room and upgrade AV "
                "low-voltage wiring."
            ),
            reason="Owner request",
            line_items=[
                item("Acoustic panels", MATERIAL, 6960.00, 48, "EA", 145.00),
                item("Panel installation labor", LABOR, 2880.00, 40, "HR", 72.00),
                item("AV wiring (Clearwave AV, subcontractor)", SUBCONTRACT, 3450.00),
                item("Scissor lift rental", EQUIPMENT, 520.00, 1, "WK", 520.00),
            ],
            subtotal=13810.00,
            markup_amount=1381.00,
            tax_amount=574.20,
            total=15765.20,
            schedule_impact_days=5,
            status=PENDING,
        ),
    ),
    # ------------------------------------------------------------------ 010
    Sample(
        id="co_010_aia_style",
        format="pdf",
        style="AIA G701-style form: many contract-sum numbers that are NOT the CO total",
        text=doc("""
            CHANGE ORDER

            PROJECT: Elm Street Townhomes, Phase 2      CHANGE ORDER NUMBER: 009
            OWNER: Elm Street Housing Partners          DATE: September 9, 2024
            CONTRACTOR: Redstone Construction Co.
            ARCHITECT: Studio Nine Architects

            THE CONTRACT IS CHANGED AS FOLLOWS:
            Install additional waterproofing membrane at below-grade walls of
            Buildings 3 and 4 per RFI #27 response.

            The original Contract Sum was ......................... $2,450,000.00
            Net change by previously authorized Change Orders ..... $   63,200.00
            The Contract Sum prior to this Change Order was ....... $2,513,200.00
            The Contract Sum will be increased by this Change Order
              in the amount of .................................... $    4,250.00
            The new Contract Sum including this Change Order will be $2,517,450.00

            The Contract Time will be increased by ten (10) days.

            ARCHITECT                CONTRACTOR                 OWNER
            Studio Nine Architects   Redstone Construction Co.  Elm Street Housing Partners
            By: A. Feld  9/9/24      By: T. Nguyen  9/10/24     By: ______________
        """),
        truth=ChangeOrder(
            change_order_number="009",
            project_name="Elm Street Townhomes, Phase 2",
            date=dt.date(2024, 9, 9),
            contractor="Redstone Construction Co.",
            owner="Elm Street Housing Partners",
            description=(
                "Install additional waterproofing membrane at below-grade walls of "
                "Buildings 3 and 4 per RFI #27 response."
            ),
            reason="RFI #27 response",
            total=4250.00,
            schedule_impact_days=10,
            status=PENDING,
        ),
    ),
    # ------------------------------------------------------------------ 011
    Sample(
        id="co_011_rejected",
        format="txt",
        style="Change order request that the owner rejected",
        text=doc("""
            COASTAL INTERIORS INC.
            CHANGE ORDER REQUEST  #CO-031
            Date: 07/22/2025
            Project: Bayview Hotel Renovation (BH-1022)
            Owner: Bayview Hospitality Group

            Reason: Damage by other trades
            Description: Additional drywall patching in guest rooms, floors 3-5.

              Labor      $2,760.00
              Material     $410.00
              Total      $3,170.00

            Time requested: 3 days

            STATUS: REJECTED - Owner declined 07/25/2025 (P. Shah). Work to be
            performed under base contract scope.
        """),
        truth=ChangeOrder(
            change_order_number="CO-031",
            project_name="Bayview Hotel Renovation",
            project_number="BH-1022",
            date=dt.date(2025, 7, 22),
            contractor="Coastal Interiors Inc.",
            owner="Bayview Hospitality Group",
            description="Additional drywall patching in guest rooms, floors 3-5.",
            reason="Damage by other trades",
            line_items=[item("Labor", LABOR, 2760.00), item("Material", MATERIAL, 410.00)],
            total=3170.00,
            schedule_impact_days=3,
            status=REJECTED,
        ),
    ),
    # ------------------------------------------------------------------ 012
    Sample(
        id="co_012_ocr_noise",
        format="txt",
        style="Bad OCR output: O/0 and l/1 swaps, broken words",
        text=doc("""
            PAC1FIC C0AST BUILDERS
            Ch ange 0rder No. CO-08
            Date: 1O/03/2024
            Pr0ject: Sunset Ridge C1ubhouse   J0b 5520
            Owner: Sunset Ridge H0A

            Descripti0n: Add exteri0r hose bibs (4) with frost-free va1ves.

              Plumbing lab0r ....... 1,28O.OO
              Materia1s ............ 1,370.00
              Markup 1O% ...........   265.00
              T0TAL ................ 2,915.OO

            Time: 1 day
        """),
        truth=ChangeOrder(
            change_order_number="CO-08",
            project_name="Sunset Ridge Clubhouse",
            project_number="5520",
            date=dt.date(2024, 10, 3),
            contractor="Pacific Coast Builders",
            owner="Sunset Ridge HOA",
            description="Add exterior hose bibs (4) with frost-free valves.",
            line_items=[
                item("Plumbing labor", LABOR, 1280.00),
                item("Materials", MATERIAL, 1370.00),
            ],
            markup_amount=265.00,
            total=2915.00,
            schedule_impact_days=1,
        ),
    ),
    # ------------------------------------------------------------------ 013
    Sample(
        id="co_013_lump_sum",
        format="pdf",
        style="Lump-sum price with no cost breakdown",
        text=doc("""
            CORNERSTONE BUILDERS GROUP
            Change Order #2

            Date: May 12, 2025
            Project: Greenway Medical Office Building
            Project Number: N/A
            Owner: Greenway Health Partners

            Scope of Work:
            Furnish and install two (2) additional ADA-compliant automatic door
            operators at the main entrance vestibule.

            Lump Sum Price: $7,800.00
            Contract Time Change: 0 days

            Accepted by Owner: Dr. Karen Wells          Date: 5/15/2025
        """),
        truth=ChangeOrder(
            change_order_number="2",
            project_name="Greenway Medical Office Building",
            date=dt.date(2025, 5, 12),
            contractor="Cornerstone Builders Group",
            owner="Greenway Health Partners",
            description=(
                "Furnish and install two (2) additional ADA-compliant automatic door "
                "operators at the main entrance vestibule."
            ),
            total=7800.00,
            schedule_impact_days=0,
            status=APPROVED,
            approved_by="Dr. Karen Wells",
            approval_date=dt.date(2025, 5, 15),
        ),
    ),
    # ------------------------------------------------------------------ 014
    Sample(
        id="co_014_supersedes",
        format="txt",
        style="Mentions another CO number (supersedes CO-017) as a distractor",
        text=doc("""
            NORTHWIND CONSTRUCTION
            Change Order CO-018   (supersedes CO-017, withdrawn)
            Date: 12/02/2024
            Project: Cedar Hills High School Gym     Proj# CHHS-031
            Owner: Cedar Hills School District
            Requested by: Athletic Director

            Description: Revised pricing for gym floor game-line striping and
            center-court logo.

              Labor           $1,650.00
              Material          $720.00
              Subtotal        $2,370.00
              Fee 8%            $189.60
              Total           $2,559.60

            No time extension requested.
        """),
        truth=ChangeOrder(
            change_order_number="CO-018",
            project_name="Cedar Hills High School Gym",
            project_number="CHHS-031",
            date=dt.date(2024, 12, 2),
            contractor="Northwind Construction",
            owner="Cedar Hills School District",
            description="Revised pricing for gym floor game-line striping and center-court logo.",
            line_items=[item("Labor", LABOR, 1650.00), item("Material", MATERIAL, 720.00)],
            subtotal=2370.00,
            markup_amount=189.60,
            total=2559.60,
            schedule_impact_days=0,
            requested_by="Athletic Director",
        ),
    ),
    # ------------------------------------------------------------------ 015
    Sample(
        id="co_015_bad_dates",
        format="txt",
        style="Approval date is BEFORE the change-order date",
        text=doc("""
            AQUATECH CONSTRUCTORS
            CHANGE ORDER  CO-044
            Date Issued: 08/20/2025
            Project: Riverbend Water Treatment Plant Upgrade  (RWT-2025-03)
            Owner: City of Riverbend Public Works

            Reason: Unforeseen condition
            Description: Replace corroded 8" gate valve at Clarifier #2.

              Labor ........................ $3,100.00
              8" gate valve (material) ..... $2,640.00
              Excavator ................... $  450.00
              Subtotal ..................... $6,190.00
              Markup 15% ................... $  928.50
              TOTAL ........................ $7,118.50

            Time Extension: 4 days

            Approved: J. Ramirez, PE      Date: 08/12/2025
        """),
        truth=ChangeOrder(
            change_order_number="CO-044",
            project_name="Riverbend Water Treatment Plant Upgrade",
            project_number="RWT-2025-03",
            date=dt.date(2025, 8, 20),
            contractor="Aquatech Constructors",
            owner="City of Riverbend Public Works",
            description='Replace corroded 8" gate valve at Clarifier #2.',
            reason="Unforeseen condition",
            line_items=[
                item("Labor", LABOR, 3100.00),
                item('8" gate valve (material)', MATERIAL, 2640.00),
                item("Excavator", EQUIPMENT, 450.00),
            ],
            subtotal=6190.00,
            markup_amount=928.50,
            total=7118.50,
            schedule_impact_days=4,
            status=APPROVED,
            approved_by="J. Ramirez",
            approval_date=dt.date(2025, 8, 12),
        ),
        expected_issues=["approval_before_date"],
    ),
    # ------------------------------------------------------------------ 016
    Sample(
        id="co_016_weeks",
        format="pdf",
        style="Clean PDF, schedule given in weeks, subcontractor line",
        text=doc("""
            TITAN INDUSTRIAL BUILDERS
            CHANGE ORDER

            CO No. 6                           Date: Oct 28, 2024
            Project: Silver Creek Warehouse    Project No. SCW-881
            Owner: Silver Creek Logistics Inc.

            Reason: Owner request
            Description: Add two 10' x 12' insulated overhead doors at the
            north loading dock wall.

              Overhead doors (Precision Door Co., sub) ...... $14,200.00
              Structural steel framing (material) ...........  $3,650.00
              Concrete work & labor .........................  $2,980.00
              Subtotal ...................................... $20,830.00
              OH&P 10% ......................................  $2,083.00
              TOTAL ......................................... $22,913.00

            Time Extension: 2 weeks

            Approved by: G. Patel                Date: 11/01/2024
        """),
        truth=ChangeOrder(
            change_order_number="6",
            project_name="Silver Creek Warehouse",
            project_number="SCW-881",
            date=dt.date(2024, 10, 28),
            contractor="Titan Industrial Builders",
            owner="Silver Creek Logistics Inc.",
            description=(
                "Add two 10' x 12' insulated overhead doors at the north loading dock wall."
            ),
            reason="Owner request",
            line_items=[
                item("Overhead doors (Precision Door Co., sub)", SUBCONTRACT, 14200.00),
                item("Structural steel framing (material)", MATERIAL, 3650.00),
                item("Concrete work & labor", LABOR, 2980.00),
            ],
            subtotal=20830.00,
            markup_amount=2083.00,
            total=22913.00,
            schedule_impact_days=14,
            status=APPROVED,
            approved_by="G. Patel",
            approval_date=dt.date(2024, 11, 1),
        ),
    ),
]
