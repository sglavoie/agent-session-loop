"""The protected-path guard: refuse a turn that moved a frozen path.

:func:`_enforce_protected_path_guard` refuses a turn that moved a path an
operator froze mid-run, with :func:`_protected_path_changed_paths` reading the
changed set out of the write guard's own evidence and
:func:`_protected_path_review_changed_paths` recapturing it for a legacy review
turn that crashed before producing any.

* **One capture answers both questions.** The guard reads the write guard's
  recorded evidence instead of recapturing, because a second snapshot taken at a
  different instant could convict a path the first cleared.
* **A guard that cannot see is not a guard that saw nothing.** ``changed_paths``
  of ``None`` refuses the turn through the unavailable reason.
* **Adjudicate-only never displaces a decided outcome.** A turn already ending
  for another reason still reaches and records the verdict, and amends the live
  pause details in place, but performs no second revert and publishes no pause.
* **Filesystem changes are always retained.** Nothing rolls back.

The handoff-note appender and the post-revert plan re-read enter as required
keyword-only seams. Nothing here imports the runner.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    durability,
    events,
    guards,
    handoff_guards,
    plans_models,
    plans_payloads,
    plans_snapshot,
    repository_evidence,
    review_guard,
    run_persistence,
    run_termination,
    turn_execution_commit,
)


def _protected_path_changed_paths(
    evidence: Mapping[str, Any] | None,
) -> tuple[str, ...] | None:
    """Every qualified path the turn changed, as the write guard recorded it.

    Read from the guard's own evidence rather than recaptured: a second
    snapshot would be taken at a different instant, so a path that moved
    between the two comparisons could be convicted by one and cleared by the
    other. One capture, two questions.

    ``None`` means the comparison cannot answer the protected-path question at
    all. That includes a write guard that failed to inventory a repository:
    its ``changed`` map is then a partial view, and reading an empty one as
    "nothing moved" would clear a frozen path the run never actually looked
    at. This only arises on a composed finalization, because an incomplete
    inventory already fails the write guard.
    """
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("unavailable"):
        return None
    changed = evidence.get("changed")
    if not isinstance(changed, Mapping):
        return None
    qualified: list[str] = []
    for repository, entry in changed.items():
        if not isinstance(entry, Mapping):
            continue
        paths = entry.get("qualified_paths")
        if isinstance(paths, list):
            qualified.extend(str(path) for path in paths)
        if not entry.get("head_changed"):
            continue
        # A committed change leaves the status lanes clean and moves only the
        # tip, so the paths the tip move carried are read alongside them. A
        # moved tip without that list is a commit nobody looked inside.
        head_paths = entry.get("head_paths")
        if not isinstance(head_paths, list):
            return None
        alias = entry.get("alias")
        qualifier = alias if isinstance(alias, str) and alias else str(repository)
        qualified.extend(f"{qualifier}:{path}" for path in head_paths)
    return tuple(dict.fromkeys(qualified))


def _protected_path_review_changed_paths(
    guard_context: Mapping[str, Any],
    *,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> tuple[str, ...] | None:
    """Every qualified path a crashing legacy review turn changed.

    A legacy review turn captures no write-guard comparison, so the crash path
    has nothing to read out of ``workspace_write_guard``. It recaptures the
    review fingerprint instead and compares it against the one taken at turn
    start -- the same comparison the mutation guard would have made had the
    turn finished -- so the protected-path guard has a baseline to adjudicate
    against.

    ``None`` means the recapture cannot answer the question: it raised, or the
    evidence came back unusable. That is reported as unavailable rather than
    read as "nothing moved", because a guard that cannot see is not a guard
    that saw nothing.
    """
    fingerprint_before = guard_context.get("review_fingerprint_before")
    if not isinstance(fingerprint_before, Mapping):
        return None
    try:
        fingerprint_after = review_guard._capture_review_repo_fingerprint(
            guard_context["cwd"],
            guard_context["before"].plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    except Exception:  # noqa: BLE001
        return None
    if review_guard._review_guard_unavailable_detail(fingerprint_after) is not None:
        return None
    if fingerprint_after == fingerprint_before:
        # Equal fingerprints prove the whole worktree is unchanged, which
        # clears every path rule at once -- a stronger clearance than an empty
        # change list, and the same one the finished-turn path grants.
        return ()
    changes = review_guard._review_guard_changed_paths(
        fingerprint_before, fingerprint_after
    )
    qualified = repository_evidence.qualified_repository_paths(
        guards.LEGACY_PATH_QUALIFIER, changes
    )
    if changes.get("head_changed"):
        # The same committed-change blind spot the finished-turn path closes.
        try:
            head_paths = repository_evidence.head_transition_paths(
                guard_context["cwd"],
                str(changes.get("head_before") or ""),
                str(changes.get("head_after") or ""),
            )
        except Exception:  # noqa: BLE001
            return None
        qualified.extend(
            f"{guards.LEGACY_PATH_QUALIFIER}:{path}" for path in head_paths
        )
    return tuple(dict.fromkeys(qualified))


def _enforce_protected_path_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    guard_controller: guards.GuardController | None,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    append_handoff_note: handoff_guards.HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
    changed_paths: tuple[str, ...] | None,
    adjudicate_only: bool = False,
) -> bool:
    """Refuse to publish a turn that moved a path an operator declared frozen.

    This runs after the general workspace write guard and before every result
    branch, and it is deliberately a *separate* step: the write guard asks
    whether the session stayed inside its declared scope, which is a question
    about the plan, while this asks whether the run honoured a standing
    instruction an operator gave mid-flight, which is a question about the
    operator. A path can be perfectly in scope and still be one the operator
    said not to touch.

    The baseline is the turn-start snapshot, so a guard added mid-turn convicts
    a change the turn had already made. That is the intended reading of "this
    file must not change": an operator who sees a file they care about and
    freezes it is asking about its state now, not about the moment their
    command was drained.

    Filesystem changes are always retained. The run has no way to know whether
    the change was the valuable part of the turn, and silently reverting an
    agent's work to satisfy a guard would destroy more than it protects.

    ``changed_paths`` of ``None`` means the turn produced no comparison this
    guard can read, which is refused rather than waved through: a guard that
    cannot see is not a guard that saw nothing.

    ``adjudicate_only`` is for a turn that is already ending for another
    reason -- another finalization guard paused it, or the harness raised.
    The verdict is still reached and still recorded, but the outcome that was
    already decided is left standing: no second revert, and no pause that
    would overwrite the first one's reason and remediation with this one's.
    An operator reading the primary pause still finds the protected-path
    verdict beside it, because a combined failure that reports only half of
    itself is the one an operator repairs twice.
    """
    active = () if guard_controller is None else guard_controller.active_paths()
    if not active:
        return False

    evidence = turn_payload.get("workspace_write_guard")
    unavailable_detail: str | None = None
    if changed_paths is None:
        changed_paths = ()
        unavailable_detail = (
            "no turn-start comparison was available for this turn, so the "
            "workspace could not be compared against its baseline"
        )

    coverage_by_id = {
        entry.get("id"): entry.get("coverage")
        for entry in (
            guard_controller.to_payload() if guard_controller is not None else []
        )
    }
    matched = (
        guards.match_protected_paths(active, changed_paths)
        if unavailable_detail is None
        else {}
    )
    if unavailable_detail is None and not matched:
        with persistence.lock:
            turn_payload["protected_path_guard"] = {
                "passed": True,
                "session_id": session_id,
                "guards": [rule.to_payload() for rule in active],
                "observed_paths": list(changed_paths),
                "coverage": {rule.id: coverage_by_id.get(rule.id) for rule in active},
            }
            durability.write_json_file(paths["result"], turn_payload, durable=True)
        return False

    if unavailable_detail is not None:
        reason = events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE
        failure_note = (
            "protected-path guard evidence unavailable; "
            f"{unavailable_detail}, so the guard could neither clear nor "
            "convict the turn"
        )
        message = (
            f"protected-path guard unavailable after {session_label}; "
            f"{unavailable_detail}"
        )
    else:
        reason = events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED
        named = ", ".join(
            f"{guard_id} ({', '.join(hits)})" for guard_id, hits in matched.items()
        )
        failure_note = (
            f"protected-path guard violation retained: {named}; restore the "
            "path to its turn-start state or remove the guard before rerunning"
        )
        message = (
            f"{session_label} changed protected paths: {named}; changes were retained"
        )

    matched_repositories = {
        qualified.partition(":")[0] for hits in matched.values() for qualified in hits
    }
    raw_changed = evidence.get("changed") if isinstance(evidence, Mapping) else None
    before_after = (
        {
            repository: dict(entry)
            for repository, entry in raw_changed.items()
            if isinstance(entry, Mapping)
            # A qualified path names a repository by its registered alias when
            # it has one, while the write guard keys its comparison by
            # repository path. Either name identifies the same repository, so
            # either one keeps the evidence attached; matching only the key
            # silently drops the before/after vectors for every aliased
            # workspace repository.
            and (
                repository in matched_repositories
                or entry.get("alias") in matched_repositories
            )
        }
        if isinstance(raw_changed, Mapping)
        else {}
    )
    guard_record: dict[str, Any] = {
        "passed": False,
        "session_id": session_id,
        "reason": reason,
        "phase": "after",
        "guard_ids": list(matched),
        "matched_paths": {guard_id: list(hits) for guard_id, hits in matched.items()},
        "guards": [rule.to_payload() for rule in active],
        "observed_paths": list(changed_paths),
        # The write guard's own before/after vectors for exactly the
        # repositories that hold a matched path: the operator's next decision
        # is "restore or accept", and that needs the content evidence, not just
        # the names.
        "evidence": before_after,
        "coverage": {rule.id: coverage_by_id.get(rule.id) for rule in active},
        "retained": True,
        "detail": unavailable_detail,
    }

    if adjudicate_only:
        guard_record["adjudicated_only"] = True
        # The turn is already ending, and whichever outcome got here first
        # already withdrew the session if it needed withdrawing. Reverting
        # again would append a second failure note for one turn.
        guard_record["reverted_to_pending"] = False
        primary_reason = payload.get("pause_reason")
        if payload.get("paused") and isinstance(primary_reason, str):
            guard_record["deferred_to_pause_reason"] = primary_reason
        with persistence.lock:
            turn_payload["protected_path_guard"] = guard_record
            durability.write_json_file(paths["result"], turn_payload, durable=True)
            # The primary pause was published before this verdict existed, so
            # its live details are amended in place -- ``payload["details"]``
            # is the same object -- rather than republished under a reason
            # that would displace the one an operator has to act on.
            pause_details = payload.get("pause_details")
            if payload.get("paused") and isinstance(pause_details, dict):
                pause_details["protected_path_guard"] = guard_record
        turn_execution_commit.record_turn_rejected(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="protected-path-guard",
            reason=reason,
            reopened=False,
            detail={
                "adjudicated_only": True,
                "guard_ids": list(matched),
                "matched_paths": {
                    guard_id: list(hits) for guard_id, hits in matched.items()
                },
            },
        )
        return False

    revert_target = after if after is not None else before
    reverted = handoff_guards._revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=append_handoff_note,
    )
    inspected = reinspect(before.plan_dir, revert_target, reverted, plan_cache)
    guard_record["reverted_to_pending"] = handoff_guards._reopened(reverted)
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(inspected)
        turn_payload["protected_path_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload, durable=True)
    turn_execution_commit.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="protected-path-guard",
        reason=reason,
        revert=reverted,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=message,
        details={**session_details, "protected_path_guard": guard_record},
        persistence=persistence,
    )
    return True
