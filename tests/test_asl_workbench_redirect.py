# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_asl_workbench_redirect test groups."""

from ._test_asl_workbench_redirect_submission import *  # noqa: F403
from ._test_asl_workbench_redirect_replay import *  # noqa: F403
from ._test_asl_workbench_redirect_recovery import *  # noqa: F403


ASL_TEST_LANE = "integration"


if __name__ == "__main__":
    import unittest

    unittest.main()
