"""Focused implementation extracted from :mod:`.supervisor_audit`."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    assurance_models_artifacts,
    assurance_models_findings,
    assurance_models_primitives,
    assurance_models_summary,
    assurance_models_vocab,
    assurance_projection,
    harness_failures,
    harnesses,
    model_availability,
    state,
)
from . import supervisor_audit_agent as audit_agent
from . import supervisor_audit_checks as audit_checks
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from . import supervisor_audit_evidence_input as audit_evidence_input
from . import supervisor_audit_evidence_models as audit_evidence_models
from .assurance_models_summary import AuditRole
from .assurance_models_vocab import (
    AttemptOutcome,
)
from .supervisor_audit_agent_models import (
    ATTEMPT_MESSAGE_FILE,
    ATTEMPT_OUTCOME_FILE,
    ATTEMPT_PROMPT_FILE,
    ATTEMPT_STDERR_FILE,
    ATTEMPT_STDOUT_FILE,
    IDENTITY_AMBIGUOUS_OUTCOME,
    REASON_IDENTITY_AMBIGUOUS,
    RETRYABLE_ANSWER_OUTCOMES,
    RETRYABLE_DELIVERY_OUTCOMES,
    AgentAuditOutcome,
)
from .supervisor_audit_config import (
    AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS,
    REASON_INTERRUPTED,
    AuditCancelled,
)
from .supervisor_audit_restricted import (
    AuditAttemptResult,
    run_restricted_audit,
)


def _reason_token(value: Any, fallback: str) -> str:
    """Keep a reason only when it groups; a raw exception string never does.

    ``run_restricted_audit`` reports launch failures with the harness error's
    own text, which differs on every machine. Falling back to the attempt
    outcome keeps one recurring failure deduplicating into one inbox issue.
    """
    try:
        return assurance_models_primitives.parse_reason_token(value)
    except assurance_models_primitives.AssuranceError:
        return fallback


def _bounded_argv(argv: Sequence[str], *, prompt: str) -> list[str]:
    """Replace the inlined prompt with its digest so an attempt record stays small."""
    return [
        f"[prompt sha256:{audit_evidence_helpers._sha256_text(prompt)}]"
        if item == prompt
        else item
        for item in argv
    ]


def _write_attempt_artifacts(
    run_dir: Path,
    number: int,
    *,
    prompt: str,
    attempt: AuditAttemptResult,
    outcome: AttemptOutcome,
    detail: str | None,
    durable: bool,
) -> str | None:
    """Archive one attempt, returning its locator, or ``None`` when it cannot.

    Best effort by design, exactly like the workspace-change artifact: losing the
    archive of an attempt must not lose the verdict the attempt produced.
    """
    try:
        directory = assurance_models_artifacts.create_attempt_dir(run_dir, number)
        assurance_models_artifacts.write_new_bytes(
            directory / ATTEMPT_PROMPT_FILE, prompt.encode("utf-8"), durable=durable
        )
        assurance_models_artifacts.write_new_bytes(
            directory / ATTEMPT_MESSAGE_FILE,
            audit_evidence_helpers._truncate_utf8(
                attempt.last_message, audit_agent.MAX_AGENT_OUTPUT_BYTES
            ).encode("utf-8"),
            durable=durable,
        )
        for name, stream in (
            (ATTEMPT_STDOUT_FILE, attempt.stdout_tail),
            (ATTEMPT_STDERR_FILE, attempt.stderr_tail),
        ):
            if stream:
                assurance_models_artifacts.write_new_bytes(
                    directory / name, stream.encode("utf-8"), durable=durable
                )
        assurance_models_artifacts.write_new_bytes(
            directory / ATTEMPT_OUTCOME_FILE,
            assurance_models_primitives.canonical_json_bytes(
                {
                    "number": number,
                    "outcome": outcome,
                    "detail": detail,
                    "reason": attempt.reason,
                    "returncode": attempt.returncode,
                    "workspace_change": attempt.workspace_change,
                    "argv": _bounded_argv(attempt.argv, prompt=prompt),
                    "classification": (
                        None
                        if attempt.classification is None
                        else attempt.classification.to_dict()
                    ),
                }
            ),
            durable=durable,
        )
    except OSError, assurance_models_primitives.AssuranceError:
        return None
    return assurance_models_artifacts.attempt_dir_name(number)


def _delivery_is_retryable(attempt: AuditAttemptResult) -> bool:
    """Say whether relaunching this failed attempt could plausibly succeed.

    A timeout carries no classification and is retried on its own terms. Every
    other delivery failure defers to the shared classifier, which recognises
    only genuine transport faults as transient: authentication, quota, and
    unrecognised exits are permanent, so waiting out the backoff would just
    stall the run before reaching the same verdict.
    """
    classification = attempt.classification
    return classification is None or classification.retryable


def _refused_for_size(attempt: AuditAttemptResult) -> bool:
    """Say whether the provider rejected this attempt's request as too large.

    Read off the classification the launch already carried rather than by
    re-matching the harness text, so the rule that recognises the refusal lives
    in one place and the audit and the session loop agree about what happened.
    """
    classification = attempt.classification
    return (
        classification is not None
        and classification.marker == harness_failures.CONTEXT_OVERFLOW_MARKER
    )


def _record_capacity_outage(
    attempt: AuditAttemptResult,
    *,
    role: AuditRole,
    state_dir: Path | str | None,
    run_id: str,
    delay: float,
) -> None:
    """Note a provider capacity outage in the shared availability ledger.

    Recording it is all that happens: the auditor is never swapped for a model
    that happens to be up, because its identity is a structural property of the
    audit rather than a performance knob. The ledger entry is durable evidence
    that the outage was the provider's, and it expires on its own once the
    backoff this audit is about to wait has passed.
    """
    classification = attempt.classification
    if (
        classification is None
        or classification.marker != harness_failures.MODEL_CAPACITY_MARKER
        or role.model is None
    ):
        return
    with contextlib.suppress(OSError):
        model_availability.suppress_until(
            state.state_root(state_dir),
            role.model,
            reason=harness_failures.MODEL_CAPACITY_MARKER,
            retry_hint=classification.retry_at or str(int(delay)),
            run_id=run_id,
        )


def run_agent_audit(
    role: AuditRole,
    *,
    bundle: audit_evidence_models.AuditInput,
    run_id: str,
    run_dir: Path | str,
    workspace_root: Path | str,
    plan_dir: Path | str,
    repositories: Sequence[str],
    registry_declared: bool = True,
    state_dir: Path | str | None = None,
    results: Sequence[assurance_models_findings.DeterministicResult] | None = None,
    stream_callback: harnesses.StreamCallback | None = None,
    abort: harnesses.AbortSignal | None = None,
    durable: bool = True,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
    _sleep_fn: Callable[[float], None] = time.sleep,
) -> AgentAuditOutcome:
    """Run the semantic audit and return the audit its attempts earned.

    Two budgets are spent, not one, because a bad answer and an undelivered
    request are different failures. The agent may produce at most
    :data:`assurance_models_vocab.MAX_AUDIT_ATTEMPTS` unusable *answers*, each retry a
    fresh invocation told only which validation rule the previous one broke. A
    launch that never reached the agent — a timeout or a transport/capacity
    failure — is charged instead to
    :data:`assurance_models_vocab.MAX_AUDIT_DELIVERY_ATTEMPTS` and retried after a
    backoff with the identical prompt, since there is no answer to correct and
    the outage is the provider's doing. When either budget runs out the audit is
    ``unavailable`` under the reason that exhausted it — but it still carries the
    deterministic lanes, results, and findings, because losing reproducible
    evidence because an agent misbehaved would be a second failure, not a
    consequence of the first.

    A third budget,
    :data:`assurance_models_vocab.MAX_AUDIT_SHRINK_ATTEMPTS`, covers the one failure
    that is the caller's own: a provider that refuses the request for size
    before the agent runs. That attempt is retried with the bundle shed to
    :data:`AUDIT_INPUT_OVERFLOW_SHRINK_RATIO` of the size that was refused. Only
    the prompt shrinks — the canonical bundle, the deterministic verdicts read
    from it, and the locators the answer is validated against all stay the ones
    written to the run directory, so the retry sends less without grading a
    different run.

    A candidate retry comes out of the answer budget rather than adding to it. An
    answer that is malformed once and then identity-ambiguous therefore never
    sees the candidate list, and the audit lands ``unavailable`` under
    :data:`REASON_IDENTITY_AMBIGUOUS`. That is the safe end of the trade: the
    cost of not asking is one unpublished audit, while a third turn per audit is
    a cost every run pays. Nothing is merged and nothing is allocated either way.

    ``abort`` cancels the whole audit rather than one attempt: it is checked
    before each launch, so a cancel that arrives during a transport backoff is
    honored at the end of that wait instead of buying another launch.

    Every attempt checks the live workspace on both sides of its own turn.
    Observed activity invalidates the verdict without attributing that activity
    to the auditor; it is not retried automatically over a changing workspace.
    """
    run_dir = Path(run_dir)
    clock = datetime.now(UTC) if now is None else now()
    verdicts = (
        tuple(results)
        if results is not None
        else audit_checks.run_deterministic_checks(bundle)
    )
    declared = [entry.locator for entry in bundle.evidence]
    # Read once, before the first attempt: both attempts must be judged against
    # the catalog they were shown, and re-reading between them could reject a
    # selection for naming an issue a concurrent publisher had just moved.
    inbox = audit_agent.read_identity_catalog(state_dir)
    catalog = audit_agent.build_agent_catalog(inbox)
    attempts: list[assurance_models_summary.AuditAttempt] = []
    correction: str | None = None
    candidates: tuple[assurance_projection.IdentityCandidate, ...] = ()
    candidates_offered = False
    unavailable_reason = audit_agent.REASON_AGENT_UNAVAILABLE
    answers_spent = 0
    deliveries_spent = 0
    shrinks_spent = 0
    # What the prompt is built from, which the oversized-input retry may narrow.
    # ``bundle`` itself never moves: it is the artifact on disk, the input the
    # deterministic verdicts were read from, and the source of ``declared``.
    prompt_bundle = bundle

    while True:
        if abort is not None and abort.armed:
            raise AuditCancelled(REASON_INTERRUPTED)
        number = len(attempts) + 1
        prompt = audit_agent.build_audit_prompt(
            prompt_bundle, catalog=catalog, correction=correction, candidates=candidates
        )
        attempt = run_restricted_audit(
            role,
            run_dir=run_dir,
            run_id=run_id,
            workspace_root=workspace_root,
            plan_dir=plan_dir,
            prompt=prompt,
            repositories=repositories,
            registry_declared=registry_declared,
            state_dir=state_dir,
            stream_callback=stream_callback,
            abort=abort,
            now=now,
            _run_harness_fn=_run_harness_fn,
        )
        agent: audit_agent.AgentAudit | None = None
        matched: tuple[assurance_projection.IdentityCandidate, ...] = ()
        outcome: AttemptOutcome
        detail: str | None
        if attempt.outcome == "ok":
            try:
                answer = audit_agent.parse_agent_audit(
                    attempt.last_message, declared=declared, catalog=catalog
                )
            except audit_agent.AgentOutputError as error:
                outcome, detail = error.outcome, error.detail
                unavailable_reason = error.outcome
            else:
                resolution = audit_agent.resolve_identities(answer, catalog=inbox)
                outcome = "ok"
                detail = None
                if resolution.settled:
                    agent = resolution.audit
                else:
                    outcome = IDENTITY_AMBIGUOUS_OUTCOME
                    detail = resolution.detail
                    unavailable_reason = REASON_IDENTITY_AMBIGUOUS
                    # The candidate list is offered once. Answering `new` again
                    # after seeing it is the auditor's decision, not a failure to
                    # understand the question, so repeating it would only spend
                    # another turn arriving at the same fail-closed verdict.
                    matched = () if candidates_offered else resolution.candidates
        else:
            outcome, detail = attempt.outcome, attempt.reason
            unavailable_reason = _reason_token(attempt.reason, attempt.outcome)
        attempts.append(
            assurance_models_summary.AuditAttempt(
                number=number,
                outcome=outcome,
                artifact_dir=_write_attempt_artifacts(
                    run_dir,
                    number,
                    prompt=prompt,
                    attempt=attempt,
                    outcome=outcome,
                    detail=detail,
                    durable=durable,
                ),
                detail=None if detail is None else audit_agent._inline(detail),
            )
        )
        if agent is not None:
            rendered = audit_agent.render_supervisor_audit(
                run_id=run_id,
                bundle=bundle,
                results=verdicts,
                generated_at=clock.isoformat(),
                agent=agent,
            )
            return AgentAuditOutcome(
                audit=rendered,
                attempts=tuple(attempts),
                agent_covered=True,
                agent=agent,
                catalog=catalog,
                identity_selections=audit_agent._identity_selections(
                    agent, rendered.findings
                ),
            )
        if _refused_for_size(attempt) and (
            shrinks_spent < assurance_models_vocab.MAX_AUDIT_SHRINK_ATTEMPTS
        ):
            # The provider refused the request before the agent ran, so there is
            # no answer to correct and no outage to wait out: what has to change
            # is how much was sent. Retry immediately with a bundle shed further,
            # and only if shedding actually gave something up — an unshrinkable
            # bundle would otherwise buy a second, identically refused request.
            # Measured in bytes, which is what the provider refused, rather than
            # by digest: a pass that drops nothing still restates the omissions
            # it inherited, so digests differ where the evidence does not.
            refused_bytes = len(prompt_bundle.to_bytes())
            tightened = audit_evidence_input.tighten_audit_input(
                prompt_bundle,
                max_bytes=int(
                    refused_bytes
                    * audit_evidence_constants.AUDIT_INPUT_OVERFLOW_SHRINK_RATIO
                ),
            )
            if len(tightened.to_bytes()) < refused_bytes:
                shrinks_spent += 1
                prompt_bundle = tightened
                continue
        if outcome in RETRYABLE_DELIVERY_OUTCOMES and _delivery_is_retryable(attempt):
            # The agent never saw this prompt, so nothing about it is corrected
            # and no candidate offer is consumed: the next launch repeats it
            # verbatim once the provider has had time to recover.
            deliveries_spent += 1
            if deliveries_spent >= assurance_models_vocab.MAX_AUDIT_DELIVERY_ATTEMPTS:
                break
            delay = AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS[
                min(deliveries_spent - 1, len(AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS) - 1)
            ]
            _record_capacity_outage(
                attempt, role=role, state_dir=state_dir, run_id=run_id, delay=delay
            )
            _sleep_fn(delay)
            continue
        if unavailable_reason == REASON_IDENTITY_AMBIGUOUS and not matched:
            break
        if outcome not in RETRYABLE_ANSWER_OUTCOMES:
            break
        answers_spent += 1
        if answers_spent >= assurance_models_vocab.MAX_AUDIT_ATTEMPTS:
            break
        if matched:
            correction = None
            candidates = matched
            candidates_offered = True
            catalog = audit_agent.widen_agent_catalog(
                catalog, inbox, [candidate.issue_id for candidate in matched]
            )
        else:
            correction = detail
            candidates = ()

    return AgentAuditOutcome(
        audit=audit_agent.render_supervisor_audit(
            run_id=run_id,
            bundle=bundle,
            results=verdicts,
            generated_at=clock.isoformat(),
            reason=unavailable_reason,
        ),
        attempts=tuple(attempts),
        agent_covered=False,
        catalog=catalog,
    )
