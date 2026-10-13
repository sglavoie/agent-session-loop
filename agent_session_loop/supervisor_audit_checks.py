"""Deterministic checks over a supervisor audit's evidence bundle.

Reading the bundle is a separate job from collecting it. The deterministic
checks turn known failure modes into reproducible verdicts: each answers
``pass``, ``fail``, or ``unavailable`` under a stable invariant key and names
the evidence it read. Evidence a check needed but could not see is
``unavailable``, never ``pass``, so a shed log or an unwritten result file
reads as a gap in the audit rather than as a clean run.

The same rule runs the other way for a claim built on a record's *absence*. The
size cap sheds by usefulness, not at random, so the records it keeps are the
ones that read as evidence of a problem and the records it drops are often the
ones that would refute them. A check that infers a fault from a record it
cannot find must therefore ask whether the log was shed before it counts the
absence as evidence.

The checks read only the ``supervisor_audit_evidence_*`` modules; the agent
audit and the :mod:`.supervisor_audit` entry module read them. This module keeps
the check registry and the runners; the checks themselves live in the sibling
``supervisor_audit_checks_*`` modules, and the shared vocabulary, models and
helpers in ``supervisor_audit_vocabulary``, ``supervisor_audit_models`` and
``supervisor_audit_commit_records``."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import assurance_models_findings, assurance_models_vocab
from . import supervisor_audit_evidence_models as audit_evidence_models
from .assurance_models_findings import Finding
from .supervisor_audit_checks_anomalies import (
    _check_resource_anomalies,
    _check_unclassified_anomalies,
    _check_worker_pause_exits,
)
from .supervisor_audit_checks_commits import (
    _check_commit_after_verification,
    _check_commit_failure_accounting,
    _check_commit_group_resolution,
    _check_mutation_commit_guards,
)
from .supervisor_audit_checks_failures import (
    _check_conversation_restore_refusal,
    _check_operator_abort,
    _check_retryable_transport_failure,
)
from .supervisor_audit_checks_intent import (
    _check_repository_intent,
    _check_root_registry_agreement,
    _check_task_packet_paths,
)
from .supervisor_audit_checks_paused_work import _check_paused_work_settlement
from .supervisor_audit_checks_success import (
    _check_misleading_success,
    _check_retry_resume,
)
from .supervisor_audit_checks_terminal import (
    _check_state_event_agreement,
    _check_terminal_artifacts,
)
from .supervisor_audit_checks_verification import (
    _check_review_independence,
    _check_verification_outcomes,
)
from .supervisor_audit_models import CheckSpec, _CheckContext, _detail, _Verdict
from .supervisor_audit_vocabulary import _STATUS_PRECEDENCE
from .supervisor_audit_write_scope import _check_scope_escape, _check_scope_guard_catch

_CHECKS: tuple[tuple[CheckSpec, Callable[[_CheckContext], _Verdict]], ...] = (
    (
        CheckSpec(
            check="worker-pause-exits",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="worker-pause-disposition",
            invariant_key="lifecycle.worker-pause-exit-accounting",
            severity="medium",
            recommendation=(
                "Match the failed turn exit to its trusted worker pause and "
                "recorded held or rejected disposition before treating it "
                "as explained."
            ),
        ),
        _check_worker_pause_exits,
    ),
    (
        CheckSpec(
            check="root-registry-agreement",
            lane="identity-authority",
            category="identity",
            component="workspace-registry",
            invariant_key="identity.root-registry-agreement",
            severity="critical",
            recommendation=(
                "Re-bind the plan to the workspace it actually ran against, or "
                "re-register the repositories under the recorded root, before "
                "trusting any scope decision this run made."
            ),
        ),
        _check_root_registry_agreement,
    ),
    (
        CheckSpec(
            check="repository-intent",
            lane="identity-authority",
            category="identity",
            component="repository-intent",
            invariant_key="identity.repository-intent",
            severity="critical",
            recommendation=(
                "Declare a `## Repositories` table in manifest.md naming every "
                "repository the plan reaches, then re-run preflight."
            ),
            # The only check that replays the gate rather than reading its
            # recorded verdict: it walks the plan directory as it stands now.
            # A plan edited, published or deleted after the run answers for the
            # tree the replay is standing in, not for the run being judged.
            replayable=False,
        ),
        _check_repository_intent,
    ),
    (
        CheckSpec(
            check="task-packet-paths",
            lane="task-packet-coherence",
            category="packet",
            component="turn-artifacts",
            invariant_key="packet.artifact-path-agreement",
            severity="high",
            recommendation=(
                "Reconcile the paths written into the turn result with the "
                "directory the turn actually ran from."
            ),
        ),
        _check_task_packet_paths,
    ),
    (
        CheckSpec(
            check="scope-escape",
            lane="task-packet-coherence",
            category="packet",
            component="workspace-write-guard",
            invariant_key="packet.write-scope-containment",
            severity="critical",
            recommendation=(
                "Widen the session's declared writes or revert the out-of-scope "
                "change; do not resume until the guard passes on its own."
            ),
        ),
        _check_scope_escape,
    ),
    # Split from containment rather than expressed as a severity on the verdict:
    # severity lives on the spec, so one issue key cannot oscillate between
    # critical and medium across occurrences and corrupt the inbox's dedup
    # history for the escape that matters.
    (
        CheckSpec(
            check="scope-guard-catch",
            lane="task-packet-coherence",
            category="packet",
            component="workspace-write-guard-catch",
            invariant_key="packet.write-scope-guard-catch",
            severity="medium",
            recommendation=(
                "Widen the child's durable write scope to the aliases its own "
                "findings name, rerun expansion and preflight, then resume."
            ),
        ),
        _check_scope_guard_catch,
    ),
    (
        CheckSpec(
            check="retry-resume",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="pause-policy",
            invariant_key="lifecycle.retry-and-resume-consistency",
            severity="medium",
            recommendation=(
                "Persist the classification `pause_policy.classify_pause` returns "
                "for the recorded reason, and keep relaunches inside the budget "
                "the retry records name."
            ),
        ),
        _check_retry_resume,
    ),
    (
        CheckSpec(
            check="retryable-transport-failure",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="harness-failure-classifier",
            invariant_key="lifecycle.retryable-transport-failure",
            severity="high",
            recommendation=(
                "Keep the harness-failure classification and the pause policy "
                "telling the same story: a failure marked retryable must pause "
                "under a recoverable policy that needs no user and grants at "
                "least one relaunch, since the supervisor performs the retry "
                "rather than the run itself."
            ),
        ),
        _check_retryable_transport_failure,
    ),
    (
        CheckSpec(
            check="conversation-restore-refusal",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="conversation-recovery",
            invariant_key="lifecycle.conversation-restore-refusal",
            severity="high",
            recommendation=(
                "Retire the refused conversation and pause for explicit recovery; "
                "record the refusal and matching turn exit without claiming completion."
            ),
        ),
        _check_conversation_restore_refusal,
    ),
    (
        CheckSpec(
            check="operator-abort",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="run-control",
            invariant_key="lifecycle.operator-abort",
            severity="high",
            recommendation=(
                "Record the request and the reason behind every turn the run "
                "kills, so a killed turn traces to the operator who asked for "
                "it rather than reading as an unexplained failure."
            ),
        ),
        _check_operator_abort,
    ),
    (
        CheckSpec(
            check="review-independence",
            lane="verification-review-independence",
            category="review",
            component="review-harness",
            invariant_key="review.independent-verification",
            severity="high",
            recommendation=(
                "Run the review under a harness other than the one that did the "
                "work, and record a verdict only once the review completes."
            ),
        ),
        _check_review_independence,
    ),
    (
        CheckSpec(
            check="verification-outcomes",
            lane="verification-review-independence",
            category="verification",
            component="verification-recipe",
            invariant_key="verification.failure-accounting",
            severity="high",
            recommendation=(
                "Reconcile the recipe's outcome with the checks that ran under "
                "it: a verification may only pass when every failure it recorded "
                "belongs to a conditional diagnostic."
            ),
        ),
        _check_verification_outcomes,
    ),
    (
        CheckSpec(
            check="mutation-commit-guards",
            lane="mutation-commit-guards",
            category="guards",
            component="mutation-and-commit",
            invariant_key="guards.mutation-and-commit",
            severity="critical",
            recommendation=(
                "Capture and record repository fingerprints around every "
                "implementation turn, including legacy and commit-mode-none runs; "
                "classify worker HEAD moves and commit only inside established "
                "repository write scope."
            ),
        ),
        _check_mutation_commit_guards,
    ),
    # The check above asks where a worker's writes landed; this one asks
    # whether the commits among them outlived the turn that was refused. The
    # fingerprint is deliberately the one the agent lane already reports this
    # under, so a run graded by both lanes still folds into one issue.
    (
        CheckSpec(
            check="commit-after-verification",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-promotion",
            invariant_key="guards.commit-after-verification",
            severity="high",
            recommendation=(
                "Decide what the commits a refused turn left are: re-running "
                "the session builds the same work again on top of them, so "
                "either remove them before the next attempt or accept that the "
                "branch carries work no gate passed. Keeping the commit out of "
                "the worker's turn is what stops the choice from recurring."
            ),
        ),
        _check_commit_after_verification,
    ),
    (
        CheckSpec(
            check="commit-failure-accounting",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-failure",
            invariant_key="guards.commit-failure-accounting",
            severity="medium",
            recommendation=(
                "Record the outcome that classified the failed commit and the "
                "stream its reason was written to, so a failed commit is read "
                "from its own record instead of landing in the catch-all."
            ),
        ),
        _check_commit_failure_accounting,
    ),
    # Split from failure accounting rather than folded into it: that check asks
    # whether a failed commit explained itself, this one asks whether the
    # changes ever landed. A soft-failed commit can pass the first and fail
    # this one, and the inbox must be able to tell those two defects apart.
    (
        CheckSpec(
            check="commit-group-resolution",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-group-resolution",
            invariant_key="guards.commit-group-resolution",
            severity="high",
            recommendation=(
                "Commit the changes the named group still owes, or rerun the "
                "plan so the recovery path completes that group, before "
                "treating the run's changes as landed."
            ),
        ),
        _check_commit_group_resolution,
    ),
    # The settlement is graded apart from both because it is the one commit no
    # accepted turn produced: it commits work a failed turn retained, under a
    # pause, after the run's terminal record. Whether it explained itself and
    # whether it landed are the same question here, and neither of the checks
    # above reads the record that answers it.
    (
        CheckSpec(
            check="paused-work-settlement",
            lane="mutation-commit-guards",
            category="guards",
            component="paused-work-settlement",
            invariant_key="guards.paused-work-settlement",
            severity="high",
            recommendation=(
                "Read the run's paused_work_commit record before relaunching: "
                "commit or discard what the pause left in the worktree, so the "
                "next run's startup commit gate does not meet it as unexplained "
                "dirt."
            ),
        ),
        _check_paused_work_settlement,
    ),
    (
        CheckSpec(
            check="state-event-agreement",
            lane="truthfulness-observability",
            category="observability",
            component="state-and-events",
            invariant_key="observability.state-event-agreement",
            severity="high",
            recommendation=(
                "Write the terminal event and the state projection under one lock "
                "so an operator reading either sees the same outcome."
            ),
        ),
        _check_state_event_agreement,
    ),
    (
        CheckSpec(
            check="terminal-artifacts",
            lane="truthfulness-observability",
            category="observability",
            component="terminal-artifacts",
            invariant_key="observability.terminal-artifacts",
            severity="medium",
            recommendation=(
                "Ensure every turn writes its result file and that the run opens "
                "on run_started and closes on exactly one terminal record."
            ),
        ),
        _check_terminal_artifacts,
    ),
    (
        CheckSpec(
            check="misleading-success",
            lane="truthfulness-observability",
            category="observability",
            component="success-claim",
            invariant_key="observability.success-is-earned",
            severity="high",
            recommendation=(
                "Withhold the success claim until the anomalies, failed checks, "
                "and skipped checks it contradicts are resolved or explained."
            ),
        ),
        _check_misleading_success,
    ),
    (
        CheckSpec(
            check="resource-anomalies",
            lane="resources-stalls",
            category="resources",
            component="process-lifecycle",
            invariant_key="resources.stall-and-timeout",
            severity="medium",
            recommendation=(
                "Trace the stalled turn or surviving process to the descendant "
                "that outlived its turn, and close the handle that kept it alive."
            ),
        ),
        _check_resource_anomalies,
    ),
    (
        CheckSpec(
            check="unclassified-anomalies",
            lane="unknown-anomalies",
            category="anomalies",
            component="unclassified-evidence",
            invariant_key="anomalies.unclassified-evidence",
            severity="low",
            recommendation=(
                "Read the unclassified records directly, then either repair the "
                "damaged evidence or add a deterministic check that owns them."
            ),
        ),
        _check_unclassified_anomalies,
    ),
)


#: Every deterministic check, in the order results are reported.
DETERMINISTIC_CHECK_SPECS: tuple[CheckSpec, ...] = tuple(spec for spec, _ in _CHECKS)


def run_deterministic_checks(
    bundle: audit_evidence_models.AuditInput | Mapping[str, Any],
) -> tuple[assurance_models_findings.DeterministicResult, ...]:
    """Judge one evidence bundle against every reproducible check.

    Each check answers ``pass``, ``fail``, or ``unavailable`` under a stable
    invariant key and names the evidence it read. Evidence a check needed but
    could not see yields ``unavailable``: an audit that cannot look is never
    the same thing as an audit that looked and found nothing.
    """
    payload = (
        bundle.payload
        if isinstance(bundle, audit_evidence_models.AuditInput)
        else bundle
    )
    context = _CheckContext(payload)
    return tuple(
        assurance_models_findings.DeterministicResult(
            check=spec.check,
            lane=spec.lane,
            status=verdict.status,
            invariant_key=spec.invariant_key,
            evidence=verdict.evidence,
            detail=_detail(verdict.detail),
        )
        for spec, verdict in ((spec, run(context)) for spec, run in _CHECKS)
    )


def findings_from_results(
    results: Iterable[assurance_models_findings.DeterministicResult],
) -> tuple[Finding, ...]:
    """Raise each failing check into a finding under the check's fingerprint."""
    specs = {spec.check: spec for spec in DETERMINISTIC_CHECK_SPECS}
    findings = []
    for result in results:
        spec = specs.get(result.check)
        if result.status != "fail" or spec is None:
            continue
        findings.append(
            Finding(
                category=spec.category,
                component=spec.component,
                invariant_key=spec.invariant_key,
                lane=spec.lane,
                severity=spec.severity,
                summary=result.detail or f"the {spec.check} check failed",
                recommendation=spec.recommendation,
                evidence=result.evidence,
            )
        )
    return tuple(findings)


def lane_rollup(
    results: Iterable[assurance_models_findings.DeterministicResult],
) -> dict[str, str]:
    """Reduce results to one status per rubric lane, keeping the worst.

    A lane no check reported on rolls up as ``unavailable`` rather than as
    ``pass``, so a gap in coverage reads as a gap.
    """
    worst: dict[str, str] = {}
    for result in results:
        current = worst.get(result.lane)
        if (
            current is None
            or _STATUS_PRECEDENCE[result.status] > _STATUS_PRECEDENCE[current]
        ):
            worst[result.lane] = result.status
    return {
        lane: worst.get(lane, "unavailable")
        for lane in assurance_models_vocab.RUBRIC_LANES
    }
