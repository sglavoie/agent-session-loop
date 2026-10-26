"""Reconcile failed turns, retire refused restores, and retry Spark failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    conversation_resume,
    durability,
    events,
    handoff_correction,
    handoff_guards,
    harness_failures,
    harnesses,
    model_availability,
    plans_models,
    plans_payloads,
    run_selection,
    session_verification,
    state,
    turn_disposition_models,
    turn_execution_commit,
)


def _record_spark_fallback(
    ctx: turn_disposition_models.RunContext,
    *,
    after: plans_models.PlanSnapshot,
    classification: harness_failures.FailureClassification,
    spark_route: dict[str, Any],
    spark_status_update: dict[str, Any],
    fallback_reason: str,
    session_id: str,
    turn_index: int,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
) -> None:
    """Commit the route change, the turn artifact, and the fallback event.

    All under one lock: the retry reads its model from the payload the next
    pass sees, so a reader that caught the route half-updated would launch the
    retry against the model that just failed.
    """
    with ctx.persistence.lock:
        spark_route["fallback_reason"] = fallback_reason
        turn_payload["spark_route"] = dict(spark_route)
        ctx.payload["spark"].update(spark_status_update)
        ctx.payload["spark"]["active_route"] = dict(spark_route)
        ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(after)
        durability.write_json_file(paths["result"], turn_payload, durable=True)
        ctx.persistence.append_event(
            ctx.event_path,
            "spark_fallback",
            {
                "run_id": ctx.run_id,
                "session_id": session_id,
                "failed_turn_index": turn_index,
                "failed_model": run_selection.SPARK_MODEL,
                "fallback_model": ctx.payload["spark"]["baseline"],
                "failure_classification": classification.to_dict(),
                "retry_at": ctx.payload["spark"].get("retry_at"),
            },
        )
        ctx.persistence.write_state(ctx.payload)


def _reconcile_unsettled_completion(
    ctx: turn_disposition_models.RunContext,
    *,
    after: plans_models.PlanSnapshot,
    selected_after: plans_models.SessionStatus | None,
    session_id: str,
    turn_index: int,
    result: harnesses.HarnessResult,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    reason: str = "unsettled-completion-after-nonzero-exit",
    failure_note: str | None = None,
) -> tuple[plans_models.PlanSnapshot, dict[str, Any] | None]:
    """Take back a completion claim a terminating turn never settled.

    A nonzero exit ends the turn ahead of verification and the handoff guards,
    so a session left ``complete`` in the after-image holds a claim nothing
    adjudicated. Publishing that image would make the unverified completion
    permanent across a resume and advance the plan's next pending session past
    work no check ever passed. The revert is the same one every guard refusal
    uses, so the work on disk is untouched and only the claim is withdrawn.

    ``reason`` and ``failure_note`` default to the nonzero-exit wording this was
    written for. The redirect branch supplies its own: the claim there is
    withdrawn because an operator's correction reached the turn before it
    settled, which is not a failure and must not be recorded as one.

    Returns the snapshot to publish and the reconciliation record, or the
    unchanged snapshot and ``None`` when the session claimed nothing.
    """
    if selected_after is None or selected_after.status != "complete":
        return after, None
    reverted = handoff_guards._revert_session_status_to_pending(
        after,
        session_id,
        failure_note=(
            failure_note
            if failure_note is not None
            else (
                f"harness exited with status {result.returncode} before this "
                "turn reached an accepted settlement; the completion claim is "
                "withdrawn and the work this session already wrote is left in "
                "place"
            )
        ),
        append_handoff_note=handoff_correction._append_handoff_note,
    )
    snapshot_after = session_verification._reinspect_after_revert(
        after.plan_dir, after, reverted, ctx.plan_cache
    )
    record = {
        "session_id": session_id,
        "claimed_status": "complete",
        "restored_status": "pending" if reverted is not None else "complete",
        "reason": reason,
        "returncode": result.returncode,
        "reverted": reverted is not None,
        "session_path": str(reverted.path) if reverted is not None else None,
    }
    with ctx.persistence.lock:
        turn_payload["completion_reconciliation"] = record
        durability.write_json_file(paths["result"], turn_payload, durable=True)
        if handoff_guards._reopened(reverted):
            ctx.persistence.append_event(
                ctx.event_path,
                events.SESSION_REOPENED_EVENT,
                {
                    "run_id": ctx.run_id,
                    "turn_index": turn_index,
                    **plans_payloads.session_identity_payload(session_id),
                    "from_status": "complete",
                    "to_status": "pending",
                    "guard": "turn-completion-reconciliation",
                    "reason": reason,
                    **turn_execution_commit.reopened_snapshot_identity(
                        plans_payloads.snapshot_payload(snapshot_after)
                    ),
                },
            )
    return snapshot_after, record


def settle_restore_refused(
    ctx: turn_disposition_models.RunContext,
    *,
    plan_state: Path,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    result: harnesses.HarnessResult,
    continuation: conversation_resume.TurnContinuation,
    session_id: str,
    session_label: str,
    turn_index: int,
    turn_payload: dict[str, Any],
    session_details: dict[str, Any],
    paths: dict[str, Path],
) -> None:
    """Block a conversation the provider will not restore, and pause for recovery.

    The turn asked to continue a native conversation and was told that history
    is gone. Classified as an ordinary failure this would pause with whatever
    :mod:`harness_failures` made of the provider's complaint, and -- worse --
    leave the ledger entry resumable, so every later launch would ask for the
    same dead conversation and fail the same way. The operator's documented
    recovery controls could not reach it, because those controls act on the
    ledger and nothing had told the ledger anything.

    So this does the one durable thing the refusal implies: retires the record
    as ``unusable``, which is the ledger's word for a conversation that is no
    longer a history. That single write buys both behaviours the operator needs.
    An ordinary launch afterwards selects ``retired``, which is already outside
    the states that may proceed, and stops for recovery. A launch carrying
    ``--fresh-conversation`` spends its one-use authorization, supersedes
    nothing -- there is nothing live left to supersede -- and opens a new
    conversation with no resume id, keeping the session's retained files and
    outstanding feedback and losing only the agent's accumulated context, which
    was already lost.

    Nothing ran on this turn, so there is no completion claim to reconcile: the
    refusal happens in the handshake, before any work is submitted.
    """
    detail = result.conversation_restore_refused or "the conversation was not restored"
    binding = continuation.binding
    if binding is None:
        # A turn that resumed something always has the binding it resumed
        # against, so this is a caller that reached here without one. Report it
        # rather than invent a binding to write a ledger record under.
        refusal = conversation_resume.RestoreRefusal(
            conversation=continuation.resume_conversation_id,
            detail=detail,
            error="restore was refused but the turn carried no conversation binding",
        )
    else:
        refusal = conversation_resume.refuse_unrestorable_conversation(
            plan_state,
            binding=binding,
            conversation=(
                continuation.resume_conversation_id or result.conversation_id
            ),
            detail=detail,
        )
    record = refusal.describe()
    with ctx.persistence.lock:
        turn_payload["conversation_restore_refused"] = record
        durability.write_json_file(paths["result"], turn_payload, durable=True)
        ctx.persistence.append_event(
            ctx.event_path,
            events.CONVERSATION_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "turn_index": turn_index,
                **plans_payloads.session_identity_payload(session_id),
                **record,
            },
        )
    # The before-image when the plan could not be re-read: nothing ran, so it is
    # also the after-image in every case that matters, and publishing a
    # half-known one would say the turn changed something it never started.
    ctx.record_final_snapshot(after if after is not None else before)
    ctx.pause(
        reason=conversation_resume.RECOVERY_PAUSE_REASON,
        message=(
            f"{session_label}: {conversation_resume.restore_refusal_message(refusal)}"
        ),
        details={
            **session_details,
            "returncode": result.returncode,
            "conversation_restore_refused": record,
            "conversation_continuation": continuation.describe(),
        },
    )


def settle_nonzero_exit(
    ctx: turn_disposition_models.RunContext,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot,
    result: harnesses.HarnessResult,
    spark_route: dict[str, Any],
    session_id: str,
    session_label: str,
    turn_index: int,
    turn_payload: dict[str, Any],
    session_details: dict[str, Any],
    paths: dict[str, Path],
    state_dir_arg: str | None,
    spark_run_suppression: str | None,
) -> turn_disposition_models.SparkRetry | None:
    """Classify a failed harness exit, retrying off Spark where that is safe.

    Returns the retry the fallback scheduled, or ``None`` when the run paused.
    A Spark turn that died on an exhausted quota or a rejected model is only
    retryable when it left no trace: the session still pending and the plan
    byte-identical to the before-image. Anything else may have half-completed,
    so it pauses and says why.
    """
    classification = harness_failures.classify_harness_failure(
        result, shutdown=turn_payload.get("harness_shutdown")
    )
    spark_failure = spark_route["decision"] == "spark" and classification.reason in {
        events.PAUSE_REASON_QUOTA_EXHAUSTED,
        events.PAUSE_REASON_INVALID_MODEL,
    }
    selected_after = next(
        (item for item in after.sessions if item.id == session_id),
        None,
    )
    safely_retryable = (
        selected_after is not None
        and selected_after.status == "pending"
        and after.content_sha256 == before.content_sha256
    )
    if spark_failure and safely_retryable:
        try:
            cooldown = model_availability.suppress_until(
                state.state_root(state_dir_arg),
                run_selection.SPARK_MODEL,
                reason=classification.reason,
                retry_hint=classification.retry_at,
                run_id=ctx.run_id,
            )
        except OSError as error:
            cooldown = None
            with ctx.persistence.lock:
                ctx.payload["spark"]["warning"] = (
                    f"could not update model availability ledger: {error}"
                )
        fallback_reason = classification.reason
        if cooldown is None:
            spark_run_suppression = (
                spark_run_suppression or f"{classification.reason}-for-current-run"
            )
            spark_status_update = {
                "available": False,
                "availability": False,
                "reason": spark_run_suppression,
                "retry_at": None,
            }
        else:
            spark_status_update = cooldown.to_dict()
        _record_spark_fallback(
            ctx,
            after=after,
            classification=classification,
            spark_route=spark_route,
            spark_status_update=spark_status_update,
            fallback_reason=fallback_reason,
            session_id=session_id,
            turn_index=turn_index,
            turn_payload=turn_payload,
            paths=paths,
        )
        return turn_disposition_models.SparkRetry(
            snapshot=after,
            fallback_reason=fallback_reason,
            run_suppression=spark_run_suppression,
        )
    if classification.reason == events.PAUSE_REASON_NONZERO_EXIT:
        pause_message = (
            f"harness exited with status {result.returncode} for {session_label}"
        )
    else:
        pause_message = (
            f"{session_label} failed: {classification.reason} "
            f"(harness exit {result.returncode})"
        )
    # Before the snapshot is published, not after: the final image this run
    # ends on is the reconciled one, so a resume selects the session the turn
    # actually left unfinished rather than the one it claimed to finish.
    reconciled, reconciliation = _reconcile_unsettled_completion(
        ctx,
        after=after,
        selected_after=selected_after,
        session_id=session_id,
        turn_index=turn_index,
        result=result,
        turn_payload=turn_payload,
        paths=paths,
    )
    ctx.record_final_snapshot(reconciled)
    ctx.pause(
        reason=classification.reason,
        message=pause_message,
        details={
            **session_details,
            "returncode": result.returncode,
            "failure_classification": classification.to_dict(),
            **(
                {"spark_fallback_unsafe": True}
                if spark_failure and not safely_retryable
                else {}
            ),
            **(
                {"completion_reconciliation": reconciliation}
                if reconciliation is not None
                else {}
            ),
        },
    )
    return None
