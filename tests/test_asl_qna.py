# ruff: noqa: E402, F403, F405, I001
"""Compatibility facade for the test asl qna implementation."""

from __future__ import annotations

from . import test_asl_qna_shared as _shared

globals().update(
    {
        name: value
        for name, value in vars(_shared).items()
        if not name.startswith("__") and name != "load_tests"
    }
)
from .test_asl_qna_shared import *
from . import asl_qna_live_context as _asl_qna_live_context
from . import asl_qna_payload as _asl_qna_payload
from . import asl_qna_failures as _asl_qna_failures
from . import asl_qna_cli as _asl_qna_cli

_PARTS = (
    _asl_qna_live_context,
    _asl_qna_payload,
    _asl_qna_failures,
    _asl_qna_cli,
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


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load the focused test sections as one historical test module."""
    suite = unittest.TestSuite()
    for part in _PARTS:
        suite.addTests(loader.loadTestsFromModule(part))
    return suite


if __name__ == "__main__":
    unittest.main()
