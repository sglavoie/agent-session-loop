"""Checks over commit guards, commit failures and commit-group resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import commit_groups, events
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_failures as audit_evidence_failures
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_models import _CheckContext, _joined, _names, _Verdict
from .supervisor_audit_vocabulary import (
    _COMMIT_RESULTS,
    MUTATION_ARTIFACT_NAME,
    WORKSPACE_CHANGE_ARTIFACT_NAME,
)


def _check_mutation_commit_guards(context: _CheckContext) -> _Verdict:
    commits = context.section("commits")
    registry = context.section("registry")
    scopes = context.section("scopes")
    contradictions: list[str] = []
    references: list[str] = []

    activity = [
        f"{audit_evidence_constants.EVIDENCE_ROOT_RUN}/{name}"
        for name in (MUTATION_ARTIFACT_NAME, WORKSPACE_CHANGE_ARTIFACT_NAME)
        if f"{audit_evidence_constants.EVIDENCE_ROOT_RUN}/{name}" in context.declared
    ]
    references.extend(activity)

    writable = {
        str(alias)
        for session in audit_evidence_helpers._sequence(scopes.get("sessions"))
        for alias in audit_evidence_helpers._sequence(
            audit_evidence_helpers._mapping(session).get("writes")
        )
    }
    attempts = [
        audit_evidence_helpers._mapping(item)
        for item in audit_evidence_helpers._sequence(commits.get("attempts"))
    ]
    # A commit the guard observed is by definition one the runner never ran, so
    # the commit mode it would have run under is absent for the same reason the
    # commit is: nobody asked the runner to make it.
    ran = [
        attempt
        for attempt in attempts
        if attempt.get("origin") != audit_evidence_constants.OBSERVED_COMMIT_ORIGIN
    ]
    repair_commit_mode = commits.get("repair_commit_mode")
    if ran and not commits.get("commit_mode") and repair_commit_mode != "per-child":
        contradictions.append(
            f"the run made {len(ran)} commit attempt(s) without recording the "
            "commit mode they ran under"
        )
    for attempt in attempts:
        if isinstance(attempt.get("reference"), str):
            references.append(attempt["reference"])
        result = audit_evidence_helpers._mapping(attempt.get("result"))
        if result.get("available") is False:
            contradictions.append(
                f"{attempt.get('name')} wrote no readable commit result"
            )
            continue
        alias = result.get("repository_alias")
        if scopes.get("available") and alias and str(alias) not in writable:
            contradictions.append(
                f"{attempt.get('name')} committed to {alias}, which no session "
                "declares as writable"
            )

    guarded = 0
    head_moves = 0
    history_rewrites = 0
    unguarded: list[str] = []
    for turn in context.turns:
        result = audit_evidence_helpers._mapping(turn.get("result"))
        if result.get("available") is False or turn.get("anomalous") is True:
            continue
        # Registered workspaces guard review turns as well. Legacy runs have a
        # separate read-only review guard, but every implementation turn must
        # carry repository evidence even when commit mode is disabled.
        requires_guard = registry.get("registered") or (
            result.get("session_kind") == "implementation"
        )
        if not requires_guard:
            continue
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        guard = audit_evidence_helpers._mapping(result.get("workspace_write_guard"))
        if not guard:
            unguarded.append(str(turn.get("name")))
            continue
        guarded += 1
        for raw_changes in audit_evidence_helpers._mapping(
            guard.get("changed")
        ).values():
            changes = audit_evidence_helpers._mapping(raw_changes)
            if changes.get("head_changed") is not True:
                continue
            head_moves += 1
            transition = audit_evidence_helpers._mapping(changes.get("head_transition"))
            if not transition:
                contradictions.append(
                    f"{turn.get('name')} changed HEAD without recording its "
                    "history transition"
                )
                continue
            relation = transition.get("relation")
            if relation not in {
                "created",
                "deleted",
                "fast-forward",
                "rewind",
                "rewrite",
            }:
                contradictions.append(
                    f"{turn.get('name')} recorded an invalid HEAD transition"
                )
                continue
            if relation in {"deleted", "rewind", "rewrite"}:
                history_rewrites += 1
    if unguarded:
        contradictions.append(
            f"{_names(unguarded)} completed implementation work without a "
            "write-guard result"
        )

    evidence = context.evidence(*references, context.section("state").get("reference"))
    if contradictions:
        return _Verdict(
            "fail",
            f"the run's mutation and commit guards left a gap: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if activity:
        return _Verdict(
            "unavailable",
            "the audit recorded workspace activity, but snapshot comparisons "
            "identify no writer; this does not establish an auditor breach",
            evidence,
        )
    if not attempts and not guarded:
        return _Verdict(
            "unavailable",
            "the run recorded neither a commit attempt nor a write-guard result to "
            "judge its mutation guards by",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{guarded} guarded turn(s), {len(attempts)} commit attempt(s), "
        f"{head_moves} worker HEAD move(s), and {history_rewrites} history "
        "rewrite(s) stayed inside the run's declared write scope",
        evidence,
    )


def _worker_commits_added(guard: Mapping[str, Any]) -> list[str] | None:
    """Name what a worker turn put on HEAD, or ``None`` if that is unreadable.

    ``None`` is not "nothing". A guard that saw the tip move but recorded no
    count for it knows a commit happened and cannot say how many, and reading
    that as none would turn the one case worth reporting into a clean answer.

    The relation travels with the count because the two say different things.
    A ``fast-forward`` counts commits the worker wrote; a ``rewrite`` counts
    every commit the new tip reaches that the old one did not, which is the
    whole rewritten range rather than the turn's own work. Both outlive a
    refused turn, and a sentence that reported them as the same number would
    overstate one of them.
    """
    added: list[str] = []
    for raw_changes in audit_evidence_helpers._mapping(guard.get("changed")).values():
        changes = audit_evidence_helpers._mapping(raw_changes)
        if changes.get("head_changed") is not True:
            continue
        transition = audit_evidence_helpers._mapping(changes.get("head_transition"))
        count = transition.get("commits_added")
        relation = transition.get("relation")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        if not isinstance(relation, str) or not relation:
            return None
        if count:
            added.append(f"{count} commit(s) by {relation}")
    return added


def _check_commit_after_verification(context: _CheckContext) -> _Verdict:
    """Grade whether a turn the run refused left commits of its own behind.

    ASL commits a session's work after verification accepts it, which is what
    makes the history a record of work that passed. A worker that runs ``git
    commit`` inside its own turn moves that commit ahead of the gate, and
    nothing walks it back: the write guard's before/after comparison is taken
    before the runner would commit anything, so a tip that moved there moved
    under the worker. When the turn is then refused -- the harness exited
    nonzero, the guard reverted it, or authoritative verification failed --
    the session goes back to ``pending`` while the commit stays on the branch,
    and the next attempt rebuilds the same work on top of it (ASI-142).

    The run's own mutation check counts those moves and reports them as having
    stayed inside the declared write scope, which is true and is not this
    question. Scope asks *where* the worker wrote; this asks whether what it
    wrote outlived the turn that was rejected. Both belong to the same lane, so
    the rollup keeps the worse of the two.

    Never ``unavailable``. A turn that recorded no write guard at all recorded
    no HEAD telemetry either, and demanding it here would be a second opinion
    about a gap ``_check_mutation_commit_guards`` already owns -- its
    recommendation is precisely to capture fingerprints around every
    implementation turn. What blindness costs is stated in the clear verdict
    instead, so the report never answers for a place it did not look.
    """
    references: list[str] = []
    retained: list[str] = []
    blind: list[str] = []
    for turn in context.turns:
        result = audit_evidence_helpers._mapping(turn.get("result"))
        if result.get("available") is False:
            continue
        name = str(turn.get("name"))
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        guard = audit_evidence_helpers._mapping(result.get("workspace_write_guard"))
        if not guard:
            blind.append(name)
            continue
        # `anomalous` is the loop's own refusal, correlated from the event
        # stream by `_mark_rejected_turns`; `_turn_shows_failure` is the turn
        # result's. A verification that failed after a clean exit is only in
        # the second, and it is the shape ASI-142 was reported for.
        if not (
            turn.get("anomalous") is True
            or audit_evidence_failures._turn_shows_failure(result)
        ):
            continue
        added = _worker_commits_added(guard)
        if added is None:
            blind.append(name)
            continue
        if added:
            retained.append(f"{name} left {_joined(added)} on HEAD")

    evidence = context.evidence(*references, context.section("state").get("reference"))
    if retained:
        return _Verdict(
            "fail",
            "the run refused a turn and kept the commits it had already made: "
            f"{_joined(retained)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "pass",
            "no turn the run refused kept a commit this check could read off a "
            f"write-guard record, but {_joined(blind)} recorded no readable "
            "HEAD telemetry, so whether a refused turn committed there is "
            "mutation-and-commit's to report",
            evidence,
        )
    return _Verdict(
        "pass",
        "no turn the run refused left a commit of its own on HEAD",
        evidence,
    )


def _commit_failure_classification(payload: Mapping[str, Any]) -> str | None:
    """The token the run gave a failed commit, explicit key before outcome."""
    for key in ("failure_classification", "status_result"):
        value = payload.get(key)
        if isinstance(value, str) and value and value != "success":
            return value
    return None


def _commit_failure_reason(payload: Mapping[str, Any]) -> str | None:
    """Where the run said why a commit failed.

    The bundle carries no command output by design, so a reason is either named
    in the record itself or written to a stream the record points at and sizes.
    A failure with neither left nothing for an auditor to read.
    """
    for key in ("failure_reason", "exec_error", "status_error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for path_key, size_key in (
        ("stderr_path", "stderr_chars"),
        ("stdout_path", "stdout_chars"),
    ):
        path = payload.get(path_key)
        size = payload.get(size_key)
        if isinstance(path, str) and path and isinstance(size, int) and size > 0:
            return path
    return None


def _check_commit_failure_accounting(context: _CheckContext) -> _Verdict:
    """Grade the run's own account of every commit it recorded as failed.

    A failed commit is recorded once, in ``commit_finished``, and that record
    already carries the outcome that classified the failure and the streams the
    failure was written to. Reading them here is what lets the unknown-anomalies
    catch-all stop reporting a failed commit: this check grades every record that
    catch-all would have selected and the zero-exit failures it would have
    missed, so a record that names no classification, contradicts its own exit
    code, or points at no reason still surfaces by name.
    """
    problems: list[str] = []
    references: list[str] = []
    recorded = 0
    failures = 0
    for record in context.records:
        if record.get("name") != events.COMMIT_FINISHED_EVENT:
            continue
        recorded += 1
        payload = audit_evidence_helpers._mapping(record.get("payload"))
        if not audit_evidence_failures._commit_shows_failure(payload):
            continue
        failures += 1
        label = audit_evidence_failures._commit_record_label(payload)
        references.append(str(payload.get("artifact_dir")))
        status_result = payload.get("status_result")
        returncode = payload.get("returncode")
        if _commit_failure_classification(payload) is None:
            problems.append(f"{label} failed without classifying the failure")
        elif isinstance(status_result, str) and status_result not in _COMMIT_RESULTS:
            problems.append(
                f"{label} recorded {status_result}, which is no commit outcome ASL "
                "writes"
            )
        elif (
            isinstance(status_result, str)
            and isinstance(returncode, int)
            and (returncode != 0) != (status_result == "nonzero-exit")
        ):
            problems.append(
                f"{label} recorded {status_result} against exit {returncode}"
            )
        if payload.get("success") is True:
            problems.append(f"{label} claimed success while recording a failure")
        if _commit_failure_reason(payload) is None:
            problems.append(f"{label} failed without recording why")

    evidence = context.evidence(
        *references,
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if problems:
        return _Verdict(
            "fail",
            f"the run left a commit failure unaccounted for: {_joined(problems)}",
            evidence,
        )
    if context.events_shed:
        return _Verdict(
            "unavailable",
            "the event log was shed under the size cap, so the run's commit "
            "results cannot be read in full",
            evidence,
        )
    if not context.records:
        return _Verdict(
            "unavailable",
            "the run recorded no events, so it recorded no commit outcome to grade",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{failures} of {recorded} recorded commit(s) failed, each naming its "
        "classification and where its reason was written",
        evidence,
    )


def _unresolved_commit_group_rows(context: _CheckContext) -> list[dict[str, Any]]:
    """The commit groups the run ended still owing, by its own account.

    ``unresolved_commit_groups`` is the field the runner writes as it closes,
    so its presence is the run stating the answer directly. A bundle without it
    predates that field, and is read from the durable group rows instead —
    restricted to groups the coordinator marked ``failed``, because a
    ``pending`` row in an older bundle is as likely to be a group still in
    flight as one that was lost.
    """
    section = context.section("commits")
    if "unresolved_commit_groups" in section:
        return [
            audit_evidence_helpers._mapping(row)
            for row in audit_evidence_helpers._sequence(
                section["unresolved_commit_groups"]
            )
        ]
    return [
        audit_evidence_helpers._mapping(row)
        for row in audit_evidence_helpers._sequence(section.get("commit_groups"))
        if audit_evidence_helpers._mapping(row).get("status")
        == commit_groups.GROUP_STATUS_FAILED
    ]


def _unresolved_group_label(row: Mapping[str, Any]) -> str:
    session_id = row.get("session_id")
    scope = "the end-of-run commit" if session_id is None else f"session {session_id}"
    alias = row.get("failed_alias")
    return f"{scope} in {alias}" if alias else scope


def _check_commit_group_resolution(context: _CheckContext) -> _Verdict:
    """Grade whether the run committed everything it set out to commit.

    A failed commit does not pause the run where it fails: the group is marked
    failed, retried at the next commit boundary, and the run carries on. That
    is only safe while a group still owed when the run ends is impossible to
    miss, so this check is where the loss is named. Without it the one failure
    mode soft-fail introduces — a run that finishes reporting success while a
    session's changes were never committed — would be graded by nothing.

    A run that spent the whole retry budget now pauses at its next selection
    boundary rather than working past the loss, and it still reaches here
    owing the group, which is deliberate: the barrier decides that nothing
    further is built on top of the missing commit, not that the commit was
    made. The changes are still uncommitted and an operator still has to
    settle them or relaunch into the recovery path, so a paused run earns the
    same failing verdict a finished one does (ASI-138).
    """
    unresolved = _unresolved_commit_group_rows(context)
    references = [
        str(attempt.get("reference"))
        for attempt in audit_evidence_helpers._sequence(
            context.section("commits").get("attempts")
        )
        if isinstance(attempt, Mapping)
    ]
    evidence = context.evidence(
        context.section("state").get("reference"),
        *references,
    )
    if unresolved:
        return _Verdict(
            "fail",
            "the run ended owing commits it never made: "
            f"{_joined([_unresolved_group_label(row) for row in unresolved])}",
            evidence,
        )
    if not context.section("state"):
        return _Verdict(
            "unavailable",
            "the run's state projection could not be read, so nothing says "
            "whether the commits it owed were made",
            evidence,
        )
    return _Verdict(
        "pass",
        "every commit group the run opened reached completion",
        evidence,
    )
