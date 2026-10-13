"""Recovery builder for review-cycle continuation and resume artifacts."""

from __future__ import annotations

from . import (
    review_continuation,
    run_inputs,
)
from .workbench_recovery_actions import _inspect_action, _profile_action
from .workbench_recovery_models import (
    RecoveryActionCard,
    RecoveryArtifactReference,
    RecoveryEvidenceRow,
    RecoverySeverity,
    WorkbenchRecovery,
    _RecoveryBuildContext,
)
from .workbench_recovery_payload import _build_context_recovery_payload


def _review_resume_artifacts(
    context: _RecoveryBuildContext,
) -> tuple[RecoveryArtifactReference, ...]:
    artifacts = list(context.base_artifacts)
    for filename, label, kind in (
        ("final-review.md", "Final review", "review-output"),
        ("final-review.json", "Structured review", "review-artifact"),
    ):
        path = context.snapshot.plan_dir / filename
        try:
            present = path.is_file()
        except OSError:
            present = False
        if present:
            artifacts.append(
                RecoveryArtifactReference(
                    label=label,
                    path=path,
                    kind=kind,
                    source="review-continuation",
                )
            )
    return tuple(artifacts)


def _build_review_continuation_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery | None:
    resume = context.review_resume
    if resume.action == "initial-review":
        return None
    if (
        resume.action == "terminal"
        and resume.blocker in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
    ):
        return None

    evidence = [
        RecoveryEvidenceRow(
            label="Review continuation",
            value=resume.action,
            detail=resume.review_session_id,
            severity="info",
        ),
        RecoveryEvidenceRow(
            label="Historical materializations",
            value=str(resume.historical_materializations),
            severity="info",
        ),
    ]
    actions: tuple[RecoveryActionCard, ...]

    def profile_guidance(*, include_cap: bool) -> str:
        guidance = (
            "Configure a review harness distinct from the implementation "
            "harness so the next launch supplies --review-harness."
        )
        if include_cap:
            guidance += (
                " Set max review-fix cycles to "
                f"{resume.minimum_required_cap} or higher so the next launch "
                "supplies --max-review-fix-cycles."
            )
        return guidance

    if resume.blocker == "higher-cap-required":
        if resume.selected_cap is None:
            title = "Review-fix cap required"
            summary = (
                "Review repairs can resume after supplying "
                "--max-review-fix-cycles with a value of "
                f"{resume.minimum_required_cap} or higher."
            )
            cap_detail = "No review-fix cap has been recorded"
            action_description = profile_guidance(include_cap=True)
        else:
            title = "Higher review-fix cap required"
            summary = (
                "Review repair budget is exhausted at the selected cap of "
                f"{resume.selected_cap}; increase --max-review-fix-cycles "
                f"to {resume.minimum_required_cap} or higher."
            )
            cap_detail = f"Selected cap: {resume.selected_cap}"
            action_description = profile_guidance(include_cap=True)
        summary += (
            " Configure a review harness distinct from the implementation "
            "harness; without --review-harness, review continuation will not "
            "run."
        )
        severity: RecoverySeverity = "warning"
        evidence.append(
            RecoveryEvidenceRow(
                label="Minimum review-fix cap",
                value=str(resume.minimum_required_cap),
                detail=cap_detail,
                severity="warning",
            )
        )
        actions = (_profile_action(action_description),)
    elif resume.blocker == "artifact-invalid":
        title = "Review artifact needs correction"
        summary = resume.artifact_error or (
            "The structured review artifact is not safely materializable."
        )
        severity = "error"
        evidence.append(
            RecoveryEvidenceRow(
                label="Artifact validation",
                value="invalid",
                detail=resume.artifact_error,
                severity="error",
            )
        )
        actions = (
            _inspect_action(
                "Inspect final-review.md and final-review.json, then rerun."
            ),
        )
    elif resume.action == "terminal":
        title = "Review continuation is inconsistent"
        summary = (
            "Recorded review repair history cannot be resumed safely: "
            f"{resume.blocker or 'unknown blocker'}."
        )
        severity = "error"
        evidence.append(
            RecoveryEvidenceRow(
                label="Continuation blocker",
                value=resume.blocker,
                severity="error",
            )
        )
        actions = (_inspect_action("Inspect review events and generated sessions."),)
    else:
        title = "Review continuation ready"
        if resume.action == "materialize-repairs":
            summary = (
                "Rerun the plan to materialize repairs from review session "
                f"{resume.review_session_id}."
            )
        elif resume.action == "run-repairs":
            summary = "Rerun the plan to execute pending review repairs."
        else:
            summary = "Rerun the plan to review the completed repair sessions again."
        summary += (
            " Configure a review harness distinct from the implementation "
            "harness before launching; without --review-harness, review "
            "continuation will not run."
        )
        cap_exceeds_default = (
            resume.action == "materialize-repairs"
            and resume.minimum_required_cap > run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES
        )
        exhausted_group = (
            review_continuation.exhausted_repair_group_details(resume.to_payload())
            if cap_exceeds_default
            else None
        )
        if exhausted_group is not None:
            repair_group, attempts = exhausted_group
            summary += (
                f' The recurring repair group "{repair_group}" already hit '
                f"{attempts} {'attempt' if attempts == 1 else 'attempts'}; "
                "fix it manually or re-split instead of raising "
                "--max-review-fix-cycles."
            )
        elif cap_exceeds_default:
            summary += (
                " Set --max-review-fix-cycles to "
                f"{resume.minimum_required_cap} or higher as well."
            )
        severity = "info"
        actions = (
            (
                _inspect_action(
                    "Inspect the recurring repair group and repair it "
                    "manually or re-split before relaunching."
                ),
            )
            if exhausted_group is not None
            else (_profile_action(profile_guidance(include_cap=cap_exceeds_default)),)
        )

    return _build_context_recovery_payload(
        context,
        kind="review-continuation",
        title=title,
        summary=summary,
        severity=severity,
        recoverable=True,
        artifacts=_review_resume_artifacts(context),
        evidence=tuple(evidence),
        actions=actions,
    )
