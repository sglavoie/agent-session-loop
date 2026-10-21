"""The review verdict and repair/rereview lanes as models.

Three decisions used to be ordered branches inside the run loop: whether a
submitted verdict is accepted, retried, or has spent its budget; what one
completed review cycle settles on; and what repair materialization produced.
Each is now a function of its inputs, so each is tested as one — the runner's
effects (revert, pause, durable append) are not reached from here at all.

The clause *order* inside :func:`classify_review_cycle` is itself policy, so it
is asserted directly: a repeat that arrives on the same cycle as an exhausted
cap must be reported as a repeat, and the table-driven model tests below make an
undeclared step raise rather than silently record a lifecycle the plan never
had.
"""

import unittest

from .asl_review_transitions_cases_cycle import (
    AdjudicateTerminalReviewTests,
    ClassifyMaterializationTests,
    ClassifyReviewCycleTests,
    RepairGroupBudgetTests,
)
from .asl_review_transitions_cases_models import (
    AdjudicateReviewVerdictTests,
    FindingDeltaTests,
    ReviewCycleMachineModelTests,
    ReviewVerdictFingerprintTests,
    ReviewVerdictMachineModelTests,
    ReviewVerdictRetryHistoryTests,
)

if __name__ == "__main__":
    unittest.main()
