"""Public review continuation evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_errors,
    plans_models,
    plans_session_ids,
    plans_snapshot,
    review,
    review_repair,
)
from .review_continuation_events import (
    _standing_environment_pause_blocker,
    read_review_events,
    repeated_finding_positions,
)
from .review_continuation_groups import (
    _generated_repair_statuses,
    _generated_review_statuses,
    _reconciled_materialization_keys,
    repair_group_attempts,
)
from .review_continuation_guidance import (
    _resume,
    _unresolved_materialization_blocker,
)
from .review_continuation_models import (
    BUDGET_REVIEW_DISPOSITIONS,
    MATERIALIZED_REVIEW_DISPOSITIONS,
    REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
    REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_MALFORMED,
    REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
    REVIEW_CYCLE_DISPOSITION_REPEATED,
    TERMINAL_REVIEW_DISPOSITIONS,
    ReviewResume,
    ReviewResumeAction,
    _mapping_tuple,
    _nonempty_str,
    _nonnegative_int,
    _string_tuple,
)
from .review_continuation_state import (
    _DurableRepairScopes,
    _eventless_review_disposition,
    _latest_source,
    _parse_session_id_or_none,
    _pending_repair_scope_gaps,
    _repair_groups_from_children,
    _repair_ids_for_source,
    _resplit_repair_children,
    _selected_cap_from_events,
    _selected_group_attempt_cap_from_history,
    _source_digests,
    review_cycle_state,
)


def evaluate_review_continuation(
    snapshot_or_plan_dir: plans_models.PlanSnapshot | str | Path,
    event_path: str | Path,
    *,
    max_repair_group_attempts: int | None = None,
    max_review_fix_cycles: int | None = None,
    review_harness_requested: bool = False,
) -> ReviewResume:
    """Reconcile history, generated lineage, budget, and current artifacts.

    ``max_repair_group_attempts`` and ``max_review_fix_cycles`` are the selected
    caps for a prospective run. Read-only status consumers may omit either; the
    latest review history, then the persisted run_config, are used when
    available. ``review_harness_requested`` only affects the otherwise inert
    ``initial-review`` projection.
    """
    snapshot = (
        snapshot_or_plan_dir
        if isinstance(snapshot_or_plan_dir, plans_models.PlanSnapshot)
        else plans_snapshot.inspect_plan(snapshot_or_plan_dir)
    )
    records = read_review_events(event_path)
    cycles = [
        record.payload for record in records if record.name == events.REVIEW_CYCLE_EVENT
    ]
    review_statuses = _generated_review_statuses(snapshot)
    generated_repairs = _generated_repair_statuses(snapshot)

    materialization_keys = _reconciled_materialization_keys(
        records, generated_repairs, review_statuses
    )
    materialization_count = len(materialization_keys)
    selected_cap = (
        max_review_fix_cycles
        if max_review_fix_cycles is not None
        else _selected_cap_from_events(records)
    )
    selected_group_attempt_cap = (
        max_repair_group_attempts
        if max_repair_group_attempts is not None
        else _selected_group_attempt_cap_from_history(records, event_path)
    )

    source_id, source_cycle_record = _latest_source(cycles, review_statuses)
    if source_id is None:
        return _resume(
            source_cycle=None,
            source_id=None,
            disposition=None,
            action="initial-review",
            eligible=review_harness_requested,
            blocker=None if review_harness_requested else "review-harness-required",
            materialization_count=materialization_count,
            selected_cap=selected_cap,
        )

    disposition = (
        _nonempty_str(source_cycle_record.get("disposition"))
        if source_cycle_record is not None
        else None
    )
    recovered_eventless_all_clear = False
    if source_cycle_record is None:
        source_status = review_statuses.get(source_id)
        if source_status is not None:
            disposition = _eventless_review_disposition(
                snapshot.plan_dir, source_status
            )
            recovered_eventless_all_clear = (
                disposition == REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            )
    source_cycle = (
        _nonnegative_int(source_cycle_record.get("cycle"))
        if source_cycle_record is not None
        else None
    )
    source_fingerprints = (
        _string_tuple(source_cycle_record.get("fingerprints"))
        if source_cycle_record is not None
        else None
    )
    source_findings = (
        _mapping_tuple(source_cycle_record.get("findings"))
        if source_cycle_record is not None
        else None
    )
    source_repositories = (
        _mapping_tuple(source_cycle_record.get("repositories"))
        if source_cycle_record is not None
        else None
    )
    source_output_digest, source_artifact_digest = _source_digests(source_cycle_record)
    generated_for_source = generated_repairs.get(source_id, {})
    repair_ids = _repair_ids_for_source(source_id, records, generated_for_source)
    # The children that have to run and finish, which is the marker-bearing set
    # plus whatever stands in for a repair the plan archived under a resplit.
    # Durable scope recovery below deliberately keeps reading the marker-bearing
    # set alone: a resplit child carries no repair-group marker to recover from.
    resplit_children, superseded_repair_ids = _resplit_repair_children(
        snapshot, repair_ids, generated_for_source
    )
    accounted_for_source: dict[str, plans_models.SessionStatus] = {
        **generated_for_source,
        **resplit_children,
    }
    pending_ids = tuple(
        sorted(
            (
                session_id
                for session_id, status in accounted_for_source.items()
                if status.status == "pending"
            ),
            key=lambda item: plans_session_ids.parse_session_id(item).sort_key,
        )
    )
    completed_ids = tuple(
        sorted(
            (
                session_id
                for session_id, status in accounted_for_source.items()
                if status.status == "complete"
            ),
            key=lambda item: plans_session_ids.parse_session_id(item).sort_key,
        )
    )
    repair_attempt_counts = repair_group_attempts(event_path, snapshot)

    @cache
    def durable_scopes() -> _DurableRepairScopes | None:
        return _repair_groups_from_children(snapshot.plan_dir, generated_for_source)

    def result(
        action: ReviewResumeAction,
        eligible: bool,
        blocker: str | None,
        *,
        planning: review_repair.ReviewRepairPlan | None = None,
        scope_gaps: tuple[Mapping[str, Any], ...] = (),
    ) -> ReviewResume:
        resume = _resume(
            source_cycle=source_cycle,
            source_id=source_id,
            disposition=disposition,
            action=action,
            eligible=eligible,
            blocker=blocker,
            materialization_count=materialization_count,
            selected_cap=selected_cap,
            max_repair_group_attempts=selected_group_attempt_cap,
            repair_group_attempts=repair_attempt_counts,
            repair_ids=repair_ids,
            pending_ids=pending_ids,
            completed_ids=completed_ids,
            source_fingerprints=source_fingerprints,
            source_findings=source_findings,
            source_repositories=source_repositories,
            planning=planning,
        )
        if resume.repair_groups is None:
            durable = durable_scopes()
            resume = replace(
                resume,
                repair_groups=None if durable is None else durable.groups,
            )
        return replace(
            resume,
            source_review_output_sha256=source_output_digest,
            source_review_artifact_sha256=source_artifact_digest,
            repair_scope_gaps=scope_gaps or None,
        )

    if disposition in TERMINAL_REVIEW_DISPOSITIONS and not (
        recovered_eventless_all_clear and repair_ids
    ):
        return result("terminal", False, disposition)

    if (
        accounted_for_source
        and not pending_ids
        and len(completed_ids) != len(accounted_for_source)
    ):
        return result("terminal", False, "repair-sessions-not-runnable")

    if any(
        repair_id not in accounted_for_source and repair_id not in superseded_repair_ids
        for repair_id in repair_ids
    ):
        # Event lineage is the durable record of every repair that must run.
        # A partially deleted child set must fail closed just like a fully
        # deleted one; otherwise the surviving children could incorrectly
        # advance this source to rereview. A repair the plan archived under a
        # resplit is the one exception, and only because its descendants took
        # over the same accounting.
        return result("terminal", False, "repair-sessions-missing")
    # ASI-6: a ``not-loop-ready`` cycle whose children the executability gate
    # withdrew materialized nothing at all, so there is no child set for
    # anything to have deleted. ``repair-sessions-missing`` is meant for a cycle
    # that really did materialize one and then lost it; reporting it here
    # retires findings that still carry executable repair checks, and offers a
    # remediation -- clear the blockers and mark the plan loop-ready -- that
    # cannot restore a cycle already projected terminal. Such a cycle stays a
    # materialization candidate, gated below on the refusal being cleared.
    withdrawn_materialization = (
        not accounted_for_source
        and not materialization_count
        and disposition == REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
    )
    if (
        not accounted_for_source
        and disposition in MATERIALIZED_REVIEW_DISPOSITIONS
        and not withdrawn_materialization
    ):
        return result("terminal", False, "repair-sessions-missing")

    candidate = (
        disposition in BUDGET_REVIEW_DISPOSITIONS
        or disposition == REVIEW_CYCLE_DISPOSITION_MALFORMED
        or disposition is None
        or withdrawn_materialization
    )
    if not accounted_for_source and not candidate:
        # Preserve an unfamiliar recorded value in ``source_disposition`` for
        # diagnostics, but keep the public blocker vocabulary stable for
        # operator-facing continuations.
        return result("terminal", False, "unknown-disposition")

    try:
        planning = review_repair.plan_review_repair_sessions(
            snapshot.plan_dir, review_session_id=source_id
        )
    except plans_errors.PlanError as error:
        planning = review_repair.ReviewRepairPlan(
            ok=False,
            reason=review_repair.REVIEW_REPAIR_REASON_MALFORMED,
            review_session_id=_parse_session_id_or_none(source_id),
            error=f"could not inspect review repair inputs: {error}",
        )

    if pending_ids:
        blocker = _unresolved_materialization_blocker(snapshot.plan_dir, disposition)
        if blocker is not None:
            return result("run-repairs", False, blocker, planning=planning)
        standing_pause = _standing_environment_pause_blocker(event_path)
        if standing_pause is not None:
            return result("run-repairs", False, standing_pause, planning=planning)
        scope_gaps = _pending_repair_scope_gaps(durable_scopes(), pending_ids, planning)
        if scope_gaps:
            return result(
                "run-repairs",
                False,
                "repair-scope-stale",
                planning=planning,
                scope_gaps=scope_gaps,
            )
        return result("run-repairs", True, None, planning=planning)
    if accounted_for_source and len(completed_ids) == len(accounted_for_source):
        return result("rereview", True, None, planning=planning)
    if (
        selected_group_attempt_cap is not None
        and planning.ok
        and planning.reason == review_repair.REVIEW_REPAIR_REASON_OK
        and any(
            repair_attempt_counts.get(group.repair_group, 0)
            >= selected_group_attempt_cap
            for group in planning.repair_groups
        )
    ):
        return result(
            "terminal",
            False,
            REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
            planning=planning,
        )

    if disposition is None and not generated_for_source and planning.ok:
        cycle_state = review_cycle_state(event_path, snapshot)
        repeats = repeated_finding_positions(
            fingerprints=planning.finding_fingerprints,
            repeat_keys=(
                review.finding_repeat_keys(list(planning.validated_findings)) or ()
            ),
            previous_fingerprints=cycle_state["previous_fingerprints"],
            previous_repeat_keys=cycle_state["previous_repeat_keys"],
        )
    else:
        repeats = ()
    if repeats:
        # A completed rereview can survive a crash before its review_cycle
        # append.  Its repair plan must still honor the same repeated-finding
        # breaker as an uninterrupted _advance_review_cycle invocation.
        return result(
            "terminal",
            False,
            REVIEW_CYCLE_DISPOSITION_REPEATED,
            planning=planning,
        )

    minimum_cap = materialization_count + 1
    if disposition in BUDGET_REVIEW_DISPOSITIONS and (
        selected_cap is None or selected_cap < minimum_cap
    ):
        return result(
            "materialize-repairs",
            False,
            "higher-cap-required",
            planning=planning,
        )

    if not planning.ok or planning.reason != review_repair.REVIEW_REPAIR_REASON_OK:
        artifact_error = planning.error
        if planning.reason == review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS:
            artifact_error = (
                "the completed findings review no longer has repair findings; "
                "a manually changed all-clear artifact cannot finalize it"
            )
        resume = result(
            "materialize-repairs",
            False,
            "artifact-invalid",
            planning=planning,
        )
        return replace(
            resume,
            artifact_valid=(
                False
                if planning.reason == review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS
                else resume.artifact_valid
            ),
            artifact_error=artifact_error,
        )

    if selected_cap is None or selected_cap < minimum_cap:
        return result(
            "materialize-repairs",
            False,
            "higher-cap-required",
            planning=planning,
        )
    if withdrawn_materialization:
        # The same readiness gate a retained child set answers to. Re-authoring
        # into the plan that refused the last attempt would only reproduce the
        # withdrawal, so the cycle waits for the refusal to be cleared rather
        # than spending another materialization on it (ASI-6).
        standing = _unresolved_materialization_blocker(snapshot.plan_dir, disposition)
        if standing is not None:
            return result("materialize-repairs", False, standing, planning=planning)
    return result("materialize-repairs", True, None, planning=planning)
