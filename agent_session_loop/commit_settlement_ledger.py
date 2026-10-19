# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Durable commit-group failure and summary records."""

from . import commit_settlement_types as _types

globals().update(
    {name: value for name, value in vars(_types).items() if not name.startswith("__")}
)


def _upsert_commit_group(payload: dict[str, Any], group: Mapping[str, Any]) -> None:
    groups = payload.setdefault("commit_groups", [])
    group_id = group.get("group_id")
    for index, existing in enumerate(groups):
        if isinstance(existing, Mapping) and existing.get("group_id") == group_id:
            groups[index] = dict(group)
            return
    groups.append(dict(group))


#: Every soft-failed commit attempt this run made, in the order it made them.
#: A commit failure does not pause the run where it fails, so this is the
#: durable trace that one happened at all — including the attempt that failed
#: before a durable group existed, which leaves no ``commit_groups`` row behind,
#: and the last attempt of a spent budget, which is what the loop's barrier
#: pauses on.
COMMIT_FAILURES_KEY = "commit_failures"

#: The commit groups still owed when the run ended, written once by
#: ``terminalize``. It is the run's own admission that commits were lost, and
#: the field the supervisor audit's commit-group-resolution check reads.
UNRESOLVED_COMMIT_GROUPS_KEY = "unresolved_commit_groups"

#: What a recoverable pause committed of the work its turn retained, written
#: onto that turn's payload. Session 5 puts the same record on the run payload
#: and into the pause details, where the supervisor and the audit read it.
PAUSED_WORK_COMMIT_KEY = "paused_work_commit"

#: The ``guard`` label on that record. A settled pause is not a guard refusal,
#: but the record sits beside ``session_commit`` and ``repair_commit`` on the
#: same turn payload, and those name the boundary that wrote them; this one
#: does the same so a reader never has to infer which boundary spoke.
PAUSED_WORK_COMMIT_GUARD = "paused-work-commit"

#: How many times one commit group is retried inside a single run before the
#: run stops attempting it. A transient failure heals on the first retry; a
#: repository that refuses every time must not turn the session loop into a
#: commit loop, so the group is left owed for a later run to recover. Each
#: attempt is a real harness call that writes the commit message, so the budget
#: is small on purpose. Spending it stops the run as well as the retries: the
#: loop pauses at its next selection boundary rather than launching a session
#: over the commit this run could not make.
MAX_COMMIT_GROUP_RETRIES = 2


def _commit_failure_record(
    *,
    scope: str,
    session_id: str | None,
    group: Mapping[str, Any] | None,
    commit: Mapping[str, Any] | None,
    error: str | None,
    message: str,
) -> dict[str, Any]:
    """Describe one commit failure the run absorbed instead of pausing on."""
    return {
        "scope": scope,
        "session_id": session_id,
        "group_id": group.get("group_id") if group is not None else None,
        "failed_alias": group.get("failed_alias") if group is not None else None,
        "message": message,
        "error": error,
        "commit": dict(commit) if commit is not None else None,
        "commit_group": dict(group) if group is not None else None,
        "recorded_at": clock.isoformat(clock.utc_now()),
    }


def _append_commit_failure(payload: dict[str, Any], record: Mapping[str, Any]) -> None:
    failures = payload.setdefault(COMMIT_FAILURES_KEY, [])
    failures.append(dict(record))


def _unresolved_commit_groups(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Summarize every commit group this run never brought to ``complete``.

    Two shapes of loss are reported side by side. A durable group that ended
    ``failed`` or ``pending`` is named by its own row, because it is the one a
    later run can still recover. An attempt that failed before any group was
    created carries no row at all, so its recorded failure is summarized in
    place — otherwise the only commit failure that leaves no durable evidence
    would also be the only one a run could end silent about.
    """
    summaries: list[dict[str, Any]] = []
    groups = payload.get("commit_groups")
    if isinstance(groups, list):
        for raw in groups:
            if not isinstance(raw, Mapping):
                continue
            status = raw.get("status")
            if status == commit_groups.GROUP_STATUS_COMPLETE:
                continue
            summaries.append(
                {
                    "group_id": raw.get("group_id"),
                    "scope": raw.get("scope"),
                    "session_id": raw.get("session_id"),
                    "status": status,
                    "failed_alias": raw.get("failed_alias"),
                    "remaining_aliases": list(raw.get("remaining_aliases") or ()),
                    "committed_aliases": [
                        row.get("key")
                        for row in raw.get("successful_repositories") or ()
                        if isinstance(row, Mapping)
                    ],
                }
            )
    failures = payload.get(COMMIT_FAILURES_KEY)
    if isinstance(failures, list):
        for raw in failures:
            if not isinstance(raw, Mapping) or raw.get("group_id") is not None:
                continue
            summaries.append(
                {
                    "group_id": None,
                    "scope": raw.get("scope"),
                    "session_id": raw.get("session_id"),
                    "status": commit_groups.GROUP_STATUS_FAILED,
                    "failed_alias": None,
                    "remaining_aliases": [],
                    "committed_aliases": [],
                    "error": raw.get("error"),
                }
            )
    return summaries


def _unresolved_commit_group_label(summary: Mapping[str, Any]) -> str:
    """Name one commit group in the sentences an operator reads.

    The session id alone does not identify the group, because one session can
    owe two commits: the accepted one its verification earned, and the one a
    recoverable pause settled for the work its failed turn retained. Those are
    separate durable groups by scope, so a label that read only the session id
    printed the same warning for both and hid the one thing the operator is
    deciding on — whether the lost commit covers verified work or work no
    verification ever passed. A ``session-paused`` group therefore says so.

    The group's own ``scope`` is read, never the commit payload's: a paused
    group's commits are stamped ``scope: "session"`` deliberately. A summary
    written before this scope existed carries no ``scope`` at all, and falls
    back to naming the session, which is what it always did.
    """
    session_id = summary.get("session_id")
    if session_id is None:
        scope = "end-of-run"
    elif summary.get("scope") == commit_groups.GROUP_SCOPE_SESSION_PAUSED:
        scope = f"session {session_id} retained work"
    else:
        scope = f"session {session_id}"
    alias = summary.get("failed_alias")
    return f"{scope} ({alias})" if alias else scope


def _persist_commit_group(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    run_id: str,
    group: dict[str, Any],
    phase: str,
) -> None:
    if phase == "started":
        event_name = events.COMMIT_GROUP_STARTED_EVENT
    elif phase == "complete":
        event_name = events.COMMIT_GROUP_FINISHED_EVENT
    else:
        event_name = events.COMMIT_GROUP_UPDATED_EVENT
    with persistence.lock:
        _upsert_commit_group(payload, group)
        persistence.append_event(
            event_path,
            event_name,
            {
                "run_id": run_id,
                "phase": phase,
                "group": group,
            },
        )
        persistence.write_state(payload)
