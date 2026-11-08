"""AbortTurnOrchestrationTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import unittest

from . import asl_control_abort_turn_outcomes, asl_control_abort_turn_timing


class AbortTurnOrchestrationTests(
    asl_control_abort_turn_outcomes.AbortTurnOrchestrationTests,
    asl_control_abort_turn_timing.AbortTurnOrchestrationTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
