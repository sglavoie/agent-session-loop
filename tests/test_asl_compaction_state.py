"""State compaction tests.

Split from tests/test_asl_compaction.py."""

from __future__ import annotations

import unittest

from . import (
    asl_compaction_state_artifacts,
    asl_compaction_state_events_and_cli,
    asl_compaction_state_schema,
)


class StateCompactionTests(
    asl_compaction_state_schema.StateCompactionTests,
    asl_compaction_state_artifacts.StateCompactionTests,
    asl_compaction_state_events_and_cli.StateCompactionTests,
):
    """Preserve the established test IDs across the focused case modules."""


if __name__ == "__main__":
    unittest.main()
