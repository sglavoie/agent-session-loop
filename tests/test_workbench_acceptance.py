# ruff: noqa: F403

import unittest

from . import workbench_acceptance_test_support as _support
from .workbench_acceptance_entrypoints import *
from .workbench_acceptance_evidence import *
from .workbench_acceptance_production import *
from .workbench_acceptance_validation import *

# Integration because the gate it proves is enforced by real git history
# and real packed artifacts: synthetic repositories are built and their
# commits packed, because a fake digest would agree with a fake packer.
ASL_TEST_LANE = "integration"

for _case in tuple(globals().values()):
    if (
        isinstance(_case, type)
        and issubclass(_case, unittest.TestCase)
        and _case.__module__.startswith("tests.workbench_acceptance_")
    ):
        _case.__module__ = __name__
# Left bound, the loop variable is one more module attribute naming the last
# test class, and the loader would run that class twice.
del _case


if __name__ == "__main__":
    unittest.main()


def setUpModule() -> None:
    _support.setUpModule()


def tearDownModule() -> None:
    _support.tearDownModule()
