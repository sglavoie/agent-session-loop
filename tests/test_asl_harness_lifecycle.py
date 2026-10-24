# mypy: ignore-errors
# ruff: noqa: F401, I001
"""Discovery facade for focused test_asl_harness_lifecycle test groups."""

from ._test_asl_harness_lifecycle_turn_state import *  # noqa: F403
from ._test_asl_harness_lifecycle_cancellation import *  # noqa: F403
from ._test_asl_harness_lifecycle_continuity import *  # noqa: F403
from ._test_asl_harness_lifecycle_capabilities import *  # noqa: F403
from ._test_asl_harness_lifecycle_support import (
    _FakePipe,
    _FakeProcess,
    _TurnTestCase,
)


ASL_TEST_PORTABLE = True


if __name__ == "__main__":
    import unittest

    unittest.main()
