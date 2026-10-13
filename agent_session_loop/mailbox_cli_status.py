"""Run status, ownership and supervisor-lease helpers for mailbox commands."""

from __future__ import annotations

import argparse
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    cli_common,
    intervention,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_output,
    mailbox_models,
    mailbox_vocabulary,
    plans_snapshot,
    state_status,
    supervision_liveness,
)


def supervision_run_evidence(
    plan_dir: str | Path,
    state_dir: str | Path | None,
) -> dict[str, Any] | None:
    """Return what the plan's own state says about a live run, or None.

    None means the question could not be answered, never that no run exists:
    a status read that fails must not be allowed to manufacture a death.
    """
    try:
        status = state_status.status_payload(
            plans_snapshot.inspect_plan(plan_dir), state_dir
        )
    except Exception:  # noqa: BLE001 - unreadable status is unprovable, not dead
        return None
    lock = status.get("lock")
    lock = lock if isinstance(lock, dict) else {}
    runner_state = status.get("runner_state")
    runner_state = runner_state if isinstance(runner_state, dict) else {}
    counts = status.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    active_turn = status.get("active_turn")
    return {
        "lock_active": bool(lock.get("active")),
        "lock_pid": lock.get("pid"),
        "run_id": runner_state.get("run_id"),
        "runner_state_fresh": bool(
            runner_state.get("present")
            and not runner_state.get("error")
            and not runner_state.get("run_finished_at")
            and not runner_state.get("stale_unfinished")
        ),
        "active_turn": (
            {
                "session_id": active_turn.get("session_id"),
                "idle_seconds": active_turn.get("idle_seconds"),
            }
            if isinstance(active_turn, dict)
            else None
        ),
        "sessions_complete": counts.get("complete", 0),
        "sessions_total": sum(
            value for value in counts.values() if isinstance(value, int)
        ),
    }


def _run_evidence_is_live(evidence: dict[str, Any] | None) -> bool:
    if evidence is None:
        return True
    return bool(evidence["lock_active"] or evidence["runner_state_fresh"])


def supervision_liveness_probe(
    plan_dir: str | Path,
    state_dir: str | Path | None,
    supervision_id: str,
    *,
    first_seen_at: datetime,
) -> Callable[[], str | None]:
    """Build the check that decides whether this launch's helper is gone.

    Two questions, in the only order that is safe. A record that exists is
    authoritative the moment ``ps`` disowns its process, so that verdict never
    waits out the grace window. A record that does not exist proves nothing on
    its own — a plain ``asl run`` supervised without the helper writes none —
    so it becomes a death only once the grace window has passed and the plan's
    own state shows no run holding it either.
    """

    def probe() -> str | None:
        record = supervision_liveness.read_record(
            plan_dir,
            supervision_id,
            state_dir=state_dir,
        )
        if record is not None:
            if supervision_liveness.probe(record) == supervision_liveness.DEAD:
                return mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED
            return None
        elapsed = (datetime.now(UTC) - first_seen_at).total_seconds()
        if elapsed < mailbox_cli_constants.SUPERVISE_LIVENESS_GRACE_SECONDS:
            return None
        if _run_evidence_is_live(supervision_run_evidence(plan_dir, state_dir)):
            return None
        return mailbox_cli_constants.SUPERVISE_LOST_HELPER_NEVER_STARTED

    return probe


def write_supervisor_lease(
    plan_dir: str | Path,
    state_dir: str | Path | None,
    supervisor_id: str | None = None,
) -> mailbox_models.MailboxLease:
    """Heartbeat a live supervisor's lease, or take over when none is alive."""
    return intervention.write_supervisor_lease(plan_dir, state_dir, supervisor_id)


def supervisor_write(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    append: Callable[[Path], mailbox_models.MailboxMessage],
    before_lease: Callable[[cli_common.PlanCommandContext], None] | None = None,
) -> tuple[cli_common.PlanCommandContext, mailbox_models.MailboxMessage, str] | None:
    context = mailbox_cli_args.resolve(
        args,
        command_name=command_name,
        text_label=text_label,
    )
    if context is None:
        return None
    if before_lease is not None:
        # Optional evidence stays fail-open.
        with contextlib.suppress(Exception):
            before_lease(context)
    try:
        # Establish the lease first.  If the state root is unwritable, this
        # avoids appending a command the caller may retry after seeing failure.
        # The reported supervisor_id comes from the lease that actually landed
        # on disk: the live supervisor's on a heartbeat, ours on a takeover.
        lease = write_supervisor_lease(
            context.snapshot.plan_dir,
            args.state_dir,
        )
        message = append(context.snapshot.plan_dir)
    except mailbox_models.MailboxError as error:
        mailbox_cli_output.print_mailbox_error(
            args,
            command_name=command_name,
            text_label=text_label,
            error=error,
        )
        return None
    return context, message, lease.supervisor_id


def _status_run_id(context: cli_common.PlanCommandContext) -> str | None:
    """Return the run a worker question belongs to, or None when unattributable.

    A torn or absent state read degrades to an unattributed question, which is
    never counted as open. That is the safe direction: it under-reports a live
    block instead of resurrecting one after the run ends.
    """
    runner_state = context.status.get("runner_state")
    if not isinstance(runner_state, dict):
        return None
    run_id = runner_state.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def status_question_ownership(
    context: cli_common.PlanCommandContext,
) -> tuple[
    str | None,
    str | None,
    int | None,
    mailbox_vocabulary.WaiterOrigin | None,
]:
    """Return exact live-turn ownership when a worker ask can prove it.

    Historical callers could append a question with only the best-effort run
    identity. Preserve that compatibility fallback, but stamp the complete
    ownership tuple whenever the status snapshot exposes the active harness
    turn. The runner relies on the complete tuple to correlate a worker's
    answerless pause with the turn that subsequently emits ``RUNNER_PAUSE``.
    """
    run_id = _status_run_id(context)
    runner_state = context.status.get("runner_state")
    if run_id is None or not isinstance(runner_state, dict):
        return run_id, None, None, None
    active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        return run_id, None, None, None
    session_id = active_turn.get("session_id")
    turn_index = active_turn.get("turn_index")
    if (
        active_turn.get("run_id") != run_id
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(turn_index, bool)
        or not isinstance(turn_index, int)
        or turn_index < 1
    ):
        return run_id, None, None, None
    return (
        run_id,
        session_id,
        turn_index,
        mailbox_vocabulary.WAITER_ORIGIN_WORKER,
    )


def active_status_run_id(
    context: cli_common.PlanCommandContext,
) -> str | None:
    """Return the authoritative live run id from one status projection."""
    runner_state = context.status.get("runner_state")
    lock = context.status.get("lock")
    if not isinstance(runner_state, dict) or not isinstance(lock, dict):
        return None
    run_id = runner_state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    if (
        not runner_state.get("present")
        or runner_state.get("error")
        or runner_state.get("paused")
        or runner_state.get("run_finished_at")
        or not lock.get("active")
    ):
        return None
    return run_id


def active_turn_ownership(
    context: cli_common.PlanCommandContext,
) -> tuple[str, str, int] | None:
    """Return exact ownership for the active worker turn, if authoritative."""
    run_id = active_status_run_id(context)
    runner_state = context.status.get("runner_state")
    if run_id is None or not isinstance(runner_state, dict):
        return None
    active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        return None
    session_id = active_turn.get("session_id")
    turn_index = active_turn.get("turn_index")
    if (
        active_turn.get("run_id") != run_id
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(turn_index, bool)
        or not isinstance(turn_index, int)
        or turn_index < 1
    ):
        return None
    return run_id, session_id, turn_index


def active_turn_harness(
    context: cli_common.PlanCommandContext,
) -> str | None:
    """Return the harness running the active worker turn, if it is named."""
    runner_state = context.status.get("runner_state")
    if not isinstance(runner_state, dict):
        return None
    active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        return None
    harness = active_turn.get("harness")
    return harness if isinstance(harness, str) and harness else None
