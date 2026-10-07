"""Separates the change order itself from backup documents attached after it.

Real change-order PDFs often bundle the signed form (page 1) with backup:
the contractor's cost memos, subcontractor quotes, proposals. Those pages have
their own 'TOTAL:', 'Subtotal:', 'Job #:' lines, which must not be read as the
change order's values.

A page (after the first) starts an attachment when its first lines look like
the start of a different document:
    * a memo header: two or more of DATE: / TO: / FROM: / RE: / SUBJECT: / CC: / ATTN:
    * a document title: QUOTE, PROPOSAL, INVOICE, ESTIMATE, CHANGE ORDER REQUEST...
    * 'Page 1 of N' (a new document's page numbering restarts)
unless the page says it continues the change order ('page 2 of 3', 'continued').
Every page after the first attachment page is an attachment too: backup comes
after the document it supports.
"""

import re

from change_order_extractor.schemas import Document, Page

HEAD_LINES = 12  # only the top of a page decides whether it starts a new document
MEMO_LABEL = re.compile(r"^\s*(?:date|to|from|re|subject|cc|attn)\s*[:;]", re.IGNORECASE)
DOCUMENT_TITLE = re.compile(
    r"^\s*(?:quote|quotation|proposal|invoice|estimate|change\s+order\s+request|"
    r"purchase\s+order)\b",
    re.IGNORECASE,
)
FIRST_PAGE = re.compile(r"\bpage\s+1\s+of\s+\d+\b", re.IGNORECASE)
CONTINUATION = re.compile(r"\bpage\s+(?:[2-9]|\d{2,})\s+of\b|\bcontinued\b", re.IGNORECASE)


def starts_new_document(page: Page) -> bool:
    lines = [line for line in page.text.splitlines() if line.strip()][:HEAD_LINES]
    if CONTINUATION.search("\n".join(lines)):
        return False
    memo_labels = sum(1 for line in lines if MEMO_LABEL.match(line))
    return (
        memo_labels >= 2
        or any(DOCUMENT_TITLE.match(line) for line in lines)
        or FIRST_PAGE.search(page.text) is not None
    )


def mark_attachments(document: Document) -> Document:
    """Return a copy of the document with `is_attachment` set on backup pages."""
    pages: list[Page] = []
    in_attachments = False
    for page in document.pages:
        if page.number > 1 and not page.is_scanned and starts_new_document(page):
            in_attachments = True
        pages.append(page.model_copy(update={"is_attachment": in_attachments}))
    return document.model_copy(update={"pages": pages})
