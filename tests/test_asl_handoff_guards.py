# ruff: noqa: F401, I001
"""Compatibility facade for handoff-guard test groups."""

from .test_asl_handoff_guards_boundary import (
    HandoffGuardsBoundaryTests,
    VerifyThenProgressThenLedgersTests,
)
from .test_asl_handoff_guards_disposition import (
    LegacyUnobservedRepositoriesTests,
    TerminatingResultDispositionTests,
)
from .test_asl_handoff_guards_progress import (
    AppendProgressEntryTests,
    RevertSessionStatusTests,
)
from .test_asl_handoff_guards_reopening import (
    PlanArtifactAdjudicationTests,
    RefusalReopeningTruthTests,
)


#: Not a shard of its own: the modules it re-exports run these tests.
ASL_TEST_SHARDS: tuple[str, ...] = ()
