"""Checks that paused work was settled before the run ended."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import commit_groups, events
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_checks_commits import _unresolved_commit_group_rows
from .supervisor_audit_models import _CheckContext, _joined, _Verdict
from .supervisor_audit_vocabulary import (
    _PAUSED_WORK_RESULT_COMMITTED,
    _PAUSED_WORK_RESULT_FAILED,
    _PAUSED_WORK_RESULT_RESIDUE,
    _PAUSED_WORK_RESULTS,
    _PAUSED_WORK_SETTLEMENT_COMMIT_EVENTS,
)

#: Records that carry a commit group's full description, any one of which
#: identifies the group when the size cap shed the others.
_COMMIT_GROUP_LIFECYCLE_EVENTS = frozenset(
    {
        events.COMMIT_GROUP_STARTED_EVENT,
        events.COMMIT_GROUP_UPDATED_EVENT,
        events.COMMIT_GROUP_FINISHED_EVENT,
    }
)


def _session_paused_group_id(group: Any) -> str | None:
    """The id of a ``session-paused`` commit group, or ``None`` for any other."""
    group = audit_evidence_helpers._mapping(group)
    scope = group.get("scope") or audit_evidence_helpers._mapping(
        group.get("identity")
    ).get("scope")
    if scope != commit_groups.GROUP_SCOPE_SESSION_PAUSED:
        return None
    group_id = group.get("group_id")
    return group_id if isinstance(group_id, str) and group_id else None


def _paused_work_settlement_group_ids(
    tail: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """Commit-group ids the paused-work settlement opened after the terminal record.

    Settling the work a recoverable pause retained is the one thing a run does
    after it has already declared itself over. ``run_loop`` calls the
    settlement at the point every loop break converges on, deliberately
    downstream of the ``run_paused`` record it settles for -- the revert, the
    plan re-read and the pause must all have happened before it can know what
    it is committing. Its commit records are therefore post-terminal by
    construction, and reading them as a run that kept working would fail every
    run that settles anything.

    Only a block that closed earns the exemption. The settlement writes its
    ``paused_work_committed`` record last, so a group with no such record after
    it is a settlement that died mid-commit. That is work after the terminal
    record in exactly the sense this check means, and is left to fail.
    """
    opened: dict[str, int] = {}
    closed_at: list[int] = []
    for index, record in enumerate(tail):
        name = str(record.get("name") or "")
        payload = audit_evidence_helpers._mapping(record.get("payload"))
        if name == events.PAUSED_WORK_COMMITTED_EVENT:
            closed_at.append(index)
            # The close names its own group, so a settlement stays
            # recognisable when the size cap shed the record that opened it.
            group_id = _session_paused_group_id(payload.get("commit_group"))
        elif name in _COMMIT_GROUP_LIFECYCLE_EVENTS:
            group_id = _session_paused_group_id(payload.get("group"))
        else:
            continue
        if group_id is not None:
            opened.setdefault(group_id, index)
    if not opened or not closed_at:
        return frozenset()
    last_closed = closed_at[-1]
    return frozenset(
        group_id for group_id, index in opened.items() if index <= last_closed
    )


def _settles_paused_work(
    record: Mapping[str, Any], settlement_group_ids: frozenset[str]
) -> bool:
    """Whether one post-terminal record is part of a closed settlement block."""
    if not settlement_group_ids:
        return False
    if str(record.get("name") or "") not in _PAUSED_WORK_SETTLEMENT_COMMIT_EVENTS:
        return False
    group_id = audit_evidence_helpers._mapping(record.get("payload")).get("group_id")
    return isinstance(group_id, str) and group_id in settlement_group_ids


def _paused_work_settlement_label(payload: Mapping[str, Any]) -> str:
    session_id = payload.get("session_id")
    return (
        "the paused-work settlement"
        if session_id is None
        else f"session {session_id}'s paused-work settlement"
    )


def _paused_work_settlement_reason(payload: Mapping[str, Any]) -> str | None:
    """Where a failed settlement wrote why it failed, if it wrote it anywhere.

    The two places are the pair ``run_reporting`` already reads when it prints
    the failure to an operator: the exception the settlement caught, and the
    outcome the underlying commit classified itself with.
    """
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error
    status_result = audit_evidence_helpers._mapping(payload.get("commit")).get(
        "status_result"
    )
    if isinstance(status_result, str) and status_result.strip():
        return status_result
    return None


def _check_paused_work_settlement(context: _CheckContext) -> _Verdict:
    """Grade the one commit a run makes that no accepted turn produced.

    A recoverable pause leaves the failed turn's changes in the worktree, and
    the settlement is the run's single chance to commit them before the process
    exits. Nothing downstream re-derives what it did: the next run meets either
    a clean tree or unexplained dirt at its startup commit gate, and only this
    record says which was intended.

    So the record is graded on its own terms rather than trusted. It must name
    an outcome the settlement actually writes; a failure must say where its
    reason was written and must leave the group among the ones the run ended
    owing, which ``run_loop`` guarantees by ordering the settlement ahead of the
    terminal record; and a success must agree with itself about residue, since
    ``committed`` and ``committed-with-residue`` differ by nothing else. The
    record must also be mirrored into state, because a record in the log with no
    mirror is a run that died between the append and the write, and every reader
    but this one reads the mirror.

    Grading it here is what earns ``paused_work_committed`` its place in
    :data:`_CLASSIFIED_EVENT_NAMES`. The catch-all reported every settlement as
    an unclassified failure, because the record carries the pause's reason; the
    exemption is only sound while this check owns the record instead.
    """
    problems: list[str] = []
    settlements = [
        audit_evidence_helpers._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.PAUSED_WORK_COMMITTED_EVENT
    ]
    commits = context.section("commits")
    mirror = audit_evidence_helpers._mapping(
        commits.get(audit_evidence_constants.PAUSED_WORK_COMMIT_KEY)
    )
    unresolved_scopes = {
        row.get("scope") for row in _unresolved_commit_group_rows(context)
    }
    for payload in settlements:
        label = _paused_work_settlement_label(payload)
        result = payload.get("result")
        if not isinstance(result, str) or result not in _PAUSED_WORK_RESULTS:
            problems.append(
                f"{label} recorded {result!r}, which is no settlement outcome ASL "
                "writes"
            )
        elif result == _PAUSED_WORK_RESULT_FAILED:
            if _paused_work_settlement_reason(payload) is None:
                problems.append(f"{label} failed without recording why")
            if commit_groups.GROUP_SCOPE_SESSION_PAUSED not in unresolved_scopes:
                problems.append(
                    f"{label} failed, but the run ended owing no paused-work "
                    "commit group for a later run to recover"
                )
        else:
            residue = audit_evidence_helpers._sequence(
                audit_evidence_helpers._mapping(payload.get("residue")).get(
                    "repositories"
                )
            )
            if result == _PAUSED_WORK_RESULT_RESIDUE and not residue:
                problems.append(
                    f"{label} reported residue while naming no repository it was "
                    "left in"
                )
            elif result == _PAUSED_WORK_RESULT_COMMITTED and residue:
                problems.append(
                    f"{label} reported a clean commit while naming "
                    f"{len(residue)} repository(ies) it left dirty"
                )
        if not mirror:
            problems.append(f"{label} was never mirrored into the run's state")
        elif mirror.get("result") != result:
            problems.append(
                f"{label} recorded {result!r} while state mirrors "
                f"{mirror.get('result')!r}"
            )

    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if problems:
        return _Verdict(
            "fail",
            f"the run's settlement of its paused work does not account for "
            f"itself: {_joined(problems)}",
            evidence,
        )
    if not settlements:
        if context.events_shed:
            return _Verdict(
                "unavailable",
                "the event log was shed under the size cap, so a settlement the "
                "run made cannot be distinguished from one it never made",
                evidence,
            )
        return _Verdict(
            "pass",
            "the run left no paused work behind, so it settled none",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(settlements)} paused-work settlement(s) recorded an outcome ASL "
        "writes, agreed with the run's state, and accounted for what they left "
        "in the worktree",
        evidence,
    )
