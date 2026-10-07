"""Shared interface that every extractor implements."""

from abc import ABC, abstractmethod
from typing import ClassVar

from change_order_extractor.schemas import Document, ExtractedChangeOrder


class Extractor(ABC):
    """Turns a cleaned Document into an ExtractedChangeOrder.

    Every field an extractor fills must carry:
      * a normalized value,
      * `evidence`: the exact text it came from, copied from the document,
      * `source`: who found it,
      * a preliminary confidence and notes. Final confidence is decided later
        by the scoring step, which also compares extractors with each other.

    Fields the extractor can't find are left empty. Guessing is worse than
    leaving a gap, because the other extractor may fill it.
    """

    name: ClassVar[str]

    @abstractmethod
    def extract(self, document: Document) -> ExtractedChangeOrder: ...
