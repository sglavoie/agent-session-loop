"""The durable write core: one lock, one state record, one event log.

The behavioural coverage of what gets written lives where it always has —
``tests/test_asl_compaction_state.py`` reads back compacted state, and
``tests/test_asl_agent_mux_projection_state.py`` drives the projection through whole
runs. What is asserted here is the contract those modules cannot state, because
they exercise the run rather than the seam:

* the module does not import the runner, and needs nothing from it;
* the lock contract — ``_RunPersistence.lock`` is the outermost reentrant mutex
  every durable write takes, and it is never held across a mailbox read, a
  lease read, a sleep, or a subprocess; and
* the two bounds a compact record has to keep: it never embeds raw verification
  output, and it never claims a schema version for a block it does not carry;
  and
* the converse of those bounds — a block the compaction was never told about
  survives it verbatim, which is what lets a free-form record like
  ``paused_work_commit`` reach its readers without a schema of its own.

The lock group is the reason this file exists. A lock bug here does not fail a
gate: it tears a ``state.json`` between two transitions, or it lets a slow
``amx`` spawn stall the heartbeat thread that only wanted to stamp liveness,
and both look exactly like a healthy run from the outside.
"""

import unittest

from . import (
    asl_run_persistence_locking,
    asl_run_persistence_projections,
    asl_run_persistence_writes,
)

ASL_TEST_PORTABLE = True


class RunPersistenceBoundaryTests(
    asl_run_persistence_locking.RunPersistenceBoundaryTests
):
    """Preserve the established test IDs."""


class RunPersistenceLockTests(asl_run_persistence_locking.RunPersistenceLockTests):
    """Preserve the established test IDs."""


class CompactStateTests(asl_run_persistence_projections.CompactStateTests):
    """Preserve the established test IDs."""


class PausedWorkCommitProjectionTests(
    asl_run_persistence_projections.PausedWorkCommitProjectionTests
):
    """Preserve the established test IDs."""


class UnreadableStateProjectionTests(
    asl_run_persistence_projections.UnreadableStateProjectionTests
):
    """Preserve the established test IDs."""


class RunFinishedEventPayloadTests(
    asl_run_persistence_projections.RunFinishedEventPayloadTests
):
    """Preserve the established test IDs."""


class DurableWriteTests(asl_run_persistence_writes.DurableWriteTests):
    """Preserve the established test IDs."""


class NotifySeamTests(asl_run_persistence_writes.NotifySeamTests):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
