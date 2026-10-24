# ruff: noqa: F401, I001
"""Compatibility facade for progress journal test groups."""

from .test_asl_progress_cli import ProgressCliTests
from .test_asl_progress_journal import ProgressJournalTests
from .test_asl_progress_repair import ProgressRepairHelperTests
from .test_asl_progress_runner import RunnerProgressEntryHelperTests


#: Not a shard of its own: the modules it re-exports run these tests.
ASL_TEST_SHARDS: tuple[str, ...] = ()
