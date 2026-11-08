"""ControlQueueDrainInRunnerTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import unittest

from . import asl_control_drain_abort, asl_control_drain_commands


class ControlQueueDrainInRunnerTests(
    asl_control_drain_commands.ControlQueueDrainInRunnerTests,
    asl_control_drain_abort.ControlQueueDrainInRunnerTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
