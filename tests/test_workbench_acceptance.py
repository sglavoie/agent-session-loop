# ruff: noqa: F401, F403, F405

import unittest

from . import workbench_acceptance_test_support as _support
from .workbench_acceptance_entrypoints import *
from .workbench_acceptance_evidence import *
from .workbench_acceptance_production import *
from .workbench_acceptance_validation import *

for _case in tuple(globals().values()):
    if (
        isinstance(_case, type)
        and issubclass(_case, unittest.TestCase)
        and _case.__module__.startswith("tests.workbench_acceptance_")
    ):
        _case.__module__ = __name__


if __name__ == "__main__":
    unittest.main()


def setUpModule() -> None:
    _support.setUpModule()


def tearDownModule() -> None:
    _support.tearDownModule()
