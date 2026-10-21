"""What survives a review verdict, asserted at the boundary rather than a run.

The behavioural coverage of the repair/rereview loop already lives where it
always has — ``tests/test_asl_review_*.py`` owns the cycle accounting,
``tests/test_asl_review_lifecycle_*.py`` drives real runs through reopen,
materialization and pause, and ``tests/test_asl_cli.py`` owns the run JSON
those paths publish. What is asserted here is the contract those modules
cannot state, because they exercise the run rather than the boundary:

* the module does not import the runner, and needs nothing from it;
* it opens no file of its own — every durable write goes through the
  ``_RunPersistence`` handle the caller supplied, under that handle's lock;
* both seams the loop still binds are structural, so drift in either is
  silent: ``handoff_guards._read_review_verdict`` has to keep matching
  ``review_delta.ReviewVerdictReader`` and
  ``session_verification._reinspect_after_plan_mutation`` has to keep matching
  :data:`review_cycle.PlanMutationReinspector`; and
* the arithmetic nobody watching a run can see is right — a missing cap is not
  a zero cap, a recovered backfill counts its source once, and a refused reopen
  is recorded as loudly as a successful one.

The last two are why this file exists. A reader whose signature drifted still
type-checks at the one call site that passes it by keyword, and reopen lineage
is only ever read after the fact, by an operator asking why a repair never
appeared — which is exactly when a miscount is no longer reproducible.
"""

import unittest

from .asl_review_cycle_cases_boundary import (
    PriorReviewCapTests,
    ReopenLineageTests,
    ReviewCycleBoundaryTests,
)
from .asl_review_cycle_cases_reopen import (
    RecordReviewCycleTests,
    ReopenBaselineTests,
    TerminalReviewReasonTests,
    VerdictAttestationTests,
)

if __name__ == "__main__":
    unittest.main()
