# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_asl_conversations test groups."""

from ._test_asl_conversations_part_1 import *  # noqa: F401, F403
from ._test_asl_conversations_part_2 import *  # noqa: F401, F403


if __name__ == "__main__":
    import unittest

    unittest.main()
