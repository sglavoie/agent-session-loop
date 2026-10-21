"""Boundary tests for `agent_session_loop.write_guards` and its siblings.

The behavioural coverage of these guards is end to end and lives elsewhere:
`tests/test_asl_run_protected_path_guard.py` drives real runs against real checkouts and
asserts what an operator sees after a violation, and `tests/test_asl_turn_commit_*.py`
pins that the commit happens after every guard. Neither can cheaply state the
things this module owns, so they are stated here: the extraction contract
(runner-free, no alias left behind, the seams required rather than defaulted and
supplied at every call site), the five-step refusal every enforcement path
performs in one order, and the four functions whose inputs are payloads rather
than repositories.

Nothing here spawns a process. The two functions that would reach Git --
`_protected_path_review_changed_paths` through the review fingerprint and
`_regenerate_stale_repair_child_scopes` through the scope reader -- are exercised
against their collaborators' seams rather than against checkouts, which is what
keeps this module in `PORTABLE_MODULES`.
"""

from .asl_write_guards_cases_boundary import (
    RefusalShapeTests,
    SeamTests,
    WriteGuardsBoundaryTests,
)
from .asl_write_guards_cases_protected import (
    ProtectedPathChangedPathsTests,
    ProtectedPathReviewChangedPathsTests,
)
from .asl_write_guards_cases_repair import (
    PendingRepairChildContinuationTests,
    RegenerateStaleRepairChildScopesTests,
)

__all__ = [name for name in globals() if name.endswith("Tests")]
