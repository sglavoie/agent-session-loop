"""Checks for success claims the evidence does not support, and retried resumes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from . import events, pause_policy, review_continuation
from . import supervisor_audit_evidence_failures as audit_evidence_failures
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_checks_failures import _aborted_turns
from .supervisor_audit_checks_terminal import (
    _is_expected_conditional_skip,
    _superseded_turn_names,
    _verification_covered_the_turn,
)
from .supervisor_audit_models import _CheckContext, _joined, _Verdict

#: A deferred broad-check failure is closed by a later all-clear review
#: (``resolved``), still waiting on a repair loop the run never finished
#: (``pending``), or accepted without either (``unresolved``).
_DeferralResolution = Literal["resolved", "pending", "unresolved"]


def _recorded_review_cycles(context: _CheckContext) -> tuple[Mapping[str, Any], ...]:
    """Return ordered cycle evidence, preferring the durable state projection."""
    projected = audit_evidence_helpers._sequence(
        context.section("review").get("review_cycles")
    )
    if projected:
        return tuple(audit_evidence_helpers._mapping(item) for item in projected)
    return tuple(
        audit_evidence_helpers._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.REVIEW_CYCLE_EVENT
    )


def _cycle_is_all_clear(cycle: Mapping[str, Any]) -> bool:
    """Whether one recorded review cycle closed the loop with nothing left."""
    return review_continuation.cycle_is_all_clear(cycle)


def _run_is_in_flight(context: _CheckContext) -> bool:
    """Whether the run stopped short of an outcome rather than reaching one."""
    status = context.section("status")
    if status.get("paused") is True:
        return True
    names = {record.get("name") for record in context.records}
    if events.RUN_PAUSED_EVENT in names:
        return True
    return events.RUN_FINISHED_EVENT not in names and "ok" not in status


def _deferred_review_resolution(
    context: _CheckContext, session_id: Any
) -> _DeferralResolution:
    """How a deferred findings review ended: cleared, still open, or unearned.

    Only a later all-clear review closes a deferral. But a run that paused
    before its repair loop converged never got the chance to record one, and
    grading that as a false deferral claim reads an unfinished run as a
    dishonest one. Such a run is ``pending``: evidence the audit has not seen
    yet rather than evidence that contradicts the run.
    """
    cycles = _recorded_review_cycles(context)
    if review_continuation.deferred_review_is_cleared(
        cycles, review_session_id=session_id
    ):
        return "resolved"
    # Deliberately not gated on a repairs-materialized source: a run can pause
    # while its only cycle is malformed, and that is still an unfinished repair
    # loop rather than an unearned acceptance.
    if _run_is_in_flight(context):
        return "pending"
    return "unresolved"


def _accepted_verification_is_accounted_for(
    context: _CheckContext,
    verification_payload: Mapping[str, Any],
    *,
    session_id: Any,
) -> _DeferralResolution:
    """Validate deferral identity, raw failures, and its later all-clear.

    Identity and name validation stay a hard ``unresolved``: a malformed
    acceptance is a defect whether or not the run ever finished.
    """
    acceptance = audit_evidence_failures._deferred_review_acceptance(
        verification_payload
    )
    if acceptance is None:
        return "unresolved"
    deferred = verification_payload.get("deferred_failures")
    if deferred is not None:
        failures = [
            audit_evidence_helpers._mapping(item)
            for item in audit_evidence_helpers._sequence(deferred)
        ]
        names = [failure.get("name") for failure in failures]
        if names != list(
            audit_evidence_helpers._sequence(acceptance.get("deferred_failure_names"))
        ):
            return "unresolved"
        if any(
            not isinstance(name, str)
            or not name.startswith(audit_evidence_failures._BROAD_REVIEW_CHECK_PREFIX)
            for name in names
        ):
            return "unresolved"
    return _deferred_review_resolution(context, session_id)


def _review_cycles_ended_all_clear(context: _CheckContext) -> bool:
    """Whether the last review cycle the run recorded closed every finding."""
    cycles = _recorded_review_cycles(context)
    return bool(cycles) and _cycle_is_all_clear(cycles[-1])


def _check_misleading_success(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    finished = [
        record
        for record in context.records
        if record.get("name") == events.RUN_FINISHED_EVENT
    ]
    claimed = status.get("ok") is True or any(
        audit_evidence_helpers._mapping(record.get("payload")).get("ok") is True
        for record in finished
    )

    counter: list[str] = []
    references: list[str] = []
    superseded = _superseded_turn_names(context.turns)
    # A successful operator stop claims that the stop was honored. It does not
    # claim the interrupted work completed (ASI-23). Require both abort records
    # and scope this exception to the exact turn they identify.
    stopped_turns = (
        _aborted_turns(context) if status.get("reason") == "stopped" else set()
    )
    for turn in context.turns:
        result = audit_evidence_helpers._mapping(turn.get("result"))
        guard = audit_evidence_helpers._mapping(result.get("workspace_write_guard"))
        # A retry replaces the attempt, never the breach: a turn that wrote
        # outside its declared scope stays counter-evidence however cleanly the
        # session finished afterwards.
        stopped = (
            (result.get("session"), result.get("turn_index")) in stopped_turns
            and result.get("status_result") in {"nonzero-exit", "runner-pause"}
            and turn.get("rejected") is not True
            and not result.get("plan_error")
            and not audit_evidence_failures._guard_shows_failure(guard)
            and audit_evidence_helpers._mapping(result.get("verification")).get(
                "passed"
            )
            is not False
        )
        anomalous = (
            turn.get("anomalous") is True
            and turn.get("name") not in superseded
            and not stopped
        )
        contradicts = anomalous or bool(
            audit_evidence_helpers._sequence(guard.get("violations"))
        )
        if anomalous:
            counter.append(f"{turn.get('name')} recorded an anomalous result")
        if audit_evidence_helpers._sequence(guard.get("violations")):
            counter.append(f"{turn.get('name')} exceeded its declared write scope")
        if contradicts and isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
    for session in audit_evidence_helpers._sequence(
        context.section("verification").get("sessions")
    ):
        session_verification = audit_evidence_helpers._mapping(
            audit_evidence_helpers._mapping(session).get("verification")
        )
        # The whole entry, not just its failure: a coverage gap in a superseded
        # turn's recipe reappears on the turn that replaced it.
        if audit_evidence_helpers._mapping(session).get("turn") in superseded:
            continue
        if (
            session_verification.get("passed") is False
            # A deferral whose repair loop the run never finished is unfinished
            # evidence, not a contradiction of the success the run claims.
            and _accepted_verification_is_accounted_for(
                context,
                session_verification,
                session_id=audit_evidence_helpers._mapping(session).get("session_id")
                or session_verification.get("session"),
            )
            == "unresolved"
        ):
            counter.append(
                "verification failed for "
                f"{audit_evidence_helpers._mapping(session).get('turn')}"
            )
        skipped = audit_evidence_helpers._sequence(
            session_verification.get("skipped_checks")
        )
        unexplained = [
            entry for entry in skipped if not _is_expected_conditional_skip(entry)
        ]
        if unexplained:
            counter.append(
                f"{len(unexplained)} verification check(s) were skipped for "
                f"{audit_evidence_helpers._mapping(session).get('turn')} "
                "without a condition that "
                "explains the skip"
            )
        elif skipped and not _verification_covered_the_turn(session_verification):
            counter.append(
                "every verification check for "
                f"{audit_evidence_helpers._mapping(session).get('turn')} was "
                "skipped, so nothing gated the changes it made"
            )
    skipped_records = [
        audit_evidence_helpers._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.VERIFICATION_CHECK_SKIPPED_EVENT
    ]
    unexplained_records = [
        payload
        for payload in skipped_records
        if not _is_expected_conditional_skip(payload)
    ]
    if unexplained_records:
        counter.append(
            f"{len(unexplained_records)} verification check(s) recorded themselves "
            "as skipped without a condition that explains the skip"
        )
    elif skipped_records and not context.events_shed:
        # Absence of a ``verification_check_finished`` record is only evidence
        # in a log the cap kept whole. Shedding is not neutral here: a skipped
        # record outranks a finished one, so a shed bundle keeps exactly the
        # half of the pair that reads as a coverage gap and drops the half that
        # would refute it (ASI-23).
        executed_turns = {
            audit_evidence_helpers._mapping(record.get("payload")).get("turn_index")
            for record in context.records
            if record.get("name") == events.VERIFICATION_CHECK_FINISHED_EVENT
        }
        uncovered = sorted(
            {
                str(payload.get("turn_index"))
                for payload in skipped_records
                if payload.get("turn_index") not in executed_turns
            }
        )
        if uncovered:
            counter.append(
                f"every verification check recorded for turn(s) "
                f"{_joined(uncovered)} was skipped, so nothing gated the changes "
                "they made"
            )
    if status.get("paused") is True:
        counter.append("the state projection records the run as paused")
    if any(record.get("name") == events.RUN_PAUSED_EVENT for record in context.records):
        counter.append("the event log records the run as paused")
    verdict = audit_evidence_helpers._mapping(
        context.section("review").get("review_verdict")
    )
    # The verdict is rewritten per cycle, so this is what the last review
    # *found*, not what it left open. Only the cycle log says whether a later
    # all-clear closed them.
    found = verdict.get("total_findings")
    if (
        isinstance(found, int)
        and found > 0
        and not _review_cycles_ended_all_clear(context)
    ):
        counter.append(
            f"the recorded review verdict reports {found} finding(s) and no later "
            "all-clear review closed them"
        )

    evidence = context.evidence(context.section("state").get("reference"), *references)
    if claimed and counter:
        return _Verdict(
            "fail",
            f"the run reports success its own evidence contradicts: {_joined(counter)}",
            evidence,
        )
    if claimed:
        return _Verdict(
            "pass",
            "nothing the run recorded contradicts the success it claims",
            evidence,
        )
    if "ok" not in status and not finished:
        return _Verdict(
            "unavailable",
            "the run recorded no outcome to weigh against its own evidence",
            evidence,
        )
    return _Verdict(
        "pass", "the run claimed no success, so no success is unearned", evidence
    )


def _check_retry_resume(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.section("state").get("reference"),
        context.event_summary.get("reference"),
    )
    reason = status.get("pause_reason")
    paused = status.get("paused") is True or any(
        record.get("name") == events.RUN_PAUSED_EVENT for record in context.records
    )
    recorded = audit_evidence_helpers._mapping(status.get("pause_policy"))
    if paused and reason and not recorded:
        return _Verdict(
            "unavailable",
            f"the run paused for {reason} but recorded no retry classification to "
            "check against the pause policy",
            evidence,
        )

    contradictions: list[str] = []
    # A block written under an earlier policy version classified the same keys
    # by different rules, so diffing it against today's classifier reports a
    # schema change as a contradiction.
    if recorded and recorded.get("policy_version") == pause_policy.POLICY_VERSION:
        expected = pause_policy.classify_pause(
            reason if isinstance(reason, str) else None,
            audit_evidence_helpers._mapping(status.get("pause_details")),
        )
        for key in ("reason", "recoverable", "requires_user", "max_retries"):
            if recorded.get(key) != expected[key]:
                contradictions.append(
                    f"the recorded retry policy gives {key}={recorded.get(key)!r} "
                    f"where the pause classifier gives {expected[key]!r}"
                )
    # The runner gives every review cycle a fresh retry budget
    # (``review_transitions.read_prior_review_verdict_retries`` forgets the
    # attempts before the latest ``review_cycle`` record), so attempts are
    # weighed against the budget of the cycle that spent them.
    retries: dict[str, dict[int, list[dict[str, Any]]]] = {}
    cycle = 0
    for record in context.records:
        name = record.get("name")
        if name == events.REVIEW_CYCLE_EVENT:
            cycle += 1
            continue
        if name != events.REVIEW_VERDICT_RETRY_EVENT:
            continue
        payload = audit_evidence_helpers._mapping(record.get("payload"))
        retries.setdefault(str(payload.get("session")), {}).setdefault(
            cycle, []
        ).append(payload)
    spent = [
        (session, attempts)
        for session, cycles in sorted(retries.items())
        for _, attempts in sorted(cycles.items())
    ]
    for session, attempts in spent:
        budgets = [
            payload["max_attempts"]
            for payload in attempts
            if isinstance(payload.get("max_attempts"), int)
        ]
        if not budgets:
            contradictions.append(
                f"session {session} recorded {len(attempts)} retry attempt(s) "
                "without naming the budget they were spent against"
            )
            continue
        budget = min(budgets)
        if len(attempts) > budget:
            contradictions.append(
                f"session {session} was relaunched {len(attempts)} times against a "
                f"budget of {budget}"
            )
        beyond = [
            payload["attempt"]
            for payload in attempts
            if isinstance(payload.get("attempt"), int) and payload["attempt"] > budget
        ]
        if beyond:
            contradictions.append(
                f"session {session} recorded retry attempt {max(beyond)} beyond its "
                f"budget of {budget}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the run's retry and resume records are inconsistent: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not recorded and not retries:
        if not paused:
            return _Verdict(
                "pass",
                "the run neither paused nor retried, so no retry budget was spent",
                evidence,
            )
        return _Verdict(
            "unavailable",
            "the run paused without recording either a reason or a retry "
            "classification",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the recorded retry classification matches the pause policy and "
        f"{len(retries)} session(s) stayed inside their retry budget",
        evidence,
    )
