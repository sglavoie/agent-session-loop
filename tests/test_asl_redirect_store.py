# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_asl_redirect_store test groups."""

from ._test_asl_redirect_store_submission import *  # noqa: F403
from ._test_asl_redirect_store_replay import *  # noqa: F403
from ._test_asl_redirect_store_launch_records import *  # noqa: F403
from ._test_asl_redirect_store_durability import *  # noqa: F403


ASL_TEST_PORTABLE = True


if __name__ == "__main__":
    import unittest

    unittest.main()
