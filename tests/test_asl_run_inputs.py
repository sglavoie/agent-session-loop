"""The run-admission boundary: what a run is allowed to start on, and reach.

The behavioural coverage of these refusals lives where it always has —
``tests/test_asl_run_launch_gates.py`` drives the flag cross-checks through both the
runner and the supervisor helper, ``tests/test_asl_workspaces.py`` resolves
real registries, and ``tests/test_asl_review_*.py`` routes whole runs through
the cycle caps. What is asserted here is the boundary those modules cannot
state, because they exercise the loop rather than the seam:

* the module does not import the runner, and needs nothing from it;
* it opens exactly one path of its own — the task packet's artifacts, read to
  judge them — and its single durable write is delegated to the migration that
  owns it; and
* the sequence and the refusals that make admission separable at all.

That last group is the reason this file exists. An admission bug does not fail
a gate: it starts a run whose destination nothing has bound, or refuses a run
for the wrong reason and in the wrong order, and the operator sees only the
message that came out last.
"""

from .asl_run_inputs_cases_admission import (
    CycleCapTests,
    RepositoryPathLabelTests,
    RunAdmissionTests,
    RunInputsBoundaryTests,
)
from .asl_run_inputs_cases_packet import (
    PlanStoreStateRootTests,
    RepositoryIntentMessageTests,
    TaskPacketIntentTests,
)
from .asl_run_inputs_cases_plan import (
    LegacyBindingTests,
    PlanStatusProjectionTests,
    RunnerOwnedRootsTests,
)

__all__ = [name for name in globals() if name.endswith("Tests")]
