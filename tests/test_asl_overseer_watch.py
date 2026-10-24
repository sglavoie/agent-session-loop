# ruff: noqa: E402, F403, F405, I001
"""Compatibility facade for the test asl overseer watch implementation."""

from __future__ import annotations

from . import test_asl_overseer_watch_shared as _shared

globals().update(
    {
        name: value
        for name, value in vars(_shared).items()
        if not name.startswith("__") and name != "load_tests"
    }
)
from .test_asl_overseer_watch_shared import *
from .support_base import load_section_tests
from . import asl_overseer_watch_section_1 as _asl_overseer_watch_section_1
from . import asl_overseer_watch_section_2 as _asl_overseer_watch_section_2
from . import asl_overseer_watch_section_3 as _asl_overseer_watch_section_3

_PARTS = (
    _asl_overseer_watch_section_1,
    _asl_overseer_watch_section_2,
    _asl_overseer_watch_section_3,
)
for _part in _PARTS:
    globals().update(
        {
            name: value
            for name, value in vars(_part).items()
            if not name.startswith("__") and name != "load_tests"
        }
    )
for _part in _PARTS:
    _part.__dict__.update(
        {
            name: value
            for name, value in globals().items()
            if not name.startswith("__") and name != "load_tests"
        }
    )
del _part


#: The sections run as their own shards, in parallel. Assigned after the
#: shared names are copied in, so the shared module's own marker cannot
#: shadow it.
ASL_TEST_SHARDS = (
    "tests.asl_overseer_watch_section_1",
    "tests.asl_overseer_watch_section_2",
    "tests.asl_overseer_watch_section_3",
)
ASL_TEST_PORTABLE = True


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load the focused test sections as one historical test module."""
    return load_section_tests(loader, *_PARTS)


if __name__ == "__main__":
    unittest.main()
