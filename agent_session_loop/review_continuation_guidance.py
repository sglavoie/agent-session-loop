"""Resume projection construction and operator guidance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    plans_content_hash,
    plans_errors,
    plans_readiness,
    review,
    review_repair,
)
from .review_continuation_groups import (
    exhausted_repair_group_guidance,
)
from .review_continuation_models import (
    REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
    TERMINAL_REVIEW_DISPOSITIONS,
    ReviewResume,
    ReviewResumeAction,
    _nonempty_str,
    _string_tuple,
)


def _resume(
    *,
    source_cycle: int | None,
    source_id: str | None,
    disposition: str | None,
    action: ReviewResumeAction,
    eligible: bool,
    blocker: str | None,
    materialization_count: int,
    selected_cap: int | None,
    max_repair_group_attempts: int | None = None,
    repair_group_attempts: Mapping[str, int] | None = None,
    repair_ids: tuple[str, ...] = (),
    pending_ids: tuple[str, ...] = (),
    completed_ids: tuple[str, ...] = (),
    source_fingerprints: tuple[str, ...] | None = None,
    source_findings: tuple[Mapping[str, Any], ...] | None = None,
    source_repositories: tuple[Mapping[str, Any], ...] | None = None,
    planning: review_repair.ReviewRepairPlan | None = None,
) -> ReviewResume:
    current_fingerprints: tuple[str, ...] | None = None
    current_repeat_keys: tuple[str, ...] | None = None
    current_output_digest: str | None = None
    current_artifact_digest: str | None = None
    artifact_valid: bool | None = None
    artifact_error: str | None = None
    current_findings: tuple[Mapping[str, Any], ...] | None = None
    current_repositories: tuple[Mapping[str, Any], ...] | None = None
    repair_groups: tuple[Mapping[str, Any], ...] | None = None
    if planning is not None:
        repository_aware = (
            bool(planning.repository_bases)
            or any(group.read_repositories for group in planning.repair_groups)
            or any(
                isinstance(finding.get("repo"), str)
                for finding in planning.validated_findings
            )
        )
        current_fingerprints = (
            planning.finding_fingerprints
            if planning.ok or planning.finding_fingerprints
            else None
        )
        if current_fingerprints is not None:
            current_repeat_keys = (
                review.finding_repeat_keys(list(planning.validated_findings)) or ()
            )
        current_output_digest = planning.review_output_sha256
        current_artifact_digest = planning.review_artifact_sha256
        artifact_valid = planning.ok
        artifact_error = planning.error
        if repository_aware and planning.validated_findings:
            current_findings = tuple(
                dict(finding) for finding in planning.validated_findings
            )
        if planning.repository_bases:
            current_repositories = tuple(
                dict(repository) for repository in planning.repository_bases
            )
        if planning.repair_groups:
            repair_groups = tuple(
                dict(
                    {
                        "repair_group": group.repair_group,
                        "finding_ids": list(group.finding_ids),
                        "repair_session_id": group.proposed_child_id.value,
                    },
                    **(
                        {
                            "read_repositories": list(group.read_repositories),
                            "write_repositories": list(group.write_repositories),
                        }
                        if repository_aware
                        else {}
                    ),
                )
                for group in planning.repair_groups
            )
        if action in {"run-repairs", "rereview"} and planning.existing_child_ids:
            # Existing children are authoritative for these lifecycle states.
            # Planning stops at that expected conflict before it can establish
            # full materializability, so do not present the conflict as proof
            # that the structured artifact itself is invalid.
            artifact_valid = None
            artifact_error = None
    return ReviewResume(
        source_cycle=source_cycle,
        review_session_id=source_id,
        source_disposition=disposition,
        action=action,
        eligible=eligible,
        blocker=blocker,
        historical_materializations=materialization_count,
        minimum_required_cap=materialization_count + 1,
        selected_cap=selected_cap,
        max_repair_group_attempts=max_repair_group_attempts,
        repair_group_attempts=(
            {
                group: repair_group_attempts[group]
                for group in sorted(repair_group_attempts)
            }
            if repair_group_attempts
            else None
        ),
        repair_session_ids=repair_ids,
        pending_repair_session_ids=pending_ids,
        completed_repair_session_ids=completed_ids,
        source_fingerprints=source_fingerprints,
        current_fingerprints=current_fingerprints,
        current_repeat_keys=current_repeat_keys,
        current_review_output_sha256=current_output_digest,
        current_review_artifact_sha256=current_artifact_digest,
        artifact_valid=artifact_valid,
        artifact_error=artifact_error,
        source_findings=source_findings,
        current_findings=current_findings,
        source_repositories=source_repositories,
        current_repositories=current_repositories,
        repair_groups=repair_groups,
    )


def _repair_scope_widenings(review_resume: Mapping[str, Any]) -> tuple[str, ...]:
    """Render one ``<child> needs <aliases>`` fragment per stale repair scope."""
    gaps = review_resume.get("repair_scope_gaps")
    if not isinstance(gaps, Sequence) or isinstance(gaps, str):
        return ()
    fragments: list[str] = []
    for gap in gaps:
        if not isinstance(gap, Mapping):
            continue
        repair_session_id = _nonempty_str(gap.get("repair_session_id"))
        missing = _string_tuple(gap.get("missing_write_repositories"))
        if repair_session_id is None or not missing:
            continue
        fragments.append(f"{repair_session_id} needs {', '.join(missing)}")
    return tuple(fragments)


def review_resume_guidance(
    review_resume: Mapping[str, Any] | None,
    *,
    review_harness_active: bool,
) -> str | None:
    """Return concise operator guidance for a continuation projection.

    ``review_resume`` is a :meth:`ReviewResume.to_payload` mapping (or ``None``
    when the surface carries no projection).  ``review_harness_active`` is True
    when the surfacing command already ran with a review harness, so the
    guidance omits the "resume with a review harness" prerequisite.  This is the
    single renderer shared by the runner, the CLI, and the autopilot helper so
    all three describe the same continuation identically.
    """
    if not isinstance(review_resume, Mapping):
        return None
    if (
        review_resume.get("blocker")
        in {REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED, "higher-cap-required"}
        or review_resume.get("action") == "materialize-repairs"
    ):
        exhausted_guidance = exhausted_repair_group_guidance(review_resume)
        if exhausted_guidance is not None:
            return exhausted_guidance
    needs_review_harness = not review_harness_active
    blocker = review_resume.get("blocker")
    if blocker == "higher-cap-required":
        minimum = review_resume.get("minimum_required_cap")
        if type(minimum) is int:
            requirements = []
            if needs_review_harness:
                requirements.append("a review harness")
            requirements.append(f"--max-review-fix-cycles {minimum} or higher")
            return "resume the same plan with " + " and ".join(requirements)
    if blocker == "artifact-invalid":
        artifact_error = review_resume.get("artifact_error")
        detail = f": {artifact_error}" if artifact_error else ""
        guidance = f"correct the review artifact{detail}"
        if needs_review_harness:
            guidance += ", then resume the same plan with a review harness"
        return guidance
    if blocker == "repair-scope-stale":
        widenings = _repair_scope_widenings(review_resume)
        if widenings:
            return (
                "widen **Writes repositories** in the pending repair children, "
                "then resume the same plan: " + "; ".join(widenings)
            )
    if review_resume.get("action") == "terminal" and blocker:
        if blocker in TERMINAL_REVIEW_DISPOSITIONS:
            return None
        return f"no automatic continuation ({blocker})"
    if review_resume.get("eligible") is True:
        action = review_resume.get("action") or "review"
        minimum = review_resume.get("minimum_required_cap")
        cap_guidance = (
            f" and --max-review-fix-cycles {minimum} or higher"
            if action == "materialize-repairs" and type(minimum) is int
            else ""
        )
        command_guidance = (
            "resume the same plan with a review harness"
            if needs_review_harness
            else "rerun the same review-enabled command"
        )
        return (
            f"{command_guidance}{cap_guidance} "
            f"to run the eligible {action} continuation"
        )
    return None


def _unresolved_materialization_blocker(
    plan_dir: Path, disposition: str | None
) -> str | None:
    """Whether the gate that refused the last materialization still stands.

    Repair children are written to disk before the executability gate judges
    them, so a refused materialization leaves runnable-looking pending sessions
    behind and the continuation would call them eligible while the run is still
    paused on the refusal. Readiness is the resolution signal: the refusal left
    the plan at ``draft``, and clearing it is what re-marks it loop-ready.

    ``running`` clears it too. A run that reaches this projection has already
    passed its own startup readiness gate and, on the strength of that pass,
    transitioned the plan from ``loop-ready`` to ``running`` before entering the
    session loop. Demanding ``loop-ready`` here would therefore refuse every
    resumed run whose children were left pending by an earlier refusal -- the
    operator marks the plan loop-ready, the run demotes it, and the same blocker
    returns. The SHA comparison below is what keeps ``running`` honest: it still
    proves the plan is structurally the one that was gated.
    """
    if disposition != REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY:
        return None
    try:
        readiness = plans_readiness.read_readiness_metadata(plan_dir)
        if readiness is None or readiness.state not in ("loop-ready", "running"):
            return REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
        if readiness.readiness_sha256 != plans_content_hash.stable_readiness_sha256(
            plan_dir
        ):
            return REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
    except plans_errors.PlanError:
        return REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
    return None
