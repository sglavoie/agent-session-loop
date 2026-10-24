"""The run loop as a module: what it owns, what it left, and what it may reach.

The behaviour of a run is covered where it always has been — whole runs against
a fake harness in ``tests/test_asl_harness_*.py``, the gate sequence
in ``tests/test_asl_run_disposition_source.py``, and one file per delegate boundary for
the work Sessions 31-36 moved out. Those suites drive the loop; none of them can
state what this file states, because each is scoped to one behaviour rather
than to the module's shape.

What is asserted here is the boundary Session 37 created:

* the module never imports the runner, directly or lazily, and reaches nothing
  through it either — the loop is the bottom of the package, so a runner import
  here would be a cycle rather than a layering slip;
* every symbol the session moved lives here and is gone from the runner, so
  there is no alias to keep the old spelling alive;
* the loop is singular: one ``run_locked``, one ``while`` statement, one
  ``RunContext``, and the private helpers that serve them are reached from
  nowhere else in the package;
* what the runner has left is two delegating entry points, and the one that
  starts a run supplies this module's ``run_locked`` as the seam.

The charter is widened by two behaviours, deliberately, and both are about
where something sits in this module rather than about what a run does.

The first is the paused-work settlement's *gate*.
``PausedRetainedWorkGateTests`` below decides which pauses
reach ``commit_settlement`` and which never do, with that module's own
settlement replaced by a recorder — so what is asserted is still a property of
this module's shape (one arming site, one call site, one set of refusals, one
progress line reported after the record is durable, and the two source-order
assertions that fail if either site moves)
rather than of a run. The commit those refusals let through is covered where it
is made, in ``tests/test_asl_turn_commit_*.py``, and the cross-process behaviour
this all exists for is covered in ``tests/test_asl_harness_*.py``.
The second is the commit-group barrier. ``CommitGroupBarrierTests`` asserts
that spending a group's retry budget arms it and that it is read at the loop's
selection boundary, which is again a property of this module's shape: the
bound on the retries lives in ``commit_settlement``, and what this module
decides is that exhausting it ends the run rather than only ending the
attempts. The commit that budget is spent on is covered in
``tests/test_asl_commit_groups.py``.

Nothing here drives a subprocess, so the module stays in both the fast and the
portable lanes.
"""

# The moved-symbol and borrowed-name tables live in ``asl_run_loop_support``:
# they record names the session 36 extraction used to spell ``runner._x``.
from .asl_run_loop_cases_barrier import CommitGroupBarrierTests
from .asl_run_loop_cases_boundary import (
    BoundaryTests,
    RunnerRemainderTests,
    SingularityTests,
)
from .asl_run_loop_cases_gate import PausedRetainedWorkGateTests

ASL_TEST_PORTABLE = True

__all__ = [name for name in globals() if name.endswith("Tests")]
