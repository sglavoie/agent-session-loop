"""Checks over resource anomalies, worker pause exits and unclassified anomalies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import events
from . import supervisor_audit_evidence as audit_evidence
from .supervisor_audit_checks_failures import (
    _aborted_turns,
    _check_conversation_restore_refusal,
    _classified_failure_sessions,
    _restore_refusal_records,
)
from .supervisor_audit_models import _CheckContext, _joined, _Verdict
from .supervisor_audit_vocabulary import (
    _CLASSIFIED_EVENT_NAMES,
    _PAUSED_TURN_EVENT_NAMES,
)
from .supervisor_audit_write_scope import _write_scope_session


def _check_resource_anomalies(context: _CheckContext) -> _Verdict:
    anomalies: list[str] = []
    references: list[str] = []
    suspected = 0
    cleared = 0
    automatically_recovered = 0
    for record in context.records:
        name = record.get("name")
        payload = audit_evidence._mapping(record.get("payload"))
        if name == events.TURN_HANG_SUSPECTED_EVENT:
            suspected += 1
        elif name == events.TURN_HANG_CLEARED_EVENT:
            cleared += 1
            block = audit_evidence._mapping(payload.get("hang_suspected"))
            recovery = audit_evidence._mapping(block.get("recovery"))
            if audit_evidence._sequence(recovery.get("signaled_pids")):
                automatically_recovered += 1
        elif name == events.PROCESS_SWEEP_EVENT:
            if audit_evidence._sequence(payload.get("kill_failed")):
                anomalies.append(
                    "a post-turn process sweep failed to kill a process it owned"
                )
            if audit_evidence._sequence(payload.get("survivors_after")):
                anomalies.append("processes survived a post-turn sweep")
    if suspected > cleared:
        anomalies.append(f"{suspected - cleared} suspected hang(s) were never cleared")
    for turn in context.turns:
        result = audit_evidence._mapping(turn.get("result"))
        stalled = [
            label
            for key, label in (
                ("timed_out", "timed out"),
                ("interrupted", "was interrupted"),
            )
            if result.get(key) is True
        ]
        for label in stalled:
            anomalies.append(f"{turn.get('name')} {label}")
        if stalled and isinstance(turn.get("directory"), str):
            references.append(turn["directory"])

    evidence = context.evidence(
        *references,
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if anomalies:
        return _Verdict(
            "fail",
            f"the run stalled or leaked processes: {_joined(anomalies)}",
            evidence,
        )
    if context.events_shed:
        return _Verdict(
            "unavailable",
            "the event log was shed under the size cap, so the run's process "
            "sweeps cannot be read in full",
            evidence,
        )
    if not context.records and not context.turns:
        return _Verdict(
            "unavailable",
            "the run recorded neither events nor turns to judge stalls and timeouts by",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(context.turns)} turn(s) ran without a timeout, an interrupt, an "
        "uncleared hang, or a surviving process; "
        f"{automatically_recovered} post-result hang(s) recovered automatically",
        evidence,
    )


def _worker_pause_exit_verdict(
    context: _CheckContext, payload: Mapping[str, Any], result: Mapping[str, Any]
) -> _Verdict:
    """Account for one failed exit carrying a worker's own pause declaration.

    This grades its recorded disposition, not why the harness needed to be
    stopped. Resource checks still own shutdown, stalls, and process sweeps.
    """
    identity = (
        str(payload.get("session_id", payload.get("session"))),
        payload.get("turn_index"),
    )
    evidence = context.evidence(context.event_summary.get("reference"))
    problems = []
    if _write_scope_session(payload.get("session_id", payload.get("session"))) is None:
        problems.append("the failed exit names no session")
    if (
        not isinstance(identity[1], int)
        or isinstance(identity[1], bool)
        or identity[1] < 1
        or identity
        != (
            str(result.get("session_id", result.get("session"))),
            result.get("turn_index"),
        )
        or payload.get("returncode") != result.get("returncode")
    ):
        problems.append("the failed exit and its turn result disagree")
    if payload.get("timed_out") or payload.get("interrupted") or payload.get("aborted"):
        problems.append("the exit records a timeout, interruption, or abort")
    declaration = result.get("runner_pause")
    if not isinstance(declaration, str) or not declaration.strip():
        problems.append("the worker declared no pause reason")
    if result.get("runner_pause_source") not in {"last_message", "stdout"}:
        problems.append("the pause declaration came from no trusted stream")
    status = context.section("status")
    details = audit_evidence._mapping(status.get("pause_details"))
    session = str(details.get("session_id", details.get("session")))
    if (
        status.get("paused") is not True
        or status.get("ok") is True
        or session != identity[0]
    ):
        problems.append("the run is not holding that failed session under a pause")
    if result.get("status_result") == "complete":
        problems.append("the failed turn also claims accepted completion")

    guard = audit_evidence._mapping(result.get("workspace_write_guard"))
    guard_refused = (
        status.get("pause_reason")
        in {
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
        }
        and guard.get("reason") == status.get("pause_reason")
        and guard.get("passed") is False
        and guard.get("reverted_to_pending") is True
    )
    if not guard_refused:
        if (
            status.get("pause_message") != declaration
            or details.get("source") != result.get("runner_pause_source")
            or result.get("status_result")
            not in {status.get("pause_reason"), "runner-pause"}
        ):
            problems.append("the run never acknowledged that worker pause")
        if result.get("pending_completed_ids") or result.get("pending_completed"):
            problems.append("the worker pause still carries an unreconciled completion")

    if problems:
        return _Verdict("fail", _joined(problems), evidence)
    return _Verdict(
        "pass",
        "the failed exit matches a trusted worker pause and its held or "
        "rejected disposition",
        evidence,
    )


def _worker_pause_exits(
    context: _CheckContext,
) -> list[tuple[dict[str, Any], dict[str, Any], _Verdict]]:
    candidates = []
    for record in context.records:
        if record.get("name") != events.TURN_EXECUTION_FINISHED_EVENT:
            continue
        payload = audit_evidence._mapping(record.get("payload"))
        code = payload.get("returncode")
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or code == 0
            or payload.get("aborted") is True
        ):
            continue
        identity = (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        for turn in context.turns:
            result = audit_evidence._mapping(turn.get("result"))
            if (
                identity
                == (
                    str(result.get("session_id", result.get("session"))),
                    result.get("turn_index"),
                )
                and result.get("runner_pause") is not None
            ):
                candidates.append(
                    (
                        payload,
                        result,
                        _worker_pause_exit_verdict(context, payload, result),
                    )
                )
    return candidates


def _check_worker_pause_exits(context: _CheckContext) -> _Verdict:
    candidates = _worker_pause_exits(context)
    problems = [
        verdict.detail for _, _, verdict in candidates if verdict.status != "pass"
    ]
    evidence = context.evidence(
        context.event_summary.get("reference"), *context.turn_directories()
    )
    if problems:
        return _Verdict(
            "fail",
            f"worker pause exits have contradictory dispositions: {_joined(problems)}",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(candidates)} recorded failed exit(s) carrying worker pauses "
        "match their held or rejected dispositions; shutdown causes remain "
        "separately audited",
        evidence,
    )


def _check_unclassified_anomalies(context: _CheckContext) -> _Verdict:
    summary = context.event_summary
    evidence = context.evidence(
        summary.get("reference"), context.section("state").get("reference")
    )
    unread: list[str] = []
    unparsable = summary.get(
        audit_evidence.UNPARSABLE_LINE_COUNT_KEY,
        summary.get(audit_evidence.LEGACY_UNPARSABLE_LINE_COUNT_KEY),
    )
    if isinstance(unparsable, int) and unparsable > 0:
        unread.append(f"{unparsable} event log line(s) would not parse")
    for omission in context.omissions:
        if omission.get("reason") in (
            audit_evidence.REASON_UNREADABLE,
            audit_evidence.REASON_MALFORMED,
        ):
            unread.append(
                f"{omission.get('count')} {omission.get('section')} artifact(s) were "
                f"{omission.get('reason')}"
            )
    unclassified: dict[str, int] = {}
    classified = _classified_failure_sessions(context)
    aborted = _aborted_turns(context)
    worker_pauses = {
        (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        for payload, _, verdict in _worker_pause_exits(context)
        if verdict.status == "pass"
    }
    restored = (
        {
            (
                str(record.get("session_id", record.get("session"))),
                record.get("turn_index"),
            )
            for record in _restore_refusal_records(context)
        }
        if _check_conversation_restore_refusal(context).status == "pass"
        else set()
    )
    for record in context.records:
        name = str(record.get("name") or "")
        if name in _CLASSIFIED_EVENT_NAMES:
            continue
        payload = audit_evidence._mapping(record.get("payload"))
        # `_check_operator_abort` owns every explicitly aborted exit, including
        # missing acknowledgments. Such a defect belongs to that named check.
        if (
            name == events.TURN_EXECUTION_FINISHED_EVENT
            and payload.get("aborted") is True
        ):
            continue
        if (
            name == events.TURN_EXECUTION_FINISHED_EVENT
            and (
                str(payload.get("session_id", payload.get("session"))),
                payload.get("turn_index"),
            )
            in worker_pauses
        ):
            continue
        if (
            name == events.TURN_EXECUTION_FINISHED_EVENT
            and restored
            and isinstance(payload.get("turn_index"), int)
            and (
                str(payload.get("session_id", payload.get("session"))),
                payload.get("turn_index"),
            )
            in restored
        ):
            continue
        # Narrowed to the session the pause names, because a run reports the
        # outcome of every session it ran and only one of them is the failure
        # the pause classified.
        if name in _PAUSED_TURN_EVENT_NAMES and payload.get("session") in classified:
            continue
        if (
            name in _PAUSED_TURN_EVENT_NAMES
            and (
                payload.get("session"),
                payload.get("turn_index"),
            )
            in aborted
        ):
            continue
        if audit_evidence._payload_shows_failure(payload):
            unclassified[name] = unclassified.get(name, 0) + 1
    for name in sorted(unclassified):
        unread.append(
            f"{unclassified[name]} {name} record(s) report a failure no "
            "deterministic check classifies"
        )

    if unread:
        return _Verdict(
            "fail",
            f"the run holds evidence no deterministic check accounts for: "
            f"{_joined(unread)}",
            evidence,
        )
    if not context.records and not context.omissions:
        return _Verdict(
            "unavailable",
            "the run recorded no events and no omissions, so there is nothing to "
            "classify",
            evidence,
        )
    return _Verdict(
        "pass",
        "every recorded failure is accounted for by a named deterministic check",
        evidence,
    )
