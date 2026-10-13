"""Checks over harness failures, conversation-restore refusals and operator aborts."""

from __future__ import annotations

from typing import Any

from . import events
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_models import _CheckContext, _joined, _Verdict


def _harness_failure_classification(context: _CheckContext) -> dict[str, Any]:
    """The classification the run attached to the harness failure it paused on.

    Empty when the run did not pause on a harness failure, which is every pause
    this invariant has nothing to say about.
    """
    status = context.section("status")
    if status.get("paused") is not True:
        return {}
    details = audit_evidence_helpers._mapping(status.get("pause_details"))
    return audit_evidence_helpers._mapping(details.get("failure_classification"))


def _classified_failure_sessions(context: _CheckContext) -> set[Any]:
    """Sessions whose turn failure the run named a reason for when it paused.

    The pause carries the classification for the turn it died on, and that
    classification is what the harness-failure check grades. Counting the same
    turn again as evidence nothing looked at reports a named failure as an
    unknown one, whether or not anything will relaunch it.
    """
    if not _harness_failure_classification(context).get("reason"):
        return set()
    session = audit_evidence_helpers._mapping(
        context.section("status").get("pause_details")
    ).get("session")
    return set() if session is None else {session}


def _restore_refusal_records(context: _CheckContext) -> list[dict[str, Any]]:
    return [
        payload
        for record in context.records
        if record.get("name") == events.CONVERSATION_CONTINUATION_EVENT
        and (payload := audit_evidence_helpers._mapping(record.get("payload"))).get(
            "disposition"
        )
        == "restore-refused"
    ]


def _check_conversation_restore_refusal(context: _CheckContext) -> _Verdict:
    """Grade the refusal that bypasses the ordinary harness-failure classifier."""
    status = context.section("status")
    details = audit_evidence_helpers._mapping(status.get("pause_details"))
    refusal = audit_evidence_helpers._mapping(
        details.get("conversation_restore_refused")
    )
    records = _restore_refusal_records(context)
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if not records:
        return _Verdict(
            "unavailable"
            if refusal and context.events_shed
            else "fail"
            if refusal
            else "pass",
            "the restore refusal has no conversation event to account for it"
            if refusal
            else "the run recorded no refused conversation restore",
            evidence,
        )
    problems: list[str] = []
    missing_exit = False
    if (
        status.get("paused") is not True
        or status.get("pause_reason")
        != events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
        or status.get("ok") is True
    ):
        problems.append("the refused restore did not leave the run paused for recovery")
    for record in records:
        session = record.get("session_id", record.get("session"))
        turn_index = record.get("turn_index")
        if (
            not isinstance(session, (int, str))
            or isinstance(session, bool)
            or not str(session)
            or not isinstance(turn_index, int)
            or isinstance(turn_index, bool)
            or turn_index < 1
        ):
            problems.append("a restore refusal names no session or turn")
        if (
            record.get("retired") is not True
            or record.get("error")
            or not isinstance(record.get("conversation"), str)
            or not record.get("conversation")
            or not isinstance(record.get("detail"), str)
            or not record.get("detail")
        ):
            problems.append("a refused conversation was not explained and retired")
        if str(details.get("session_id", details.get("session"))) != str(
            session
        ) or any(
            refusal.get(key) != record.get(key)
            for key in ("conversation", "disposition", "retired", "detail", "error")
        ):
            problems.append("the recovery pause does not match the recorded refusal")
        exits = [
            payload
            for event in context.records
            if event.get("name") == events.TURN_EXECUTION_FINISHED_EVENT
            and str(
                (payload := audit_evidence_helpers._mapping(event.get("payload"))).get(
                    "session_id", payload.get("session")
                )
            )
            == str(session)
            and payload.get("turn_index") == turn_index
        ]
        if not exits:
            missing_exit = True
            if not context.events_shed:
                problems.append("the refused restore has no matching turn exit")
        for exit_record in exits:
            code = exit_record.get("returncode")
            if (
                not isinstance(code, int)
                or isinstance(code, bool)
                or code == 0
                or code != details.get("returncode")
                or exit_record.get("timed_out")
                or exit_record.get("interrupted")
                or exit_record.get("aborted")
                or exit_record.get("plan_readable") is False
            ):
                problems.append("the refused turn's exit has an unexplained outcome")
    if missing_exit and not problems:
        return _Verdict(
            "unavailable",
            "the refused turn's exit was shed from the evidence",
            evidence,
        )
    return _Verdict(
        "fail" if problems else "pass",
        _joined(problems)
        if problems
        else (
            f"{len(records)} refused restore(s) retired the unavailable conversation "
            "and paused for explicit recovery"
        ),
        evidence,
    )


def _abort_turn_records(context: _CheckContext, action: str) -> list[dict[str, Any]]:
    """The run-control payloads carrying one turn-abort action, in order."""
    payloads: list[dict[str, Any]] = []
    for record in context.records:
        if record.get("name") != events.RUN_CONTROL_EVENT:
            continue
        payload = audit_evidence_helpers._mapping(record.get("payload"))
        if payload.get("action") == action:
            payloads.append(payload)
    return payloads


def _aborted_turns(context: _CheckContext) -> set[tuple[Any, Any]]:
    """The turns the run killed because an operator asked it to.

    Keyed by session and turn index together, so the exemption covers the one
    turn the abort named rather than every turn that session ever ran.
    """
    requested = {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED
        )
        if payload.get("session") is not None and payload.get("turn_index") is not None
    }
    return {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED
        )
        if payload.get("abort_reason")
        and (payload.get("session"), payload.get("turn_index")) in requested
    }


def _check_operator_abort(context: _CheckContext) -> _Verdict:
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    honored = _abort_turn_records(context, events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED)
    aborted_exits = [
        audit_evidence_helpers._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.TURN_EXECUTION_FINISHED_EVENT
        and audit_evidence_helpers._mapping(record.get("payload")).get("aborted")
        is True
    ]
    if not honored and not aborted_exits:
        return _Verdict(
            "pass",
            "the run killed no turn on an operator abort",
            evidence,
        )
    requested = {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED
        )
    }
    contradictions: list[str] = []
    acknowledged = {
        (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        for payload in honored
    }
    missing = [
        payload
        for payload in aborted_exits
        if (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        not in acknowledged
    ]
    for payload in missing if not context.events_shed else ():
        contradictions.append(
            f"session {payload.get('session_id', payload.get('session'))!r} "
            f"turn {payload.get('turn_index')!r} records an aborted exit "
            "without an abort acknowledgment"
        )
    for payload in honored:
        turn = (payload.get("session"), payload.get("turn_index"))
        if turn not in requested:
            contradictions.append(
                f"session {turn[0]!r} turn {turn[1]!r} was killed on an abort no "
                "recorded request asked for"
            )
        elif not payload.get("abort_reason"):
            contradictions.append(
                f"session {turn[0]!r} turn {turn[1]!r} was killed on an abort "
                "that names no reason"
            )
    if contradictions:
        return _Verdict(
            "fail",
            f"the run killed a turn without an accountable abort behind it: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if missing and context.events_shed:
        return _Verdict(
            "unavailable",
            "aborted turn exits lack acknowledgments in an event log shed "
            "under the size cap",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(honored)} turn(s) were killed on an operator abort the run "
        "recorded a request and a reason for",
        evidence,
    )


def _check_retryable_transport_failure(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    classification = _harness_failure_classification(context)
    if not classification:
        return _Verdict(
            "pass",
            "the run did not pause on a harness failure, so no retry was owed "
            "to any classification",
            evidence,
        )

    reason = classification.get("reason")
    retryable = classification.get("retryable")
    policy = audit_evidence_helpers._mapping(status.get("pause_policy"))
    if not policy:
        return _Verdict(
            "unavailable",
            f"the run classified its harness failure as {reason!r} but recorded "
            "no pause policy to judge that classification against",
            evidence,
        )

    contradictions: list[str] = []
    expected_retryable = reason in events.RETRYABLE_HARNESS_FAILURE_REASONS
    if isinstance(retryable, bool) and retryable is not expected_retryable:
        contradictions.append(
            f"the failure is classified retryable={retryable!r} while its reason "
            f"{reason!r} is {'' if expected_retryable else 'not '}one the "
            "classifier marks retryable"
        )
    if retryable is True:
        # A retryable failure is one the supervisor may relaunch unattended.
        # Either half of that going missing strands the run: a policy that
        # refuses the relaunch, or one that sends it to a human who has no
        # answer to give.
        if policy.get("recoverable") is not True:
            contradictions.append(
                "the failure is classified retryable while its pause policy "
                f"gives recoverable={policy.get('recoverable')!r}, so nothing "
                "will relaunch it"
            )
        if policy.get("requires_user") is not False:
            contradictions.append(
                "the failure is classified retryable while its pause policy "
                f"gives requires_user={policy.get('requires_user')!r}, so it "
                "waits on a person no reply can help"
            )
        budget = policy.get("max_retries")
        if not isinstance(budget, int) or budget <= 0:
            contradictions.append(
                f"the failure is classified retryable while its pause policy "
                f"grants max_retries={budget!r}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the run's harness-failure classification contradicts the policy it "
            f"paused under: {_joined(contradictions)}",
            evidence,
        )
    if retryable is True:
        fingerprint = classification.get("fingerprint") or reason
        return _Verdict(
            "pass",
            f"the turn failed with the retryable {fingerprint} "
            f"classification and the run paused under a policy granting "
            f"{policy.get('max_retries')} supervisor relaunch(es), which is where "
            "the retry is owed rather than inside this run",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the turn failed with the non-retryable {reason!r} classification and "
        "the run paused rather than relaunching it",
        evidence,
    )
