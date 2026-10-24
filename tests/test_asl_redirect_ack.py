# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_asl_redirect_ack test groups."""

from ._test_asl_redirect_ack_coalescing import *  # noqa: F403
from ._test_asl_redirect_ack_receipts import *  # noqa: F403
from ._test_asl_redirect_ack_command import *  # noqa: F403


ASL_TEST_PORTABLE = True


if __name__ == "__main__":
    import unittest

    unittest.main()
