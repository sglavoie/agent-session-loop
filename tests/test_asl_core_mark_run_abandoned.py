"""MarkRunAbandonedTests.

Split from tests/test_asl_core.py."""

from __future__ import annotations

import unittest

from . import (
    asl_core_mark_run_abandoned_locking,
    asl_core_mark_run_abandoned_takeover_and_assurance,
)


class MarkRunAbandonedTests(
    asl_core_mark_run_abandoned_locking.MarkRunAbandonedTests,
    asl_core_mark_run_abandoned_takeover_and_assurance.MarkRunAbandonedTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
