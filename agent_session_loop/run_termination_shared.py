# ruff: noqa: F401
"""How a run ends: the terminal verdict, the pause, and the honored stop.

Every run stops exactly once, and there are only three ways it can: it reaches
an ending it earned, it is paused because something blocked it, or a supervisor
asked it to stop. This module owns all three so the loop is left deciding *when*
a run ends rather than restating *what* each ending means.

The terminal path is a fixed order and the order is the contract.
:func:`_terminalize_run` adjudicates first, audits second, finalizes third.
Adjudication is the only step here that can change a verdict: a run may not
report success it did not earn, so :func:`_adjudicate_terminal_run` re-derives
the claim from the durable evidence and pauses the run when the evidence is
missing. The audit stays advisory and downstream of it, and it grades the same
verdict an operator reads. Finalization closes the readiness lifecycle and the
commit ledger last, so ``run_finished`` names artifacts that already exist.

Durable records this boundary writes:

* ``payload["terminal_adjudication"]`` -- the typed verdict of every ending,
  carrying what the run claimed about itself so an operator can see the claim
  was tested rather than assumed. Written under the run's mutex, before the
  audit and before any pause it causes.
* ``payload["paused"]`` and its whole block -- ``pause_reason``,
  ``pause_message``, ``pause_details``, ``pause_policy`` and the
  ``action_required`` ask. :func:`set_pause` is the single writer;
  :func:`_pause_run` is the same write plus the ``run-paused`` event and the
  AgentMux notification.
* ``payload["readiness_state_final"]`` -- the plan's readiness state as the run
  leaves it, or the error that stopped it being written.
* ``payload["reason"] = "stopped"`` and ``payload["final_snapshot"]`` -- the
  clean early exit a supervisor's stop request earns.

Nothing here imports the runner. Four collaborators this module does not own
enter through seams rather than through an import, each declared below and
satisfied by the module that owns the write:
:class:`ReadinessStateWriter` by ``readiness_gate.write_readiness_state``,
:class:`SessionStatusReverter` by ``handoff_guards.session_status_reverter``,
:data:`SnapshotReinspector` by ``session_verification._reinspect_after_revert``,
and :data:`ExistingReviewVerdictRecorder` by
``review_cycle.existing_review_verdict_recorder``.

Invariants that survive the move:

* **One definition of success.** Every terminal path -- normal completion,
  review exhaustion, a resumed run, stop-after-current, and the failure paths
  -- closes through :func:`_adjudicate_terminal_run`. A run that already paused
  keeps the specific blocker an operator needs instead of a generic verdict, and
  a dry run is not adjudicated because it claimed nothing.
* **Lock ownership.** Adjudication, the pause write, and the honored stop each
  take the run's reentrant persistence mutex around their whole
  read-decide-write sequence, so a concurrent control action cannot interleave
  between a decision and the record of it.
* **Advisory audit.** An audit that fails, finds something, or is cancelled
  never changes the adjudicated verdict or the exit code.
* **One pause writer.** A pause is never assembled field by field at a call
  site: :func:`set_pause` classifies the reason through
  :mod:`agent_session_loop.pause_policy` and attaches the remediation the pause
  evidence carries, so ``asl status`` and the supervisor helper read one
  vocabulary whichever refusal produced the pause.
* **Consumption at the stop.** A stop-after-current request is consumed only in
  :func:`_honor_stop_after_current`, at the moment the loop actually stops, so a
  request is never recorded as consumed by a run that then keeps working.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import (
    clock,
    commit_settlement,
    events,
    harnesses,
    live_verification,
    overseer_discovery,
    pause_policy,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_models,
    plans_payloads,
    plans_registry,
    plans_snapshot,
    preflight_auto_repair,
    preflight_runtime_hints,
    run_control,
    run_coordination_control,
    run_coordination_control_drain,
    run_coordination_mailbox_poll,
    run_coordination_terminal,
    run_persistence,
    run_reporting,
    verification_models,
    verification_transitions,
)


class ReadinessStateWriter(Protocol):
    """Writes a plan's readiness metadata, reporting failure rather than raising.

    Satisfied by ``readiness_gate.write_readiness_state``. It stays a seam
    rather than an import because the readiness gate left the runner *after*
    this module did, and because the dependency has to run this way round: the
    readiness gate pauses through :func:`_pause_run`, so an import back the
    other way would be a cycle.
    """

    def __call__(
        self,
        plan_dir: Path,
        new_state: plans_models.ReadinessState,
        *,
        expected_sha256: str | None = None,
    ) -> tuple[bool, str | None]: ...


class SessionStatusReverter(Protocol):
    """Puts an aborted session back to ``pending`` with a note saying why.

    Returns the session file it rewrote, or ``None`` when there was nothing to
    revert -- which is what tells the caller whether the plan snapshot it holds
    is now stale.
    """

    def __call__(
        self,
        snapshot: plans_models.PlanSnapshot,
        session_id: str,
        *,
        failure_note: str,
    ) -> Path | None: ...


#: Re-reads the plan after a status revert, returning the fallback snapshot
#: unchanged when nothing was reverted (the third argument is ``None``; any other
#: value -- a rewritten path or a ``handoff_guards.SessionRevert`` -- means the
#: plan changed). The most-shared primitive in the guard layer; declared here
#: because this is the first boundary to leave the runner that needs it.
SnapshotReinspector = Callable[
    [Path, plans_models.PlanSnapshot, object | None, plans_snapshot.PlanSnapshotCache],
    plans_models.PlanSnapshot,
]

#: Projects a review verdict already persisted in the plan directory onto the
#: run payload. Used by the stop that lands when only review work would follow,
#: so the run reports the verdict it has rather than none at all.
ExistingReviewVerdictRecorder = Callable[[dict[str, Any], Path], None]


def readiness_finalizer(
    write_readiness_state: ReadinessStateWriter,
) -> commit_settlement.RunFinalizer:
    """Bind the readiness writer into the finalizer ``_finish_run`` expects.

    The commit ledger takes the readiness close as a callback so it never has to
    import this boundary; this is the one place that binding is made, so the
    writer seam is threaded in a single spot rather than at every terminal path.
    """

    def finalize(payload: dict[str, Any]) -> None:
        from .run_termination_terminal import _finalize_readiness_lifecycle

        _finalize_readiness_lifecycle(
            payload, write_readiness_state=write_readiness_state
        )

    return finalize


#: The one ending that closes without an audit: a dry run rehearsed a loop and
#: produced no run to audit. Every other persisted ending is a supervisor
#: decision — a pause and an interruption most of all — and grading those is
#: what the audit exists for.
_UNAUDITED_TERMINAL_OUTCOMES = frozenset({"dry-run"})
