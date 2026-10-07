"""Change-order extraction pipeline.

from change_order_extractor import run
result = run("change_order.pdf")
"""

from change_order_extractor.pipeline import Pipeline, run

__all__ = ["Pipeline", "run"]
