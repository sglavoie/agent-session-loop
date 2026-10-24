# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_asl_copilot_resume test groups."""

from ._test_asl_copilot_resume_restore import *  # noqa: F403
from ._test_asl_copilot_resume_adapter import *  # noqa: F403


ASL_TEST_LANE = "integration"


if __name__ == "__main__":
    import unittest

    unittest.main()
