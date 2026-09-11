"""What a completed turn has to survive before its handoff is believed.

A worker's turn ends by claiming something: this session is complete, here is
the handoff. That claim is not taken at face value. Between the harness
returning and the run recording the work, three guards ask whether the claim
was earned -- whether a blocking mailbox question was really answered, whether
the review session touched only the plan directory, and whether the turn
rewrote a session artifact it was never given. Only a turn that clears all
three reaches the progress journal.

This module owns that stretch of the turn, and the two writes at either end of
it: :func:`_append_progress_entry`, the one durable append to the plan's
``progress.md``, and :func:`_revert_session_status_to_pending`, the one way a
session's ``complete`` claim is taken back.

Durable records this boundary writes:

* ``progress.md`` -- one row per verified-complete session, appended by
  :func:`_append_progress_entry`. The append is idempotent: a repeat call for
  a ``(session_id, completed_at)`` pair the journal already ends with is a
  no-op returning ``False``, so a resumed run cannot double-write the row it
  crashed after writing.
* ``session-<id>.md`` -- rewritten in place by
  :func:`_revert_session_status_to_pending`, which puts the session's
  ``- **Status**:`` line back to ``pending`` and appends a note under
  ``- **Notes**`` saying which guard refused it. Nothing else here rewrites a
  session file.
* ``turn_payload["mailbox_completion_guard"]``,
  ``turn_payload["review_mutation_guard"]``, ``turn_payload["review_repo_state"]``
  and ``turn_payload["plan_artifact_guard"]`` -- each guard's verdict, passed
  or failed, written to the turn's result file under the run's mutex.
* ``payload["final_snapshot"]`` -- refreshed from the re-read plan whenever a
  guard reverted a session, so the published snapshot never describes the plan
  as it was before the revert.
* One ``turn-rejected`` event and one ``run-paused`` event per failed guard,
  through ``turn_execution.record_turn_rejected`` and
  ``run_termination._pause_run``.

Nothing here imports the runner. The event path, the payload, the persistence
handle, the plan snapshots, the snapshot cache and the resolved workspace all
enter as parameters. The two collaborators the caller supplies enter as seams:
the handoff-note appender, through :class:`HandoffNoteAppender`, which
``handoff_correction._append_handoff_note`` satisfies; and the post-revert plan
re-read, through ``run_termination.SnapshotReinspector``, which Session 15
declared for exactly this shape and
``session_verification._reinspect_after_revert`` satisfies.
Both are required keyword-only arguments, never defaulted, so a guard cannot
silently skip the note or the re-read by omitting them.

Invariants that survive the move:

* **Verify, then progress, then ledgers.** The runner's own docstring promises
  that ordering and :func:`_append_progress_entry` is the middle step of it.
  Only a verified-complete handoff reaches the journal, and the journal write
  happens before the derived status ledgers are re-synced, so a ledger can
  never describe a session the journal has not recorded.
* **A failed guard always reverts.** Leaving a session ``complete`` after a
  guard refused it would make the unguarded work permanent across a resume, so
  every refusal calls :func:`_revert_session_status_to_pending` before it
  pauses, and re-reads the plan afterwards rather than publishing the
  pre-revert snapshot.
* **The guard runs before the branch that would end the turn.**
  :func:`_terminating_result_disposition` names the result branch that would
  otherwise end the turn, and the plan-artifact guard runs first on those
  paths: a retryable pause or a mid-turn continue would otherwise relaunch
  against the tampered packet and adopt it as the new baseline.
* **A place you did not look is not evidence of no change.**
  :func:`_capture_legacy_implementation_snapshot` fingerprints exactly one
  repository, so :func:`_legacy_unobserved_repositories` re-derives every
  other repository the turn reached and reports it through the guard's
  fail-closed ``unavailable`` lane rather than letting silence read as a pass.
* **Registered repositories only.**
  :func:`_capture_workspace_write_guard_snapshot` fingerprints exactly the
  repositories the plan registered, so a concurrent run in a sibling worktree
  never appears as evidence about this turn.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import (
    durability,
    events,
    harnesses,
    mailbox,
    plans,
    redirect_continuation,
    repository_evidence,
    repository_intent,
    review,
    review_guard,
    run_persistence,
    run_termination,
    turn_execution,
    workspaces,
)

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class HandoffNoteAppender(Protocol):
    """Inserts one note bullet under a session handoff's ``- **Notes**`` list.

    Takes the session file's lines and the bounds of its handoff section, and
    returns the rewritten lines; it never touches the filesystem, so the caller
    stays the only writer. The concrete implementation is
    ``handoff_correction._append_handoff_note``, which this module cannot import:
    ``handoff_correction`` imports *this* module for the revert, so the edge only
    runs one way and the seam is what keeps it acyclic.
    """

    def __call__(
        self,
        lines: list[str],
        *,
        handoff_index: int,
        section_end: int,
        note: str,
    ) -> list[str]: ...


def session_status_reverter(
    append_handoff_note: HandoffNoteAppender,
) -> run_termination.SessionStatusReverter:
    """Bind the note appender into the reverter ``run_termination`` expects.

    ``SessionStatusReverter`` fixes the argument list at ``(snapshot,
    session_id, *, failure_note)``, leaving no room for the appender seam, so a
    reverter that needs one cannot be handed over directly. This is the one
    place that binding is made, mirroring ``run_termination.readiness_finalizer``
    and ``review_cycle.existing_review_verdict_recorder``.
    """

    def revert(
        snapshot: plans.PlanSnapshot,
        session_id: str,
        *,
        failure_note: str,
    ) -> Path | None:
        return _revert_session_status_to_pending(
            snapshot,
            session_id,
            failure_note=failure_note,
            append_handoff_note=append_handoff_note,
        )

    return revert


def _session_path_in_snapshot(
    snapshot: plans.PlanSnapshot, session_id: str
) -> Path | None:
    for session in snapshot.sessions:
        if session.id == session_id:
            return session.path
    return None


def _append_progress_entry(
    after: plans.PlanSnapshot,
    session_id: str,
    completed_at: str,
) -> bool:
    """Append a progress-journal entry for the just-completed session.

    Returns ``True`` when a new entry was written, ``False`` when the call
    was an idempotent no-op because the most recent journal entry already
    matches ``(session_id, completed_at)``.
    """
    session_status = next(
        (item for item in after.sessions if item.id == session_id),
        None,
    )
    if session_status is None:
        raise plans.PlanError(f"session {session_id!r} not found in plan snapshot")
    handoff = plans.read_session_handoff(session_status.session)
    files_modified: tuple[str, ...] = ()
    if handoff.files_modified:
        inline = handoff.files_modified[0]
        files_modified = tuple(
            part.strip() for part in inline.split(",") if part.strip()
        )
    entry = plans.ProgressEntry(
        session_id=handoff.session_id,
        status=handoff.status,
        completed_at=completed_at,
        files_modified=files_modified,
        key_decisions=handoff.key_decisions,
        discovered_facts=handoff.discovered_facts,
        open_questions=handoff.open_questions,
        blockers=handoff.blockers,
    )
    existing = plans.read_progress_entries(after.plan_dir)
    if (
        existing
        and existing[-1].session_id == entry.session_id
        and existing[-1].completed_at == entry.completed_at
    ):
        return False
    plans.append_progress_entry(after.plan_dir, entry)
    return True


def _revert_session_status_to_pending(
    snapshot: plans.PlanSnapshot,
    session_id: str,
    *,
    failure_note: str,
    append_handoff_note: HandoffNoteAppender,
) -> Path | None:
    path = _session_path_in_snapshot(snapshot, session_id)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines(keepends=False)
    handoff_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == plans.HANDOFF_HEADING:
            handoff_index = index
            break
    if handoff_index is None:
        return None
    section_end = len(lines)
    for index in range(handoff_index + 1, len(lines)):
        if lines[index].startswith("## "):
            section_end = index
            break
    for index in range(handoff_index + 1, section_end):
        if lines[index].startswith(plans.STATUS_PREFIX):
            lines[index] = f"{plans.STATUS_PREFIX} pending"
            break
    lines = append_handoff_note(
        lines,
        handoff_index=handoff_index,
        section_end=section_end,
        note=failure_note,
    )
    suffix = "\n" if text.endswith("\n") else ""
    path.write_text("\n".join(lines) + suffix, encoding="utf-8")
    return path


def _mailbox_completion_guard_evidence(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> dict[str, Any] | None:
    """Return body-free evidence when a blocking worker question was ignored."""
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except (OSError, mailbox.MailboxError) as error:
        return {
            "mailbox_path": str(mailbox.mailbox_path_for_plan(plan_dir, state_dir_arg)),
            "mailbox_schema_version": mailbox.MAILBOX_SCHEMA_VERSION,
            "projection_seq": None,
            "read_error": (
                error.reason
                if isinstance(error, mailbox.MailboxError)
                else mailbox.ERROR_MAILBOX_READ
            ),
            "questions": [],
        }
    violations: list[dict[str, Any]] = []
    for question in view.messages:
        if (
            question.kind != mailbox.KIND_QUESTION
            or question.version < mailbox.MAILBOX_WAIT_SCHEMA_VERSION
            or question.delivery_mode != mailbox.DELIVERY_MODE_WAIT
            or question.waiter_origin != mailbox.WAITER_ORIGIN_WORKER
            or question.run_id != run_id
            or question.session_id != session_id
            or question.turn_index != turn_index
        ):
            continue
        closure = view.closure_for(question.id)
        receipt = None if closure is None else view.receipt_for_closure(closure.id)
        if closure is None:
            violation = "unanswered"
        elif receipt is None:
            violation = "unconsumed"
        elif (
            closure.kind == mailbox.KIND_RESOLUTION
            and closure.action == mailbox.RESOLUTION_ACTION_PAUSE
        ):
            violation = "pause-resolved"
        else:
            continue
        record: dict[str, Any] = {
            "question_id": question.id,
            "question_seq": question.seq,
            "delivery_mode": question.delivery_mode,
            "violation": violation,
            "active_wait": view.active_wait_for(question.id) is not None,
        }
        if closure is not None:
            record.update(
                closure_id=closure.id,
                closure_seq=closure.seq,
                closure_kind=closure.kind,
                resolution_action=closure.action,
            )
        if receipt is not None:
            record.update(receipt_id=receipt.id, receipt_seq=receipt.seq)
        violations.append(record)
    if not violations:
        return None
    return {
        "mailbox_path": str(view.mailbox_path),
        "mailbox_schema_version": mailbox.MAILBOX_SCHEMA_VERSION,
        "projection_seq": view.last_seq,
        "questions": violations,
    }


def _enforce_mailbox_completion_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    after: plans.PlanSnapshot,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_index: int,
    state_dir_arg: str | None,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    append_handoff_note: HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
) -> bool:
    """Reject completion that bypassed a v3 blocking-question lifecycle."""
    evidence = _mailbox_completion_guard_evidence(
        plan_dir=after.plan_dir,
        state_dir_arg=state_dir_arg,
        run_id=str(payload.get("run_id")),
        session_id=session_id,
        turn_index=turn_index,
    )
    if evidence is None:
        return False
    reverted = _revert_session_status_to_pending(
        after,
        session_id,
        failure_note=(
            "runner rejected completion: a blocking mailbox question was "
            "unanswered, unconsumed, or resolved with pause"
        ),
        append_handoff_note=append_handoff_note,
    )
    snapshot_after = reinspect(after.plan_dir, after, reverted, plan_cache)
    guard = {
        "passed": False,
        "reverted_to_pending": reverted is not None,
        **evidence,
    }
    with persistence.lock:
        payload["final_snapshot"] = plans.snapshot_payload(snapshot_after)
        turn_payload["mailbox_completion_guard"] = guard
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="mailbox-completion-guard",
        reason="blocked",
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason="blocked",
        message=(
            f"{session_label} completed while a blocking mailbox question "
            "was not safely consumed"
        ),
        details={**session_details, "mailbox": evidence},
        persistence=persistence,
    )
    return True


def _read_review_verdict(
    plan_dir: Path, *, require_artifact: bool = False
) -> dict[str, Any]:
    return review.load_review_verdict(plan_dir, require_artifact=require_artifact)


def _enforce_review_mutation_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    cwd: Path,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    fingerprint_before: dict[str, Any],
    exclude: tuple[str, ...],
    include_ignored: bool,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    append_handoff_note: HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
) -> bool:
    """Compare the post-turn fingerprint against `fingerprint_before`.

    Runs ahead of every result branch, so the turn's own outcome never
    suppresses the verdict: a retryable pause such as ``timeout`` would
    otherwise relaunch against the mutated tree and adopt it as the new
    baseline. `after` is ``None`` when the post-turn plan could not be
    inspected; the revert then targets the pre-turn snapshot, which names the
    same session file. Returns ``True`` when the run must pause (the caller
    must ``break``).
    """
    fingerprint_after = review_guard._capture_review_repo_fingerprint(
        cwd,
        before.plan_dir,
        exclude=exclude,
        include_ignored=include_ignored,
    )
    excluded = list(exclude)
    unavailable = review_guard._review_guard_unavailable_detail(fingerprint_after)
    if unavailable is not None:
        reason = events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE
        failure_note = f"review mutation guard unavailable: {unavailable['reason']}"
        message = (
            "review mutation guard unavailable after "
            f"{session_label}: {unavailable['reason']}"
        )
        evidence: dict[str, Any] = {
            "phase": "after",
            "unavailable": unavailable,
            "excluded": excluded,
            "include_ignored": include_ignored,
        }
    elif fingerprint_after != fingerprint_before:
        reason = "review-mutated-worktree"
        changed_paths = review_guard._review_guard_changed_paths(
            fingerprint_before, fingerprint_after
        )
        failure_note = (
            "review mutation guard failed: repository content changed "
            "outside the plan directory"
        )
        message = (
            f"review session {session_label} mutated repository content "
            "outside the plan directory"
            f"{review_guard._review_guard_changed_summary(changed_paths)}"
        )
        evidence = {
            "fingerprint_before": fingerprint_before,
            "fingerprint_after": fingerprint_after,
            "changed_paths": changed_paths,
            "excluded": excluded,
            "include_ignored": include_ignored,
        }
    else:
        with persistence.lock:
            turn_payload["review_mutation_guard"] = {
                "passed": True,
                "excluded": excluded,
                "include_ignored": include_ignored,
            }
            repo_state = review_guard._review_repo_state(fingerprint_after)
            if repo_state is not None:
                turn_payload["review_repo_state"] = repo_state
            durability.write_json_file(paths["result"], turn_payload)
        return False

    # Leaving the review complete would make an unguarded review permanent
    # across a resume, so a failed guard always puts it back to pending.
    revert_target = after if after is not None else before
    reverted = _revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=append_handoff_note,
    )
    snapshot_after = reinspect(before.plan_dir, revert_target, reverted, plan_cache)
    guard_record = {
        "passed": False,
        "reason": reason,
        "reverted_to_pending": reverted is not None,
        **evidence,
    }
    with persistence.lock:
        payload["final_snapshot"] = plans.snapshot_payload(snapshot_after)
        turn_payload["review_mutation_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="review-mutation-guard",
        reason=reason,
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=message,
        details={
            **session_details,
            **evidence,
            "review_mutation_guard": guard_record,
        },
        persistence=persistence,
    )
    return True


def _terminating_result_disposition(
    result: harnesses.HarnessResult,
    after: plans.PlanSnapshot | None,
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
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    digests_before: Mapping[str, str],
    runner_written: tuple[str, ...],
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
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
    digests_after = plans.session_artifact_digests(
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
        turn_execution.record_turn_rejected(
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
        "reverted_to_pending": reverted is not None,
        **evidence,
    }
    with persistence.lock:
        payload["final_snapshot"] = plans.snapshot_payload(snapshot_after)
        turn_payload["plan_artifact_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="plan-artifact-guard",
        reason=reason,
        reopened=reverted is not None,
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


def _capture_workspace_write_guard_snapshot(
    *,
    workspace: workspaces.ResolvedWorkspace,
    plan_dir: Path,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> repository_evidence.WorkspaceSnapshot:
    """Capture exactly the repositories the plan registered in ``workspace``.

    Repositories the plan never registered are not the plan's business: a
    concurrent run working in a sibling worktree must not appear as evidence
    about this turn. The public review-guard exclusions are
    repository-relative and apply to every registered repository. Git's
    ordinary ignore policy is still honored unless ``include_ignored`` was
    explicitly requested.
    """
    return repository_evidence.capture_registered_snapshot(
        workspace.root,
        plan_dir,
        tuple(repository.repository.path for repository in workspace.repositories),
        default_exclude=exclude,
        include_ignored=include_ignored,
        _run_git_fn=review_guard._run_git,
        _run_git_bytes_fn=review_guard._run_git_bytes,
    )


def _capture_legacy_implementation_snapshot(
    *,
    cwd: Path,
    plan_dir: Path,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> repository_evidence.WorkspaceSnapshot:
    """Capture the one Git repository that grants a legacy run authority."""
    return OrderedDict(
        (
            (
                ".",
                review_guard._capture_review_repo_fingerprint(
                    cwd,
                    plan_dir,
                    exclude=exclude,
                    include_ignored=include_ignored,
                ),
            ),
        )
    )


def _legacy_unobserved_repositories(
    *,
    cwd: Path,
    artifact_paths: Sequence[Path],
    exempt_roots: Sequence[Path],
    provenance_roots: Sequence[Path],
    limit: int = repository_intent.INTENT_REPORT_LIMIT,
) -> dict[str, dict[str, Any]]:
    """Name every repository this turn reached that the snapshot never saw.

    :func:`_capture_legacy_implementation_snapshot` fingerprints exactly one
    repository, the one containing ``cwd``. A write landing anywhere else
    therefore produces no ``changed`` entry, no added or removed repository,
    and nothing ``unavailable`` -- so the guard reported ``passed: True`` while
    an edit landed in a repository it had never looked at. Silence about a
    place you did not look is not evidence of no change.

    The repositories a turn reaches are re-derived here from the same intent
    machinery the ``identity.repository-intent`` check reads, so the two stop
    contradicting each other. The pre-turn packet gate scans what the runner
    hands the worker; this scan runs after the turn over what came back --
    plan artifacts the worker just rewrote and the message it signed off with
    -- because that is where a write the fingerprint could not see gets named.

    Only the exemptions the pre-turn gate already honors apply: runner-owned
    trees, the document the plan was split from, non-Git paths, and system
    paths. Whatever survives that and is not the bound repository is reported
    through ``unavailable``, the guard's existing fail-closed lane.
    """
    candidates: list[repository_intent.IntentCandidate] = []
    for path in artifact_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # An artifact that cannot be read names no repository. The turn is
            # still judged on every artifact that can be, and a missing
            # fingerprint is caught by the comparison, not by this scan.
            continue
        candidates.extend(repository_intent.candidates_from_plan_document(text))
    candidates.extend(repository_intent.candidates_from_execution_roots([cwd]))
    intent = repository_intent.resolve_intent(
        candidates,
        root=cwd,
        exempt_roots=exempt_roots,
        provenance_roots=provenance_roots,
    )
    unobserved = [root for root in intent.git_roots if root != intent.bound_git_root]
    reported, omitted = workspaces.bounded_repository_rows(unobserved, limit=limit)
    observed = (
        repository_intent.redact_credentials(str(intent.bound_git_root))
        if intent.bound_git_root is not None
        else None
    )
    payload: dict[str, dict[str, Any]] = {}
    for root in reported:
        payload[repository_intent.redact_credentials(str(root))] = {
            "reason": "repository-not-observed",
            "observed_repository": observed,
        }
    if omitted:
        # The count matters more than the names once the list is truncated:
        # the guard fails closed on the first entry either way.
        payload["<omitted>"] = {
            "reason": "repository-not-observed",
            "observed_repository": observed,
            "omitted_repositories": omitted,
        }
    return payload
