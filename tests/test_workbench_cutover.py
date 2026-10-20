# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_workbench_cutover test groups."""

from ._test_workbench_cutover_part_1 import *  # noqa: F401, F403
from ._test_workbench_cutover_part_2 import *  # noqa: F401, F403
from ._test_workbench_cutover_part_3 import *  # noqa: F401, F403


if __name__ == "__main__":
    import unittest

    unittest.main()
