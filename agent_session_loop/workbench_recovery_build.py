"""Recovery Center cards: a read-only diagnosis of why a plan is stuck.

``build_workbench_recovery`` turns one plan's files, runner state, lock state,
commit artifacts, verification payloads and preflight repair hints into a
deterministic ``WorkbenchRecovery`` card: what kind of blocker it is, the
evidence behind that, and the actions and copyable commands that clear it.
Nothing here persists a diagnosis or calls a harness, so the card is recomputed
on every read.

The module sits below ``workbench_summary``: the per-plan summary embeds the compact
card and shares a few state-reading helpers defined here, so ``workbench``
imports this module and never the reverse. ``DiscoveredPlan`` is imported for
type checking only for that reason.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    events,
    plans_errors,
    plans_snapshot,
    qna,
)
from .workbench_recovery_builders_run import (
    _build_active_run_recovery,
    _build_commit_failed_recovery,
    _build_commit_preflight_recovery,
    _build_harness_config_recovery,
    _build_not_loop_ready_recovery,
    _build_verification_failure_recovery,
    _is_harness_config_recovery,
)
from .workbench_recovery_builders_state import (
    _build_clean_none_recovery,
    _build_failed_finished_recovery,
    _build_reply_needed_recovery,
    _build_stale_lock_recovery,
    _build_stale_state_recovery,
)
from .workbench_recovery_conversation import _build_conversation_recovery
from .workbench_recovery_models import RecoveryEvidenceRow, WorkbenchRecovery
from .workbench_recovery_payload import _VERIFICATION_FAILURE_REASONS
from .workbench_recovery_review import _build_review_continuation_recovery
from .workbench_recovery_state import (
    _build_recovery_context,
    _expired_wait_qna_context,
    _pause_reason_recovery_kind,
    _payload_mapping,
    _recovery_base_artifacts,
    _summary_base_paths,
)

if TYPE_CHECKING:
    from .workbench_registry_models import DiscoveredPlan


def build_workbench_recovery(
    plan: DiscoveredPlan,
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> WorkbenchRecovery:
    """Build a deterministic, read-only recovery diagnosis for one plan.

    ``reclaim=False`` suppresses the stale-lock hygiene the shared lock reader
    performs by default, so an observational caller leaves the corpus untouched.
    """
    if not plan.exists:
        return WorkbenchRecovery(
            plan_dir=plan.plan_dir,
            kind="generic",
            title="Plan cannot be inspected",
            summary=f"Plan directory does not exist: {plan.plan_dir}",
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Plan directory",
                    value=str(plan.plan_dir),
                    severity="error",
                ),
            ),
        )

    try:
        snapshot = plans_snapshot.inspect_plan(plan.plan_dir)
    except plans_errors.PlanError as error:
        state_path, events_path, progress_path = _summary_base_paths(
            plan.plan_dir, state_dir
        )
        return WorkbenchRecovery(
            plan_dir=plan.plan_dir,
            kind="generic",
            title="Plan cannot be inspected",
            summary=str(error),
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Plan error",
                    value=str(error),
                    severity="error",
                ),
            ),
            artifacts=_recovery_base_artifacts(
                state_path=state_path,
                events_path=events_path,
                progress_path=progress_path,
            ),
        )

    try:
        context = _build_recovery_context(snapshot, state_dir, reclaim=reclaim)
    except (plans_errors.PlanError, OSError, ValueError) as error:
        state_path, events_path, progress_path = _summary_base_paths(
            snapshot.plan_dir, state_dir
        )
        return WorkbenchRecovery(
            plan_dir=snapshot.plan_dir,
            kind="generic",
            title="Workspace metadata cannot be inspected",
            summary=str(error),
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Workspace metadata error",
                    value=str(error),
                    severity="error",
                ),
            ),
            artifacts=_recovery_base_artifacts(
                state_path=state_path,
                events_path=events_path,
                progress_path=progress_path,
            ),
        )
    expired_wait_context = _expired_wait_qna_context(
        snapshot,
        context.runner_state,
        state_dir,
        reclaim=reclaim,
    )
    if expired_wait_context is not None:
        context = replace(
            context,
            pause_reason=expired_wait_context.pause_reason,
            pause_details=expired_wait_context.pause_details,
            pause_message=(
                expired_wait_context.pause_message or qna.EXPIRED_WAIT_PAUSE_REASON
            ),
        )

    active_turn = _payload_mapping(context.raw_state, "active_turn")
    active_verification = _payload_mapping(context.raw_state, "active_verification")
    lock_info = context.lock_info
    if (
        lock_info is not None
        and lock_info.active
        and (active_turn is not None or active_verification is not None)
    ):
        return _build_active_run_recovery(context, selected_turn=active_turn)

    reason = context.pause_reason
    details = context.pause_details
    recovery_kind = _pause_reason_recovery_kind(reason)

    if expired_wait_context is not None:
        return _build_reply_needed_recovery(context, reason=reason)

    if context.runner_state.get("paused") is True:
        if reason == events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR:
            continuation = _build_review_continuation_recovery(context)
            if continuation is not None:
                return continuation

        if recovery_kind == "not-loop-ready":
            return _build_not_loop_ready_recovery(context)

        if recovery_kind in _VERIFICATION_FAILURE_REASONS:
            return _build_verification_failure_recovery(context, reason=recovery_kind)

        if recovery_kind == "commit-preflight":
            return _build_commit_preflight_recovery(context)

        if recovery_kind == "commit-failed":
            return _build_commit_failed_recovery(context)

        if recovery_kind == "conversation-recovery":
            return _build_conversation_recovery(
                context, reason=reason, state_dir=state_dir
            )

        if _is_harness_config_recovery(reason=reason, details=details):
            return _build_harness_config_recovery(context, reason=reason)

        if recovery_kind == "generic":
            return _build_failed_finished_recovery(context, reason=reason)

        return _build_reply_needed_recovery(context, reason=reason)

    if context.stale_unfinished:
        return _build_stale_state_recovery(context)

    if lock_info is not None and lock_info.stale:
        return _build_stale_lock_recovery(context)

    if (
        context.runner_state.get("present")
        and context.runner_state.get("run_finished_at")
        and context.runner_state.get("ok") is False
    ):
        if reason == events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR:
            continuation = _build_review_continuation_recovery(context)
            if continuation is not None:
                return continuation

        if recovery_kind == "commit-failed":
            return _build_commit_failed_recovery(context)

        if recovery_kind in _VERIFICATION_FAILURE_REASONS:
            return _build_verification_failure_recovery(context, reason=recovery_kind)

        return _build_failed_finished_recovery(context, reason=reason)

    continuation = _build_review_continuation_recovery(context)
    if continuation is not None:
        return continuation
    return _build_clean_none_recovery(context)
