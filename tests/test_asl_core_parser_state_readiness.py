"""Readiness metadata, split-from pointers, and status ledger sync.

Split from tests/test_asl_core.py."""

from __future__ import annotations

import unittest

from . import (
    asl_core_parser_state_readiness_parsing,
    asl_core_parser_state_readiness_rewrites_and_hashes,
)


class ParserAndStateTests(
    asl_core_parser_state_readiness_parsing.ParserAndStateTests,
    asl_core_parser_state_readiness_rewrites_and_hashes.ParserAndStateTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
