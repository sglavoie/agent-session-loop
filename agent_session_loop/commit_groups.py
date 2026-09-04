"""Durable, restart-safe orchestration for repository commit groups.

The runner owns command execution and operational persistence.  This module
owns the state machine that selects the next repository, records successful
SHAs, and reconstructs an unfinished group after a process restart.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GROUP_SCHEMA_VERSION = 1
LEGACY_REPOSITORY_KEY = "."
GROUP_STATUS_PENDING = "pending"
GROUP_STATUS_FAILED = "failed"
GROUP_STATUS_COMPLETE = "complete"
GROUP_STATUSES = (
    GROUP_STATUS_PENDING,
    GROUP_STATUS_FAILED,
    GROUP_STATUS_COMPLETE,
)

#: One session's work committed on the accepted path, after its verification
#: passed.
GROUP_SCOPE_SESSION = "session"
#: One session's *retained* work committed at a recoverable pause instead of at
#: acceptance. It is a scope of its own rather than a flag on
#: ``GROUP_SCOPE_SESSION`` because group identity is hashed from ``{scope,
#: session_id, ordered_aliases}``: reusing the accepted scope would give the
#: paused commit the same ``group_id`` as the accepted commit the same session
#: earns afterwards, and persisting it would overwrite that completed row in
#: place.
GROUP_SCOPE_SESSION_PAUSED = "session-paused"
#: The end-of-run sweep, which belongs to no session.
GROUP_SCOPE_END = "end"
#: The scopes that name exactly one session. Membership here, not equality with
#: a single scope, is what the scope/session-id invariant tests.
SESSION_GROUP_SCOPES = (GROUP_SCOPE_SESSION, GROUP_SCOPE_SESSION_PAUSED)
GROUP_SCOPES = (*SESSION_GROUP_SCOPES, GROUP_SCOPE_END)


class CommitGroupError(RuntimeError):
    """Raised when durable commit-group evidence is malformed or unsafe."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def repository_key(alias: str | None) -> str:
    return alias if alias is not None else LEGACY_REPOSITORY_KEY


def create_target(
    *,
    alias: str | None,
    root: Path,
    repository_path: str,
    qualified_paths: Sequence[str] = (),
    staging_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the canonical durable projection for one commit target.

    ``qualified_paths`` is the full change evidence for the target, ignored
    files included.  ``staging_paths`` is the subset the runner may hand to
    ``git add``; the two are separate because evidence must stay complete
    while staging must not name a path git refuses to add.
    """
    return {
        "key": repository_key(alias),
        "alias": alias,
        "root": str(root),
        "repository_path": repository_path,
        "qualified_paths": list(qualified_paths),
        "staging_paths": list(staging_paths),
    }


def _check_scope(scope: object, *, has_session_id: bool) -> None:
    """Enforce the scope/session-id invariant for writers and readers alike.

    :func:`create_group` and :func:`validate_group` are mirrors of one another,
    so the rule is stated once here: a scope that names a session requires
    exactly one session id, and ``end`` requires none. Relaxing the check in
    one of the two callers alone would split the invariant and let a group be
    written that could never be read back.
    """
    if scope not in GROUP_SCOPES:
        raise CommitGroupError(
            "commit group scope must be one of " + ", ".join(GROUP_SCOPES)
        )
    if (scope in SESSION_GROUP_SCOPES) != has_session_id:
        raise CommitGroupError(
            "session-scoped commit groups require exactly one session id"
        )


def create_group(
    *,
    scope: str,
    session_id: str | None,
    targets: Sequence[Mapping[str, Any]],
    initial_heads: Mapping[str, str | None],
    verbatim_message: str | None = None,
) -> dict[str, Any]:
    """Create an unresolved group whose identity is stable across restarts.

    ``scope`` is one of :data:`GROUP_SCOPES`. Identity is hashed from
    ``{scope, session_id, ordered_aliases}``, so the scope is the field that
    separates the two commits one session can owe — the accepted one and the
    one settled at a recoverable pause — even though both name the same
    session and the same repositories.

    ``verbatim_message`` is the message a group commits *literally*, and is
    ``None`` for every group whose message is generated. It is durable rather
    than derived because a group outlives the process that created it: a later
    run recovering an unfinished group has no other route back to the exact
    text, and re-deriving one would make the recovered commit say something
    the original attempt did not. It is deliberately outside the identity
    hash — the message describes the commit, it does not name which commit is
    owed, and a group that changed identity when its text changed could no
    longer be found by the id its own failure recorded.
    """
    _check_scope(scope, has_session_id=session_id is not None)
    target_rows = [dict(target) for target in targets]
    ordered_aliases = [str(target.get("key")) for target in target_rows]
    if any(not alias for alias in ordered_aliases):
        raise CommitGroupError("commit target keys must be non-empty")
    if len(set(ordered_aliases)) != len(ordered_aliases):
        raise CommitGroupError("commit target keys must be unique")
    identity = {
        "scope": scope,
        "session_id": session_id,
        "ordered_aliases": ordered_aliases,
    }
    group_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    now = _utc_now()
    return {
        "version": GROUP_SCHEMA_VERSION,
        "group_id": group_id,
        "identity": identity,
        "scope": scope,
        "session": int(session_id)
        if session_id and session_id.isdigit()
        else session_id,
        "session_id": session_id,
        "ordered_aliases": ordered_aliases,
        "verbatim_message": verbatim_message,
        "repositories": target_rows,
        "initial_heads": {alias: initial_heads.get(alias) for alias in ordered_aliases},
        "successful_repositories": [],
        "skipped_repositories": [],
        "failed_alias": None,
        "remaining_aliases": list(ordered_aliases),
        "status": GROUP_STATUS_PENDING,
        "created_at": now,
        "updated_at": now,
    }


def _target_map(group: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = group.get("repositories")
    if not isinstance(rows, list):
        raise CommitGroupError("commit group repositories must be a list")
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise CommitGroupError("commit group repository entries must be objects")
        target = dict(raw)
        key = target.get("key")
        if not isinstance(key, str) or not key or key in result:
            raise CommitGroupError("commit group repository keys are invalid")
        result[key] = target
    return result


def validate_group(group: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy a durable group before using it for mutations."""
    copied = copy.deepcopy(dict(group))
    if copied.get("version") != GROUP_SCHEMA_VERSION:
        raise CommitGroupError("commit group version is unsupported")
    group_id = copied.get("group_id")
    if (
        not isinstance(group_id, str)
        or len(group_id) != 64
        or any(character not in "0123456789abcdef" for character in group_id)
    ):
        raise CommitGroupError("commit group id is invalid")
    ordered = copied.get("ordered_aliases")
    if not isinstance(ordered, list) or any(
        not isinstance(alias, str) or not alias for alias in ordered
    ):
        raise CommitGroupError("commit group ordered aliases are invalid")
    targets = _target_map(copied)
    if list(targets) != ordered:
        raise CommitGroupError("commit group repositories do not match alias order")
    if copied.get("status") not in GROUP_STATUSES:
        raise CommitGroupError("commit group status is invalid")
    # A group written before the field existed has no key at all, which reads
    # as a generated message and is what every group of that vintage was. A
    # key that is present but not a usable message is evidence a reader would
    # otherwise commit with.
    verbatim_message = copied.get("verbatim_message")
    if verbatim_message is not None and (
        not isinstance(verbatim_message, str) or not verbatim_message.strip()
    ):
        raise CommitGroupError("commit group verbatim message is invalid")
    scope = copied.get("scope")
    session_id = copied.get("session_id")
    _check_scope(scope, has_session_id=isinstance(session_id, str))
    identity = {
        "scope": scope,
        "session_id": session_id,
        "ordered_aliases": ordered,
    }
    if copied.get("identity") != identity:
        raise CommitGroupError("commit group identity fields are inconsistent")
    expected_group_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    if group_id != expected_group_id:
        raise CommitGroupError("commit group id does not match its identity")
    initial_heads = copied.get("initial_heads")
    if not isinstance(initial_heads, Mapping) or set(initial_heads) != set(ordered):
        raise CommitGroupError("commit group initial heads do not match aliases")
    successful = _recorded_keys(copied, "successful_repositories")
    skipped = _recorded_keys(copied, "skipped_repositories")
    if successful & skipped or not (successful | skipped).issubset(ordered):
        raise CommitGroupError("commit group repository outcomes are inconsistent")
    remaining = copied.get("remaining_aliases")
    expected_remaining = [
        alias for alias in ordered if alias not in successful and alias not in skipped
    ]
    if remaining != expected_remaining:
        raise CommitGroupError("commit group remaining aliases are inconsistent")
    failed_alias = copied.get("failed_alias")
    if failed_alias is not None and failed_alias not in expected_remaining:
        raise CommitGroupError("commit group failed alias is inconsistent")
    return copied


def _recorded_keys(group: Mapping[str, Any], field: str) -> set[str]:
    rows = group.get(field)
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("key"))
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("key"), str)
    }


def _refresh_remaining(group: dict[str, Any]) -> None:
    finished = _recorded_keys(group, "successful_repositories") | _recorded_keys(
        group, "skipped_repositories"
    )
    group["remaining_aliases"] = [
        alias for alias in group["ordered_aliases"] if alias not in finished
    ]
    group["updated_at"] = _utc_now()


def _success_record(
    target: Mapping[str, Any],
    *,
    commit_sha: str,
    commit_index: int | None,
    recovered: bool,
) -> dict[str, Any]:
    return {
        "key": target["key"],
        "alias": target.get("alias"),
        "repository_path": target.get("repository_path"),
        "commit_sha": commit_sha,
        "commit_index": commit_index,
        "recovered": recovered,
    }


def coordinate_group(
    group: Mapping[str, Any],
    *,
    is_clean: Callable[[Mapping[str, Any]], bool],
    read_head: Callable[[Mapping[str, Any]], str | None],
    run_commit: Callable[[Mapping[str, Any], str], Mapping[str, Any]],
    persist: Callable[[dict[str, Any], str], None],
    skip_clean: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Drive one group to completion or the first repository failure.

    A clean unresolved repository is never committed again.  When its HEAD
    differs from the group's initial HEAD, recovery records that SHA as a
    successful commit; otherwise the repository is recorded as clean/skipped.
    """
    current = validate_group(group)
    targets = _target_map(current)
    successful = _recorded_keys(current, "successful_repositories")
    skipped = _recorded_keys(current, "skipped_repositories")
    current["status"] = GROUP_STATUS_PENDING
    current["failed_alias"] = None
    _refresh_remaining(current)
    persist(current, "resumed")

    for key in current["ordered_aliases"]:
        if key in successful or key in skipped:
            continue
        target = targets[key]
        if skip_clean and is_clean(target):
            head = read_head(target)
            initial_head = current.get("initial_heads", {}).get(key)
            if head is not None and head != initial_head:
                current["successful_repositories"].append(
                    _success_record(
                        target,
                        commit_sha=head,
                        commit_index=None,
                        recovered=True,
                    )
                )
                successful.add(key)
                phase = "recovered-clean-commit"
            else:
                current["skipped_repositories"].append(
                    {
                        "key": key,
                        "alias": target.get("alias"),
                        "repository_path": target.get("repository_path"),
                        "reason": "clean",
                        "head": head,
                    }
                )
                skipped.add(key)
                phase = "skipped-clean"
            _refresh_remaining(current)
            persist(current, phase)
            continue

        commit = dict(run_commit(target, current["group_id"]))
        if commit.get("success"):
            sha = commit.get("commit_sha")
            if not isinstance(sha, str) or not sha:
                commit["success"] = False
                commit["status_result"] = "missing-head"
            else:
                current["successful_repositories"].append(
                    _success_record(
                        target,
                        commit_sha=sha,
                        commit_index=(
                            commit.get("commit_index")
                            if isinstance(commit.get("commit_index"), int)
                            else None
                        ),
                        recovered=False,
                    )
                )
                successful.add(key)
                _refresh_remaining(current)
                persist(current, "repository-committed")
                continue

        current["status"] = GROUP_STATUS_FAILED
        current["failed_alias"] = key
        _refresh_remaining(current)
        persist(current, "repository-failed")
        return current, commit

    current["status"] = GROUP_STATUS_COMPLETE
    current["failed_alias"] = None
    _refresh_remaining(current)
    persist(current, "complete")
    return current, None


def _merge_finished_commit(group: dict[str, Any], commit: Mapping[str, Any]) -> None:
    if commit.get("group_id") != group.get("group_id") or not commit.get("success"):
        return
    key = commit.get("repository_key")
    sha = commit.get("commit_sha")
    if not isinstance(key, str) or not isinstance(sha, str) or not sha:
        return
    if key not in group.get("ordered_aliases", ()):
        return
    if key in _recorded_keys(group, "successful_repositories"):
        return
    target = _target_map(group)[key]
    group.setdefault("successful_repositories", []).append(
        _success_record(
            target,
            commit_sha=sha,
            commit_index=(
                commit.get("commit_index")
                if isinstance(commit.get("commit_index"), int)
                else None
            ),
            recovered=True,
        )
    )
    _refresh_remaining(group)


def load_unresolved_group(
    *,
    state_path: Path,
    event_path: Path,
) -> dict[str, Any] | None:
    """Reconstruct the oldest unfinished group from state and event evidence."""
    groups: dict[str, dict[str, Any]] = {}

    try:
        state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError, OSError, ValueError, json.JSONDecodeError:
        state_payload = {}
    if isinstance(state_payload, dict) and isinstance(
        state_payload.get("commit_group_load_error"), str
    ):
        raise CommitGroupError(state_payload["commit_group_load_error"])
    state_groups = (
        state_payload.get("commit_groups") if isinstance(state_payload, dict) else None
    )
    if isinstance(state_groups, list):
        for raw in state_groups:
            if not isinstance(raw, Mapping):
                raise CommitGroupError(
                    "persisted commit group state contains a non-object"
                )
            group = validate_group(raw)
            groups[group["group_id"]] = group
    elif state_groups is not None:
        raise CommitGroupError("persisted commit_groups state must be a list")

    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError, OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError, json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping):
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if record.get("name") in {
            "commit_group_started",
            "commit_group_updated",
            "commit_group_finished",
        }:
            raw_group = payload.get("group")
            if not isinstance(raw_group, Mapping):
                raise CommitGroupError("commit group event has no group object")
            group = validate_group(raw_group)
            groups[group["group_id"]] = group
        elif record.get("name") == "commit_finished":
            group_id = payload.get("group_id")
            if isinstance(group_id, str) and group_id in groups:
                _merge_finished_commit(groups[group_id], payload)

    unresolved = [
        group
        for group in groups.values()
        if group.get("status") != GROUP_STATUS_COMPLETE
    ]
    if not unresolved:
        return None
    unresolved.sort(
        key=lambda group: (str(group.get("created_at", "")), group["group_id"])
    )
    return unresolved[0]
