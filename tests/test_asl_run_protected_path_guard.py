"""Protected path guard run tests."""

from __future__ import annotations

import unittest

from . import (
    asl_run_protected_path_guard_evidence,
    asl_run_protected_path_guard_exceptions,
)

ASL_TEST_LANE = "integration"


class ProtectedPathGuardRunTests(
    asl_run_protected_path_guard_evidence.ProtectedPathGuardRunTests,
    asl_run_protected_path_guard_exceptions.ProtectedPathGuardRunTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
