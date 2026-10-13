"""The run context: the loop's memory across passes."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    commit_settlement,
    continuation_guards,
    guards,
    plans_models,
    plans_payloads,
    plans_snapshot,
    redirect_recovery,
    run_commit,
    run_control,
    run_coordination_reply_state,
    run_coordination_terminal,
    run_persistence,
    run_reporting,
    run_termination,
    verification,
)

# Registered implementation turns are enabled only because every such turn is
# wrapped by the authoritative workspace snapshot guard below. Registered
# review turns remain fail-closed until the combined review guard in Session 13.
MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED = True


@dataclasses.dataclass(kw_only=True)
class RunContext:
    """One locked run's own state, grouped by how long each value lives.

    ``run_locked`` is a single function with roughly eighty locals, and the
    helpers it owns used to reach them as closure captures. A capture is
    invisible at the call site and unsliceable: a helper that reads a loop
    local cannot leave the function that binds it. This object makes that
    dependency explicit so each helper states what it needs.

    The groups below are the run's three clocks. Run-start fields are bound
    before the first helper runs and never rebound; loop-pass fields advance
    once per pass around the session loop; turn fields advance once per
    harness turn. Nothing here is durable on its own: every write still goes
    through :attr:`persistence`, and the object never outlives the run lock.
    """

    # Fixed at run start. `plan_dir` is the canonicalized directory every
    # snapshot of this run shares, so it stays here rather than being re-read
    # off whichever snapshot the loop currently holds.
    run_id: str
    plan_dir: Path
    run_dir: Path
    event_path: Path
    payload: dict[str, Any]
    persistence: run_persistence._RunPersistence
    progress: run_reporting._ProgressReporter
    inline_tail: run_reporting._InlineTailController
    reply_state_preserver: run_coordination_reply_state.ReplyStatePreserver
    audit_context: run_coordination_terminal.TerminalAuditContext
    guard_controller: guards.GuardController
    plan_cache: plans_snapshot.PlanSnapshotCache
    verification_context: verification.VerificationContext
    commit_generation: run_commit._CommitGeneration
    #: What each session's earlier attempts in *this* run were seen to change.
    #: Bound at run start rather than per session because the loop re-attempts
    #: a session across several passes and the ledger has to outlive each of
    #: them; it is keyed by session id and retired when a session completes.
    session_attempts: continuation_guards.SessionAttempts = dataclasses.field(
        default_factory=continuation_guards.SessionAttempts
    )
    #: Read off the payload once the readiness gate has passed, because the
    #: gate is what settles which control block the rest of the run answers to.
    control_payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    #: Wired once the control listener exists, because the listener is built
    #: from the two handlers that already take this context.
    run_controls: run_control.RunControlListener | None = None
    #: What a crashed predecessor left unfinished in the redirect journal,
    #: settled once under this run's lock before any turn is selected. Held for
    #: the whole run because the session a stranded correction belongs to may
    #: not be the first one this run selects.
    redirect_recovery_report: redirect_recovery.ReconciliationReport | None = None

    # Updated once per loop pass: the counters and flags that decide whether
    # the next pass runs, and the readiness gate's latest verdict.
    completed_count: int = 0
    #: Stamped when the runner stops working on a turn, not when the harness
    #: exits: verification and finalization are runner-owned work, so counting
    #: them as idle time would report the previous turn's own cost as a gap.
    runner_work_finished_at: datetime | None = None
    first_loop_pass: bool = True
    idle_baseline_pinned: bool = False
    commit_at_end_attempted: bool = False
    review_appended: bool = False
    gate_payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    gate_passed: bool = False
    core_skipped: bool = False

    # Updated once per turn.
    run_control_context: dict[str, Any] | None = None
    last_session_details: dict[str, Any] = dataclasses.field(default_factory=dict)
    seen_external_command_ids: set[str] = dataclasses.field(default_factory=set)
    #: The group this run still owes, carried between commit boundaries. A
    #: commit failure soft-fails, so this is what makes the failure retryable
    #: inside the same run instead of only after a restart.
    owed_commit_group: dict[str, Any] | None = None
    #: Retries already spent per group, so a repository that fails the same way
    #: every time cannot turn the run loop into a commit loop.
    owed_commit_group_attempts: dict[str, int] = dataclasses.field(default_factory=dict)
    #: The group that spent its whole retry budget without settling, armed by
    #: :func:`_soft_fail_commit` and read at the loop's selection boundary.
    #: ``None`` means nothing durable is owed past its budget, which is the
    #: only state in which this run may start another session.
    unsettled_commit_group: dict[str, Any] | None = None
    #: The turn whose filesystem changes a pause would leave behind, armed
    #: only once that turn has cleared the post-turn write guards. A pause is
    #: not a place the loop can rebuild a turn's identity from, so the turn
    #: hands it over while it still exists; ``None`` past the loop means no
    #: turn ever got far enough to retain work anyone may commit.
    retained_work: commit_settlement.RetainedWorkContext | None = None

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None:
        """Publish the plan image this run ends on, under the write lock."""
        with self.persistence.lock:
            self.payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        """Stop this run with a reason, through the one pause coordinator.

        Every pause site named the same three run-start values -- the event
        path, the payload, the persistence seam -- and differed only in the
        reason it was pausing for. Those three now live here, so a caller
        states its reason and nothing else.
        """
        run_termination._pause_run(
            self.event_path,
            self.payload,
            reason=reason,
            message=message,
            details=details,
            persistence=self.persistence,
        )
