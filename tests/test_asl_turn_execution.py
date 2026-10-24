"""The typed turn-execution boundary, exercised without the run loop.

``TurnExecutor`` is the single entry and exit for spending a turn, collecting
its exited process, and publishing the accepted result. These tests drive it
through collaborators defined here rather than through ``runner``, which is
the point of the boundary: if it only worked against the run loop's own
persistence and progress objects, it would not be a boundary at all.

The five endings a turn can have -- success, failure, timeout, interrupt, and
a resumed turn following an earlier one -- are covered as properties of the
records the executor returns and the events it appends, not as call
assertions, because those records are what the ledger and the next turn read.
"""

from .asl_turn_execution_cases_boundary import (
    ResumedTurnTests,
    TurnExecutorBoundaryTests,
)
from .asl_turn_execution_cases_endings import TurnExecutorEndingTests
from .asl_turn_execution_cases_preview import PostResultTerminationPreviewTests

ASL_TEST_PORTABLE = True

__all__ = [name for name in globals() if name.endswith("Tests")]
