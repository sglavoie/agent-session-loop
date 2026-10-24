# ruff: noqa: F401, I001
"""Compatibility facade for diagnostics-export test groups."""

from .test_asl_diagnostics_export_document import (
    ExportCorrelationTests,
    ExportRedactionTests,
    ExportShapeTests,
)
from .test_asl_diagnostics_export_limits import (
    ExportBoundsTests,
    ExportCommandTests,
    ExportDegradedTests,
    ExportDeterminismTests,
)


#: Not a shard of its own: the modules it re-exports run these tests.
ASL_TEST_SHARDS: tuple[str, ...] = ()
