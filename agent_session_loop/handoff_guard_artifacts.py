"""Classify terminating turns and enforce plan-artifact integrity."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    durability,
    events,
    harnesses,
    plans_content_hash,
    plans_models,
    plans_payloads,
    plans_snapshot,
    redirect_continuation,
    run_persistence,
    run_termination,
    turn_execution_commit,
)
from .handoff_guard_sessions import (
    HandoffNoteAppender,
    _reopened,
    _revert_session_status_to_pending,
)


def _terminating_result_disposition(
    result: harnesses.HarnessResult,
    after: plans_models.PlanSnapshot | None,
    turn_payload: Mapping[str, Any],
) -> str | None:
    """Name the result branch that ends this turn before the status guards.

    The dispatch selects its terminating branch by the name returned here, so a
    branch this function cannot name is unreachable and a new one has to be
    added here to exist at all. ``None`` means the turn reaches the status
    guards, which own the sharper diagnosis for a turn that moved the wrong
    session.
    """
    if result.interrupted:
        return "interrupted"
    if redirect_continuation.turn_was_redirected(turn_payload):
        # Ahead of every result flag but the operator's own Ctrl-C, and decided
        # from the acceptance rather than from the exit. A redirect arms the
        # same abort signal an `abort-turn` does, so `aborted` and a 137 exit
        # describe both; and a worker that finished inside the grace window
        # sets no flag at all while still owing the operator the attempt their
        # correction was promised. The acceptance is the only fact that
        # separates the three, which is why it is what this reads.
        return redirect_continuation.DISPOSITION_REDIRECTED
    if result.timed_out:
        return "timeout"
    if result.aborted:
        return "aborted"
    if result.conversation_restore_refused is not None:
        # Ahead of the plan snapshot and the exit code, because this says
        # something the ledger has to be told whatever else is wrong: the
        # conversation this turn was pointed at no longer exists on the
        # provider's side. Read as a plain nonzero exit it would be diagnosed as
        # a harness that failed, and the dead conversation would stay resumable
        # for every launch after this one.
        return "restore-refused"
    if after is None:
        return "malformed-plan"
    if turn_payload.get("runner_pause") is not None:
        return "runner-pause"
    if result.returncode != 0:
        return "nonzero-exit"
    return None


def _enforce_plan_artifact_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    digests_before: Mapping[str, str],
    runner_written: tuple[str, ...],
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    append_handoff_note: HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
    result_disposition: str | None = None,
    adjudicate_only: bool = False,
) -> bool:
    """Refuse a turn that changed a session artifact it did not select.

    A task packet fixes exactly one writable session file, so every other
    `session-<id>.md` and `session-<id>.expand.md` must survive the turn
    byte-for-byte. ``result_disposition`` names the result branch that would
    otherwise have ended the turn: those branches run this guard first, because
    a retryable pause or a mid-turn ``continue`` would relaunch against the
    tampered packet and adopt it as the new baseline. Returns ``True`` when the
    run must pause (the caller must ``break``).

    ``adjudicate_only`` is for a turn an earlier finalization guard already
    ended, and it exists for the same reason the protected-path guard has one:
    the two guards answer different questions -- "did the session stay inside
    its declared write scope" and "did it rewrite a packet it was never given"
    -- and a turn that breached both has to report both. Without it a write
    guard that paused first left the changed packets with no verdict at all, so
    they stayed in the plan unadjudicated (ASI-119). The verdict is reached and
    recorded, but the outcome already decided is left standing: no second
    revert, and no pause that would displace the reason an operator has to act
    on.
    """
    digests_after = plans_content_hash.session_artifact_digests(
        before.plan_dir, exclude_session_id=session_id
    )
    baseline = dict(digests_before)
    for relative_name in runner_written:
        # The runner's own bounded review-handoff normalization runs after the
        # harness, so re-baseline what it wrote rather than blame the worker.
        if relative_name in digests_after:
            baseline[relative_name] = digests_after[relative_name]
        else:
            baseline.pop(relative_name, None)
    changed_artifacts = sorted(
        name
        for name in set(baseline) | set(digests_after)
        if baseline.get(name) != digests_after.get(name)
    )
    disposition = (
        {} if result_disposition is None else {"result_disposition": result_disposition}
    )
    adjudication = {"adjudicated_only": True} if adjudicate_only else {}
    if not changed_artifacts:
        with persistence.lock:
            turn_payload["plan_artifact_guard"] = {
                "passed": True,
                "artifact_count": len(digests_after),
                "runner_written": list(runner_written),
                **disposition,
                **adjudication,
            }
            durability.write_json_file(paths["result"], turn_payload)
        return False

    reason = events.PAUSE_REASON_PLAN_ARTIFACT_TAMPERED
    evidence = {
        "changed_artifacts": changed_artifacts,
        "digests_before": baseline,
        "digests_after": digests_after,
        "runner_written": list(runner_written),
        **disposition,
    }
    if adjudicate_only:
        # The turn is already ending, and whatever ended it first already
        # withdrew the session if it needed withdrawing. Reverting again would
        # append a second failure note for one turn.
        adjudicated_record: dict[str, Any] = {
            "passed": False,
            "reason": reason,
            "reverted_to_pending": False,
            **evidence,
            **adjudication,
        }
        primary_reason = payload.get("pause_reason")
        if payload.get("paused") and isinstance(primary_reason, str):
            adjudicated_record["deferred_to_pause_reason"] = primary_reason
        with persistence.lock:
            turn_payload["plan_artifact_guard"] = adjudicated_record
            durability.write_json_file(paths["result"], turn_payload)
            # The primary pause was published before this verdict existed, so
            # its live details are amended in place -- ``payload["details"]`` is
            # the same object -- rather than republished under a reason that
            # would displace the one an operator has to act on.
            pause_details = payload.get("pause_details")
            if payload.get("paused") and isinstance(pause_details, dict):
                pause_details["plan_artifact_guard"] = adjudicated_record
        turn_execution_commit.record_turn_rejected(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="plan-artifact-guard",
            reason=reason,
            reopened=False,
            detail={
                "adjudicated_only": True,
                "changed_artifacts": changed_artifacts,
            },
        )
        return False

    # Leaving the session complete would make the tampered packet permanent
    # across a resume, so a failed guard always puts it back to pending.
    revert_target = after if after is not None else before
    reverted = _revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=(
            "plan artifact guard failed: session artifacts outside the "
            "selected session changed during the turn"
        ),
        append_handoff_note=append_handoff_note,
    )
    snapshot_after = reinspect(before.plan_dir, revert_target, reverted, plan_cache)
    guard_record = {
        "passed": False,
        "reason": reason,
        "reverted_to_pending": _reopened(reverted),
        **evidence,
    }
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot_after)
        turn_payload["plan_artifact_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution_commit.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="plan-artifact-guard",
        reason=reason,
        revert=reverted,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=(
            f"{session_label} changed session artifacts it did not select: "
            + ", ".join(changed_artifacts)
        ),
        details={
            **session_details,
            **evidence,
            "plan_artifact_guard": guard_record,
        },
        persistence=persistence,
    )
    return True
