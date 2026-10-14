"""Checks over state events and terminal run artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import events, state, verification_models
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from . import supervisor_audit_evidence_turns as audit_evidence_turns
from .supervisor_audit_checks_paused_work import (
    _paused_work_settlement_group_ids,
    _settles_paused_work,
)
from .supervisor_audit_models import _CheckContext, _joined, _names, _Verdict
from .supervisor_audit_vocabulary import _POST_TERMINAL_WORK_EVENTS


def _check_state_event_agreement(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.section("state").get("reference"),
        context.event_summary.get("reference"),
    )
    terminal = [
        record
        for record in context.records
        if record.get("name") in audit_evidence_constants._TERMINAL_EVENT_NAMES
    ]
    if "ok" not in status and "paused" not in status and not terminal:
        return _Verdict(
            "unavailable",
            "the run recorded neither a terminal outcome in its state nor a "
            "terminal event record",
            evidence,
        )
    paused_records = [
        record for record in terminal if record.get("name") == events.RUN_PAUSED_EVENT
    ]
    finished_records = [
        record for record in terminal if record.get("name") == events.RUN_FINISHED_EVENT
    ]

    contradictions: list[str] = []
    blind: list[str] = []

    def absent(claim: str) -> None:
        (blind if context.events_shed else contradictions).append(claim)

    if not context.terminal_pending:
        if status.get("paused") is True and not paused_records:
            absent("state records the run as paused but no run_paused record survives")
        if status.get("ok") is True and not finished_records:
            absent("state records the run as ok but no run_finished record survives")
    if paused_records and status.get("paused") is False:
        contradictions.append(
            "the event log records a pause the state projection denies"
        )
    if paused_records and status.get("ok") is True:
        contradictions.append(
            "the event log records a pause the state projection reports as success"
        )
    if (
        any(
            audit_evidence_helpers._mapping(record.get("payload")).get("ok") is False
            for record in finished_records
        )
        and status.get("ok") is True
    ):
        contradictions.append(
            "the run_finished record reports failure while the state projection "
            "reports success"
        )
    reason = status.get("pause_reason")
    for record in paused_records:
        recorded = audit_evidence_helpers._mapping(record.get("payload")).get("reason")
        if reason and recorded and recorded != reason:
            contradictions.append(
                f"state names {reason} as the pause reason while the run_paused "
                f"record names {recorded}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the persisted state and the event log tell different stories: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the event log was shed under the size cap, so {_joined(blind)}",
            evidence,
        )
    return _Verdict(
        "pass",
        "the persisted status agrees with the run's terminal event records",
        evidence,
    )


def _check_terminal_artifacts(context: _CheckContext) -> _Verdict:
    problems: list[str] = []
    blind: list[str] = []
    references: list[str] = []
    for turn in context.turns:
        result = audit_evidence_helpers._mapping(turn.get("result"))
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        if result.get("available") is False:
            problems.append(
                f"{turn.get('name')} wrote no readable {state.RESULT_FILE_NAME} "
                f"({result.get('reason')})"
            )
    evidence = context.evidence(context.event_summary.get("reference"), *references)
    names = [str(record.get("name") or "") for record in context.records]
    if not names:
        if problems:
            return _Verdict("fail", _joined(problems), evidence)
        return _Verdict(
            "unavailable",
            "the run's slice of the event log holds no records to order",
            evidence,
        )

    if audit_evidence_constants._RUN_STARTED_EVENT not in names:
        (blind if context.events_shed else problems).append(
            "the run recorded no run_started event"
        )
    elif names[0] != audit_evidence_constants._RUN_STARTED_EVENT:
        problems.append(
            f"the run's first record is {names[0]} rather than "
            f"{audit_evidence_constants._RUN_STARTED_EVENT}"
        )
    terminal_at = [
        index
        for index, name in enumerate(names)
        if name in audit_evidence_constants._TERMINAL_EVENT_NAMES
    ]
    if not terminal_at:
        if not context.terminal_pending:
            (blind if context.events_shed else problems).append(
                "the run recorded no terminal event"
            )
    elif len(terminal_at) > 1:
        problems.append(f"the run recorded {len(terminal_at)} terminal events")
    settled = False
    if terminal_at:
        tail = context.records[terminal_at[-1] + 1 :]
        settlement_group_ids = _paused_work_settlement_group_ids(tail)
        settled = bool(settlement_group_ids)
        trailing = [
            str(record.get("name") or "")
            for record in tail
            if str(record.get("name") or "") in _POST_TERMINAL_WORK_EVENTS
            and not _settles_paused_work(record, settlement_group_ids)
        ]
        if trailing:
            problems.append(
                f"{len(trailing)} record(s) of work follow the run's terminal "
                f"record: {_names(trailing)}"
            )

    if problems:
        return _Verdict(
            "fail",
            f"the run's terminal artifacts are missing or out of order: "
            f"{_joined(problems)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the event log was shed under the size cap, so {_joined(blind)} "
            "cannot be distinguished from one the cap removed",
            evidence,
        )
    closing = (
        "has not yet written the terminal record it is being audited ahead of"
        if not terminal_at
        else "closes on exactly one terminal record"
    )
    epilogue = (
        ", followed only by the settlement of the work its pause retained"
        if settled
        else ""
    )
    return _Verdict(
        "pass",
        f"the run opens on {audit_evidence_constants._RUN_STARTED_EVENT}, "
        f"{closing}{epilogue}, and every "
        f"turn wrote a readable {state.RESULT_FILE_NAME}",
        evidence,
    )


def _is_expected_conditional_skip(entry: Any) -> bool:
    """Whether a skip is the recipe working as designed rather than a gap.

    A diagnostic that names the gates it explains and was omitted because none
    of them failed proves the recipe reached a clean result, so counting it
    against the run's success claim would make every healthy recipe look
    contradicted. Every other skip is a check that did not run.
    """

    if not isinstance(entry, Mapping):
        return False
    return bool(
        entry.get("reason") == verification_models.SKIP_REASON_CONDITION_NOT_MET
        and audit_evidence_helpers._sequence(entry.get("on_failure_of"))
    )


def _verification_covered_the_turn(session_verification: Mapping[str, Any]) -> bool:
    """Whether a recipe actually gated the changes the turn made.

    An expected skip is only neutral while something else still checked the
    work. A recipe that declared no gates, or that executed nothing, left the
    turn's mutation uncovered no matter how well-formed its skips were.
    """

    gate_count = audit_evidence_helpers._mapping(
        session_verification.get("recipe")
    ).get("gate_count")
    if isinstance(gate_count, int) and gate_count <= 0:
        return False
    return bool(audit_evidence_helpers._sequence(session_verification.get("checks")))


def _superseded_turn_names(turns: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Turns a later clean completion of the same session replaced.

    The loop retries a session in place, so a turn that ended badly is
    counter-evidence only while it is still the last word on that session. Once
    a later turn completed the same session cleanly, counting the earlier
    attempt against the run's success claim would report every recovered run as
    a false success. A turn whose index or session cannot be read is never
    treated as superseded, so an unreadable projection keeps the finding.
    """
    stamped = {
        turn["name"]
        for turn in turns
        if isinstance(turn.get("name"), str)
        and isinstance(turn.get(audit_evidence_turns.TURN_SUPERSEDED_BY_KEY), str)
    }
    return frozenset(audit_evidence_turns._supersessions(turns)) | stamped
