# mypy: ignore-errors
# ruff: noqa: I001
"""Discovery facade for focused test_workbench_cutover test groups."""

from ._test_workbench_cutover_backups import *  # noqa: F403
from ._test_workbench_cutover_journal import *  # noqa: F403
from ._test_workbench_cutover_phone_acceptance import *  # noqa: F403


# Integration: it writes real prefixes and takes real SQLite backups of
# them, because a backup proved against a fake database is a claim about
# the fake.
ASL_TEST_LANE = "integration"


if __name__ == "__main__":
    import unittest

    unittest.main()
