# mypy: ignore-errors
# ruff: noqa: F401, I001
"""Discovery facade for focused test_asl_harness_lifecycle test groups."""

from ._test_asl_harness_lifecycle_part_1 import *  # noqa: F403
from ._test_asl_harness_lifecycle_part_2 import *  # noqa: F403
from ._test_asl_harness_lifecycle_part_3 import *  # noqa: F403
from ._test_asl_harness_lifecycle_part_4 import *  # noqa: F403
from ._test_asl_harness_lifecycle_support import (
    _FakePipe,
    _FakeProcess,
    _TurnTestCase,
)


ASL_TEST_PORTABLE = True


if __name__ == "__main__":
    import unittest

    unittest.main()
