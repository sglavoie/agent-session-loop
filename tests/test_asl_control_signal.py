"""ControlSignalTests.

Split from tests/test_asl_control.py.

Cases are grouped by responsibility in sibling modules.
"""

import unittest

from . import (
    asl_control_signal_endpoints,
    asl_control_signal_processes,
)


class ControlSignalTests(
    asl_control_signal_endpoints.ControlSignalTests,
    asl_control_signal_processes.ControlSignalTests,
):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
