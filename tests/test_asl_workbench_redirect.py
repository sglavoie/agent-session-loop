# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_asl_workbench_redirect test groups."""

from ._test_asl_workbench_redirect_part_1 import *  # noqa: F401, F403
from ._test_asl_workbench_redirect_part_2 import *  # noqa: F401, F403
from ._test_asl_workbench_redirect_part_3 import *  # noqa: F401, F403


if __name__ == "__main__":
    import unittest

    unittest.main()
