"""Publication and rejection: the two terminal transitions of a turn.

:func:`commit_turn` is the only emitter of ``turn_finished``; it writes
``result.json`` and the run state before the event so the event can never
name a result that disagrees with what is on disk."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from . import (
    durability,
    events,
    harnesses,
    plans_errors,
    plans_models,
    plans_payloads,
    plans_snapshot,
    turn_execution_artifacts,
    turn_execution_protocols,
)

if TYPE_CHECKING:
    from .handoff_guards import SessionRevert


@dataclass(frozen=True)
class TurnFinalization:
    """What the runner knows once the harness process for a turn has exited.

    This owns the execution half of a turn's ending: the raw harness result,
    the plan re-read that follows it, and the artifacts written from both. It
    exposes no way to announce accepted completion, because at this point
    nothing has been accepted — every guard that could still reject the turn
    runs after the object is built.
    """

    result: harnesses.HarnessResult
    after: plans_models.PlanSnapshot | None
    plan_error: str | None
    turn_payload: dict[str, Any]


def finalize_turn_execution(
    *,
    event_path: Path,
    persistence: turn_execution_protocols.TurnPersistence,
    plan_cache: plans_snapshot.PlanSnapshotCache,
    result: harnesses.HarnessResult,
    before: plans_models.PlanSnapshot,
    paths: dict[str, Path],
    run_id: str,
    turn_index: int,
    session_id: str,
    live_turn: dict[str, Any],
    unredacted_prompt: str | None = None,
    pending_exception_guard: MutableMapping[str, Any] | None = None,
) -> TurnFinalization:
    """Collect a finished harness process into a :class:`TurnFinalization`.

    The plan is re-read here because the harness edits it in place, and a
    handoff that no longer parses is carried as ``plan_error`` rather than
    raised: the run loop decides what an unreadable plan means. The
    ``turn_execution_finished`` event is appended last so it never names
    artifacts that are not on disk yet.
    """
    plan_error: str | None = None
    after: plans_models.PlanSnapshot | None
    try:
        after = plan_cache.get_fresh(before.plan_dir)
    except plans_errors.PlanError as error:
        after = None
        plan_error = str(error)
    if pending_exception_guard is not None:
        pending_exception_guard["after"] = after
    turn_payload = turn_execution_artifacts.write_turn_artifacts(
        paths=paths,
        result=result,
        before=before,
        after=after,
        run_id=run_id,
        turn_index=turn_index,
        session_id=session_id,
        live_turn=live_turn,
        plan_error=plan_error,
        unredacted_prompt=unredacted_prompt,
    )
    if pending_exception_guard is not None:
        pending_exception_guard["turn_payload"] = turn_payload
    execution_payload: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        **plans_payloads.session_identity_payload(session_id),
        "harness": result.name,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "aborted": result.aborted,
        "started_at": live_turn["started_at"],
        "finished_at": live_turn["finished_at"],
        "duration_seconds": live_turn["duration_seconds"],
        "artifact_dir": str(paths["artifact_dir"]),
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "result_path": str(paths["result"]),
        "plan_readable": after is not None,
    }
    if result.stream_errors:
        execution_payload["stream_errors"] = list(result.stream_errors)
    if result.stream_io is not None:
        execution_payload["stream_io"] = dict(result.stream_io)
    if "harness_shutdown" in live_turn:
        execution_payload["harness_shutdown"] = live_turn["harness_shutdown"]
    if plan_error is not None:
        execution_payload["plan_error"] = plan_error
    persistence.append_event(
        event_path,
        events.TURN_EXECUTION_FINISHED_EVENT,
        execution_payload,
    )
    return TurnFinalization(
        result=result,
        after=after,
        plan_error=plan_error,
        turn_payload=turn_payload,
    )


def replace_turn_record(payload: dict[str, Any], committed: dict[str, Any]) -> None:
    """Swap a turn's in-flight state record for its committed form."""
    turns = payload.setdefault("turns", [])
    turn_index = committed.get("turn_index")
    for position, record in enumerate(turns):
        if isinstance(record, Mapping) and record.get("turn_index") == turn_index:
            turns[position] = committed
            return
    turns.append(committed)


class TurnCommitRejected(Exception):
    """The plan stopped agreeing with the accepted snapshot before publication."""

    def __init__(self, *, reason: str, message: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


def publication_snapshot(
    plan_cache: plans_snapshot.PlanSnapshotCache,
    accepted: plans_models.PlanSnapshot,
) -> plans_models.PlanSnapshot:
    """Re-read the plan so the published snapshot describes it as it now is.

    Runner bookkeeping between the last guard and the commit point — the
    readiness refresh above all — rewrites plan files, so ``accepted`` was
    hashed before the plan reached the state the turn is about to be published
    against. The statuses must not have moved: the guards passed on
    ``accepted``, so a different status mapping is drift rather than
    bookkeeping, and the turn is refused instead of silently republished.
    """
    try:
        recomputed = plan_cache.get_fresh(accepted.plan_dir)
    except plans_errors.PlanError as error:
        raise TurnCommitRejected(
            reason="malformed-plan",
            message="the plan could not be re-read before publishing the turn",
            detail={"error": str(error)},
        ) from error
    if plans_payloads.status_map(recomputed) != plans_payloads.status_map(accepted):
        raise TurnCommitRejected(
            reason="unexpected-status-change",
            message=(
                "session statuses moved between acceptance and publication of the turn"
            ),
            detail={
                "accepted_content_sha256": accepted.content_sha256,
                "recomputed_content_sha256": recomputed.content_sha256,
                "status_changes": list(
                    plans_payloads.status_changes(accepted, recomputed)
                ),
            },
        )
    return recomputed


@dataclass(frozen=True)
class TurnCommit:
    """The published statement that a turn was accepted, and what it published.

    ``record`` is the turn record as operators and audits will read it; it is
    read-only because amending a published turn would rewrite a statement
    already consumed. ``published`` is the plan snapshot that record describes,
    carried beside it so a caller updating the run's final snapshot uses the
    same read the publication was built from rather than a later one.
    """

    published: plans_models.PlanSnapshot
    record: Mapping[str, Any]


def commit_turn(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: turn_execution_protocols.TurnPersistence,
    progress: turn_execution_protocols.TurnProgress,
    turn_payload: dict[str, Any],
    live_turn: dict[str, Any],
    accepted: plans_models.PlanSnapshot,
    plan_cache: plans_snapshot.PlanSnapshotCache,
    paths: dict[str, Path],
) -> TurnCommit:
    """Publish the single immutable statement that a turn was accepted.

    This is the only place ``turn_finished`` is emitted. Every guard able to
    reject the turn, and every runner-owned plan mutation the turn's result
    describes, has already run, so the published snapshot is recomputed here,
    once, from a fresh read — the guards passed on ``accepted``, but the plan
    on disk has moved on since it was hashed. ``result.json`` and the run state
    are written first so the event can never name a result that disagrees with
    them, and downstream consumers hear about the turn only once that event is
    durable.

    The re-read happens before the persistence lock is entered, and this
    function never takes ``LiveTurn._lock``, so lock ordering is unchanged.

    The returned record is read-only: a later phase that tried to amend a
    published turn would be rewriting a statement operators and audits have
    already read.
    """
    published = publication_snapshot(plan_cache, accepted)
    committed = dict(turn_payload)
    committed["after"] = plans_payloads.snapshot_payload(published)
    committed["committed"] = True
    with persistence.lock:
        durability.write_json_file(paths["result"], committed, durable=True)
        replace_turn_record(payload, committed)
        persistence.write_state(payload)
        persistence.append_event(event_path, events.TURN_FINISHED_EVENT, committed)
    progress.turn_finished(committed)
    turn_execution_protocols.notify_turn_finished(payload, live_turn, persistence)
    return TurnCommit(published=published, record=MappingProxyType(committed))


def reopened_snapshot_identity(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The projection a status reversion left behind.

    A reversion that names only the transition leaves a replaying consumer
    holding a state it cannot check; the digest and counts of the plan as
    reinspected after the rewrite are what make the replay land on the run's
    own authoritative projection.
    """

    return {
        "content_sha256": snapshot.get("content_sha256"),
        "counts": snapshot.get("counts"),
    }


def record_turn_rejected(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: turn_execution_protocols.TurnPersistence,
    turn_payload: Mapping[str, Any],
    session_id: str,
    guard: str,
    reason: str,
    reopened: bool = False,
    revert: SessionRevert | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Record that a post-execution guard refused an already-executed turn.

    A guard that reverted the session passes ``revert``; it then decides
    ``reopened`` and the ``from_status`` on ``session_reopened``, so a session
    that was already pending gets its failure note but no reopening (ASI-30).

    Without this the only trace of a refusal is a pause reason plus a plan that
    silently disagrees with the last turn record. ``session_reopened`` is
    appended beside it when the guard also returned the session to pending, so
    the reverse transition the plan made is replayable from the event stream.

    That event carries the plan digest and status counts the revert produced, so
    a consumer replaying the stream lands on the same projection the run
    reports rather than on a state it has to trust the plan file to confirm.
    Every reopening guard refreshes ``final_snapshot`` from the reinspected plan
    before recording the rejection, so it is the resulting snapshot by
    construction.
    """
    from_status = "complete"
    if revert is not None:
        reopened = revert.reopened
        from_status = revert.from_status or from_status
    rejection: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "turn_index": turn_payload.get("turn_index"),
        **plans_payloads.session_identity_payload(session_id),
        "guard": guard,
        "reason": reason,
        "reopened": reopened,
    }
    if detail:
        rejection["detail"] = dict(detail)
    persistence.append_event(event_path, events.TURN_REJECTED_EVENT, rejection)
    if not reopened:
        return
    final_snapshot = payload.get("final_snapshot")
    persistence.append_event(
        event_path,
        events.SESSION_REOPENED_EVENT,
        {
            "run_id": payload.get("run_id"),
            "turn_index": turn_payload.get("turn_index"),
            **plans_payloads.session_identity_payload(session_id),
            "from_status": from_status,
            "to_status": "pending",
            "guard": guard,
            "reason": reason,
            **reopened_snapshot_identity(
                final_snapshot if isinstance(final_snapshot, Mapping) else {}
            ),
        },
    )
