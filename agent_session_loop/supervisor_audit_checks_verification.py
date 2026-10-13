"""Checks over review independence and verification outcomes."""

from __future__ import annotations

from typing import Any

from . import events, verification
from . import supervisor_audit_evidence_failures as audit_evidence_failures
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_checks_success import _accepted_verification_is_accounted_for
from .supervisor_audit_models import _CheckContext, _joined, _Verdict


def _check_review_independence(context: _CheckContext) -> _Verdict:
    review_section = context.section("review")
    run_state = context.section("state")
    evidence = context.evidence(run_state.get("reference"))
    if not review_section and not audit_evidence_helpers._sequence(
        context.section("verification").get("sessions")
    ):
        return _Verdict(
            "unavailable",
            "the run recorded neither a review verdict nor a verification result "
            "to judge independence by",
            evidence,
        )

    verdict = review_section.get("review_verdict")
    harness = run_state.get("harness")
    review_harness = review_section.get("review_harness")
    contradictions: list[str] = []
    if verdict and harness and review_harness == harness:
        contradictions.append(f"the {harness} harness reviewed its own work")
    if verdict == "approved" and review_section.get("review_completed") is False:
        contradictions.append("a verdict was approved without the review completing")
    if contradictions:
        return _Verdict(
            "fail",
            f"the run's review did not stand apart from the work it graded: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not verdict:
        return _Verdict(
            "unavailable",
            "the run recorded no review verdict, so review independence cannot be "
            "judged",
            evidence,
        )
    if not review_harness:
        return _Verdict(
            "unavailable",
            "the run recorded a review verdict without naming the harness that "
            "produced it",
            evidence,
        )
    if not harness:
        return _Verdict(
            "unavailable",
            "the run recorded a review verdict without naming the harness that "
            "did the work the review graded",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the {review_harness} review harness is independent of the {harness} "
        "implementation harness",
        evidence,
    )


def _verification_turn_label(key: tuple[Any, Any]) -> str:
    turn_index = key[1]
    return "an unidentified turn" if turn_index is None else f"turn {turn_index}"


def _check_verification_outcomes(context: _CheckContext) -> _Verdict:
    """Reconcile every failing check against the outcome its recipe reported.

    A verification only earns ``passed`` when every failure it saw was a
    conditional diagnostic, which by construction does not gate. So a failing
    check is accounted for in exactly two ways: the recipe reported the failure,
    or the recipe absorbed it as a diagnostic. Anything else is a verification
    that watched a check fail and said nothing. Both the turn result and the
    event records are read, because either can survive without the other.

    Event records carry the role each executed check played, so a failure is
    reconciled against what that check actually was. Records collected before
    the role was emitted fall back to the count of diagnostics that ran, which
    bounds how many failures a recipe could have absorbed.
    """

    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    executed: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    outcomes: dict[tuple[Any, Any], dict[str, Any]] = {}
    for record in context.records:
        name = record.get("name")
        if name not in (
            events.VERIFICATION_CHECK_FINISHED_EVENT,
            events.VERIFICATION_FINISHED_EVENT,
        ):
            continue
        payload = audit_evidence_helpers._mapping(record.get("payload"))
        key = (payload.get("run_id"), payload.get("turn_index"))
        if name == events.VERIFICATION_FINISHED_EVENT:
            outcomes[key] = payload
        else:
            executed.setdefault(key, []).append(payload)

    sessions = [
        audit_evidence_helpers._mapping(session)
        for session in audit_evidence_helpers._sequence(
            context.section("verification").get("sessions")
        )
        if audit_evidence_helpers._mapping(
            audit_evidence_helpers._mapping(session).get("verification")
        )
    ]
    if not executed and not outcomes and not sessions:
        return _Verdict(
            "unavailable",
            "the run recorded no verification outcomes to reconcile",
            evidence,
        )

    problems: list[str] = []
    blind: list[str] = []
    failing_total = 0
    for session in sessions:
        outcome = audit_evidence_helpers._mapping(session.get("verification"))
        if outcome.get("passed") is False:
            acceptance = outcome.get("acceptance")
            if acceptance is not None:
                deferred = [
                    audit_evidence_helpers._mapping(entry)
                    for entry in audit_evidence_helpers._sequence(
                        outcome.get("deferred_failures")
                    )
                ]
                failing_total += len(deferred)
                resolution = _accepted_verification_is_accounted_for(
                    context,
                    outcome,
                    session_id=session.get("session_id") or outcome.get("session"),
                )
                if resolution == "unresolved":
                    problems.append(
                        f"the failed verification recorded for {session.get('turn')} "
                        "claims broad-check deferral without a matching repair cycle "
                        "and later all-clear review"
                    )
                elif resolution == "pending":
                    blind.append(
                        f"the broad-check failures deferred by "
                        f"{session.get('turn')} reached no all-clear review "
                        "before the run stopped"
                    )
            continue
        if outcome.get("passed") is not True:
            continue
        failed = [
            entry
            for entry in audit_evidence_helpers._sequence(outcome.get("checks"))
            if audit_evidence_failures._payload_shows_failure(
                audit_evidence_helpers._mapping(entry)
            )
        ]
        failing_total += len(failed)
        absorbed = len(
            audit_evidence_helpers._sequence(outcome.get("diagnostic_failures"))
        )
        if len(failed) > absorbed:
            problems.append(
                f"the verification recorded for {session.get('turn')} passed while "
                f"{len(failed)} of its check(s) failed and only {absorbed} of those "
                "was a diagnostic"
            )
    for key in sorted(
        set(executed) | set(outcomes), key=lambda item: (str(item[0]), str(item[1]))
    ):
        label = _verification_turn_label(key)
        failures = [
            payload
            for payload in executed.get(key, ())
            if audit_evidence_failures._payload_shows_failure(payload)
        ]
        failing_total += len(failures)
        outcome = outcomes.get(key)
        if outcome is None:
            if failures:
                target = (
                    blind
                    if context.events_shed or context.terminal_pending
                    else problems
                )
                target.append(
                    f"{len(failures)} failing check record(s) for {label} never "
                    f"reached a {events.VERIFICATION_FINISHED_EVENT} record"
                )
            continue
        if outcome.get("passed") is False:
            acceptance = outcome.get("acceptance")
            if acceptance is not None:
                failing_total += len(failures)
                accepted_names = list(
                    audit_evidence_helpers._sequence(
                        audit_evidence_helpers._mapping(acceptance).get(
                            "deferred_failure_names"
                        )
                    )
                )
                failure_names = [failure.get("name") for failure in failures]
                resolution = _accepted_verification_is_accounted_for(
                    context,
                    outcome,
                    session_id=outcome.get("session_id") or outcome.get("session"),
                )
                # A deferral naming checks that never failed is a defect no
                # amount of unfinished repair loop excuses.
                if accepted_names != failure_names:
                    resolution = "unresolved"
                if resolution == "unresolved":
                    problems.append(
                        f"verification for {label} claims broad-check deferral "
                        "without matching failed checks, a repair cycle, and a "
                        "later all-clear review"
                    )
                elif resolution == "pending":
                    blind.append(
                        f"the broad-check failures deferred by {label} reached no "
                        "all-clear review before the run stopped"
                    )
            continue
        if outcome.get("passed") is not True:
            continue
        diagnostics = outcome.get("diagnostic_count")
        diagnostics = diagnostics if isinstance(diagnostics, int) else 0
        skipped = outcome.get("skipped_count")
        skipped = skipped if isinstance(skipped, int) else 0
        # Only a conditional diagnostic is ever skipped, and only a diagnostic
        # that actually ran can absorb a failure — the declared count on its own
        # lets a skipped diagnostic stand in for a gate that failed.
        absorbable = max(diagnostics - skipped, 0)
        roles = [payload.get("role") for payload in failures]
        gate_failures = sum(1 for role in roles if role == verification.CHECK_ROLE_GATE)
        diagnostic_failures = sum(
            1 for role in roles if role == verification.CHECK_ROLE_DIAGNOSTIC
        )
        unidentified = len(roles) - gate_failures - diagnostic_failures
        if gate_failures:
            problems.append(
                f"verification for {label} passed while {gate_failures} of its "
                "check record(s) reported a failure against a gate"
            )
        elif unidentified > max(absorbable - diagnostic_failures, 0):
            problems.append(
                f"verification for {label} passed while {unidentified} check "
                f"record(s) of unidentified role reported a failure against "
                f"{absorbable} executed diagnostic(s)"
            )
        executed_count = outcome.get("executed_count")
        passed_count = outcome.get("passed_count")
        if isinstance(executed_count, int) and isinstance(passed_count, int):
            unpassed = executed_count - passed_count
            if unpassed > absorbable:
                problems.append(
                    f"verification for {label} passed while its own counts leave "
                    f"{unpassed} of {executed_count} check(s) unpassed against "
                    f"{absorbable} executed diagnostic(s)"
                )

    if problems:
        return _Verdict(
            "fail",
            f"the run's verification outcomes do not account for the checks that "
            f"failed under them: {_joined(problems)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the run's verification records are incomplete, so {_joined(blind)} "
            "cannot be distinguished from evidence the audit never saw",
            evidence,
        )
    return _Verdict(
        "pass",
        f"every one of the {failing_total} failing verification check(s) is "
        "accounted for by the outcome of the recipe that ran it",
        evidence,
    )
