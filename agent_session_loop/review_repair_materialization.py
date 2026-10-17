"""Publish planned repair children atomically and roll back refused publications."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    authoring_manifest,
    authoring_transaction,
    baseline,
    plans_discovery,
    state,
    state_locks,
)
from . import (
    review_repair_models as repair_models,
)
from . import (
    review_repair_planning as repair_planning,
)
from . import (
    review_repair_publication as repair_publication,
)
from . import (
    review_repair_validation as repair_validation,
)
from .authoring_manifest import ManifestSession
from .authoring_transaction import PlanTransaction
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_session_ids import SessionId


def _malformed(
    review_id: SessionId | None, error: str, *, detail: str | None = None
) -> repair_models.ReviewRepairResult:
    return repair_models.ReviewRepairResult(
        ok=False,
        reason=repair_models.REVIEW_REPAIR_REASON_MALFORMED,
        review_session_id=review_id,
        error=error,
        detail=detail,
    )


def materialize_review_repair_sessions(
    plan_dir: str | Path,
    *,
    review_session_id: int | str | SessionId | None = None,
    cwd: str | Path | None = None,
    state_dir: str | Path | None = None,
    hold_lock: bool = True,
    allow_not_loop_ready: bool = False,
    now: datetime | None = None,
) -> repair_models.ReviewRepairResult:
    """Materialize validated review findings into serial repair children.

    Reads the structured review verdict from ``plan_dir`` (fail-closed via
    :func:`agent_session_loop.review.load_review_verdict`), groups its findings
    by ``repair_group``, and appends one pending repair child session per group
    beneath the generated review session as ``N.1``, ``N.2``, ... The write is
    atomic: on any failure the plan is left exactly as it was.

    ``review_session_id`` selects the review session explicitly; when omitted
    the latest generated review session is used. ``cwd`` (a repository root)
    enables the targeted-baseline + preflight gate that marks the plan
    loop-ready. ``hold_lock`` acquires the plan lock around the mutation; pass
    ``False`` when the caller already holds it (e.g. the runner).

    Children that reach disk but fail the loop-ready gate are withdrawn: the
    publication rolls back to the plan it started from, so a selected repair
    child is always loop-ready. ``allow_not_loop_ready=True`` is the operator
    opt-out (the runner's ``--allow-not-loop-ready``): it retains children
    blocked only by bypassable findings. Non-bypassable blockers always
    withdraw, and an interrupted publication always rolls back.
    """
    canonical_dir = canonicalize_path(plan_dir)

    lock_acquired = False
    lock_info: state.LockInfo | None = None
    if hold_lock:
        try:
            lock_info = state_locks.acquire_lock(canonical_dir, state_dir)
            lock_acquired = True
        except state.StateError as error:
            return _malformed(None, f"could not acquire plan lock: {error}")
    else:
        lock_info = state_locks.read_lock_info(canonical_dir, state_dir)
    try:
        # An earlier publication that died mid-flight owns this plan's files
        # until it is rolled back. Recover before planning so the plan being
        # read is the one a run could execute, and refuse outright when the
        # rollback did not land: publishing over an unrecovered window would
        # destroy the only image of the state it was meant to restore.
        recovery = repair_publication.recover_review_repair_publication(canonical_dir)
        if recovery is not None and not recovery.get("recovered"):
            return _malformed(
                None,
                "an interrupted review repair publication could not be rolled "
                f"back: {recovery.get('reason')}",
                detail=json.dumps(recovery, sort_keys=True),
            )
        return _materialize(
            canonical_dir,
            review_session_id=review_session_id,
            cwd=cwd,
            state_dir=state_dir,
            allow_not_loop_ready=allow_not_loop_ready,
            now=now,
            lock_info=lock_info,
        )
    finally:
        if lock_acquired:
            state_locks.release_lock(canonical_dir, state_dir)


def _materialize(
    canonical_dir: Path,
    *,
    review_session_id: int | str | SessionId | None,
    cwd: str | Path | None,
    state_dir: str | Path | None,
    allow_not_loop_ready: bool = False,
    now: datetime | None,
    lock_info: state.LockInfo | None,
) -> repair_models.ReviewRepairResult:
    repair_plan = repair_planning.plan_review_repair_sessions(
        canonical_dir, review_session_id=review_session_id
    )
    if not repair_plan.ok:
        return _malformed(
            repair_plan.review_session_id,
            repair_plan.error or "review repair planning failed",
            detail=repair_plan.detail,
        )
    review_id = repair_plan.review_session_id
    if review_id is None:
        return _malformed(None, "review repair planning did not resolve a session")
    if repair_plan.reason == repair_models.REVIEW_REPAIR_REASON_NO_FINDINGS:
        return repair_models.ReviewRepairResult(
            ok=True,
            reason=repair_models.REVIEW_REPAIR_REASON_NO_FINDINGS,
            review_session_id=review_id,
        )

    if cwd is not None:
        pre_authoring_gate = repair_validation._validate_children_before_authoring(
            canonical_dir, cwd, repair_plan.repair_groups
        )
        if not pre_authoring_gate["loop_ready"]:
            return repair_models.ReviewRepairResult(
                ok=True,
                reason=repair_models.REVIEW_REPAIR_REASON_NOT_LOOP_READY,
                review_session_id=review_id,
                error=(
                    "review repair children were refused before authoring: "
                    "they carry repair targets or verification checks the "
                    "plan's repository contract cannot satisfy"
                ),
                blockers=tuple(pre_authoring_gate["blockers"]),
                non_bypassable_blockers=tuple(
                    pre_authoring_gate["non_bypassable_blockers"]
                ),
            )

    sessions = plans_discovery.discover_session_files(canonical_dir)

    # Stage the fully validated child texts produced by the read-only planner.
    transaction = PlanTransaction(canonical_dir)
    children: list[repair_models.RepairChild] = []
    manifest_rows: list[ManifestSession] = []
    for group_plan in repair_plan.repair_groups:
        child_id = group_plan.proposed_child_id
        transaction.stage(child_id.filename, group_plan.rendered_text)
        children.append(
            repair_models.RepairChild(
                session_id=child_id,
                repair_group=group_plan.repair_group,
                title=group_plan.title,
                finding_ids=group_plan.finding_ids,
                path=canonical_dir / child_id.filename,
                read_repositories=group_plan.read_repositories,
                write_repositories=group_plan.write_repositories,
            )
        )
        manifest_rows.append(
            ManifestSession(
                session_id=child_id,
                title=group_plan.title,
                status="pending",
            )
        )

    # Refresh the manifest table + session count (derived bookkeeping).
    manifest_path = canonical_dir / "manifest.md"
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        manifest_text = None
    if manifest_text is not None:
        new_manifest = manifest_text
        try:
            for row in manifest_rows:
                new_manifest = authoring_manifest.add_manifest_session_row(
                    new_manifest, row
                )
        except PlanError:
            # No session-status table to update; leave the table untouched but
            # still refresh the derived session count when present.
            new_manifest = manifest_text
        new_manifest = authoring_manifest.replace_manifest_sessions_count(
            new_manifest, len(sessions) + len(children)
        )
        if new_manifest != manifest_text:
            transaction.stage("manifest.md", new_manifest)

    # Repoint the successor's predecessor reference at the last child, if any.
    successor = repair_planning._immediate_successor(sessions, review_id)
    if successor is not None:
        try:
            successor_text = successor.path.read_text(encoding="utf-8")
        except OSError:
            successor_text = None
        if successor_text is not None:
            updated, changed = authoring_manifest.replace_previous_session_reference(
                successor_text, review_id, children[-1].session_id
            )
            if changed:
                transaction.stage(successor.session_id.filename, updated)

    # Everything from here down changes durable plan state, so the publication
    # opens its rollback image first. Between this line and the close below the
    # plan is mid-publication: a crash leaves the image on disk and the next
    # locked caller restores it, so no run can ever select a child whose
    # loop-ready state was never established.
    window = repair_publication._open_publication_window(
        canonical_dir,
        review_id=review_id,
        child_filenames=[child.session_id.filename for child in children],
        restore_paths=[
            *transaction.staged_paths,
            # readiness metadata lives in the manifest, and the post-authoring
            # refresh rewrites it whether or not the manifest was staged.
            canonical_dir / "manifest.md",
            # the executability gate runs the children's targeted baselines,
            # which persist their outcomes here.
            canonical_dir / baseline.BASELINE_RESULTS_FILENAME,
        ],
        now=now,
    )

    try:
        transaction.commit()
    except PlanError as error:
        repair_publication._rollback_publication_window(window)
        return _malformed(review_id, f"atomic plan write failed: {error}")

    try:
        # Authoring succeeded — refresh readiness to draft (children not yet
        # executable). Mirrors the prepare-pipeline post-authoring contract.
        authoring_transaction.refresh_readiness_metadata(
            canonical_dir, state="draft", now=now
        )

        loop_ready = False
        blockers: tuple[str, ...] = ()
        non_bypassable_blockers: tuple[str, ...] = ()
        baseline_summary: dict[str, Any] | None = None
        reason = repair_models.REVIEW_REPAIR_REASON_OK
        if cwd is not None:
            repair_publication._advance_publication_phase(
                window, repair_publication.PUBLICATION_PHASE_VALIDATING
            )
            gate = repair_validation._make_children_executable(
                canonical_dir, cwd, children, state_dir, lock_info
            )
            loop_ready = gate["loop_ready"]
            blockers = tuple(gate["blockers"])
            non_bypassable_blockers = tuple(gate["non_bypassable_blockers"])
            baseline_summary = gate["baseline_summary"]
            retained = allow_not_loop_ready and not non_bypassable_blockers
            if not loop_ready:
                reason = repair_models.REVIEW_REPAIR_REASON_NOT_LOOP_READY
            if not loop_ready and not retained:
                # ASI-136: children that reached disk but cannot be executed
                # used to stay there, leaving a pending non-loop-ready repair
                # child no run could clear. Withdraw them instead: the blockers
                # still describe exactly what was refused, and the plan is the
                # valid one the publication started from.
                rollback = repair_publication._rollback_publication_window(window)
                return repair_models.ReviewRepairResult(
                    ok=True,
                    reason=repair_models.REVIEW_REPAIR_REASON_NOT_LOOP_READY,
                    review_session_id=review_id,
                    children=(),
                    loop_ready=False,
                    error=(
                        "review repair children were withdrawn after "
                        "authoring: the plan could not be marked loop-ready "
                        "with them in it"
                    ),
                    blockers=blockers,
                    baseline_summary=baseline_summary,
                    non_bypassable_blockers=non_bypassable_blockers,
                    rolled_back=not rollback["errors"],
                    rollback=rollback,
                )
    except BaseException:
        # Includes KeyboardInterrupt and SystemExit: an interrupted publication
        # is exactly the case the rollback image exists for.
        repair_publication._rollback_publication_window(window)
        raise

    repair_publication._advance_publication_phase(
        window, repair_publication.PUBLICATION_PHASE_PUBLISHING
    )
    repair_publication._close_publication_window(window)
    return repair_models.ReviewRepairResult(
        ok=True,
        reason=reason,
        review_session_id=review_id,
        children=tuple(children),
        loop_ready=loop_ready,
        blockers=blockers,
        baseline_summary=baseline_summary,
        non_bypassable_blockers=non_bypassable_blockers,
    )
