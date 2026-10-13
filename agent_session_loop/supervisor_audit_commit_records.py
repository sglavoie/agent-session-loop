"""Commit, attribution and guard-head-move records read from the evidence bundle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import supervisor_audit_evidence_commits as audit_evidence_commits
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_models import _CheckContext


def _commit_records(context: _CheckContext) -> list[Mapping[str, Any]]:
    """Every commit the bundle records, however that commit was learned.

    Covers the runner's own commit projections and the attempts read from
    ``commit-*`` directories alike, including the entries the audit derived
    from observed head transitions, because a commit the runner never ran
    still put the write it names into history.
    """
    commits = context.section("commits")
    records = [
        audit_evidence_helpers._mapping(item)
        for item in audit_evidence_helpers._sequence(commits.get("commits"))
    ]
    records.extend(
        audit_evidence_helpers._mapping(item)
        for item in audit_evidence_helpers._sequence(commits.get("commit_groups"))
    )
    records.extend(
        audit_evidence_helpers._mapping(
            audit_evidence_helpers._mapping(item).get("result")
        )
        for item in audit_evidence_helpers._sequence(commits.get("attempts"))
    )
    return [record for record in records if record]


def _commit_repository_alias(record: Mapping[str, Any]) -> str | None:
    """The repository one commit record names, or ``None`` when it names none.

    A record filed under a linked worktree is reduced to the repository the
    worktree belongs to, because a commit in a worktree lands in that
    repository's history and that is the repository a write-scope question asks
    about.
    """
    for key in ("repository_alias", "repo", "alias", "repository_key", "key"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value.split(audit_evidence_commits._WORKTREE_KEY_SEPARATOR, 1)[0]
    return None


def _commit_attributions(
    context: _CheckContext,
    *,
    skip: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[tuple[str | None, str | None]]:
    """The session and repository every commit in the bundle names.

    Either half may be ``None`` when the record does not say, and both readings
    matter: an unnamed session cannot be matched to a violation, and an unnamed
    repository cannot be ruled out of one.

    A commit group is descended into rather than read whole. The group is a
    container -- its per-repository detail lives in the entries beneath it, and
    the group itself names no repository -- so reading it whole would report a
    commit of unknown repository, which is the one shape that can never be
    ruled out (ASI-9). A group that names no repository anywhere committed
    nothing and contributes nothing.

    ``skip`` withholds individual commit entries, wherever they sit; a group
    whose every entry is withheld contributes nothing.
    """
    pairs: list[tuple[str | None, str | None]] = []
    for record in _commit_records(context):
        session = _write_scope_session(record.get("session_id"))
        nested = [
            audit_evidence_helpers._mapping(item)
            for item in audit_evidence_helpers._sequence(record.get("repositories"))
        ]
        if nested:
            pairs.extend(
                (
                    _write_scope_session(entry.get("session_id"), session),
                    _commit_repository_alias(entry),
                )
                for entry in nested
                if skip is None or not skip(entry)
            )
            continue
        if skip is not None and skip(record):
            continue
        aliases = [
            alias
            for alias in audit_evidence_helpers._sequence(record.get("ordered_aliases"))
            if isinstance(alias, str) and alias
        ]
        if aliases:
            pairs.extend((session, alias) for alias in aliases)
        elif "repositories" not in record and "ordered_aliases" not in record:
            pairs.append((session, _commit_repository_alias(record)))
    return pairs


def _guard_head_moves(guard: Mapping[str, Any]) -> set[tuple[str | None, Any]]:
    """The ``(repository, head after)`` pairs one guard record saw move."""
    moves: set[tuple[str | None, Any]] = set()
    for alias, change in audit_evidence_helpers._mapping(guard.get("changed")).items():
        change = audit_evidence_helpers._mapping(change)
        transition = audit_evidence_helpers._mapping(change.get("head_transition"))
        # Endpoints come from one source, as in _reconcile_observed_commits.
        source = transition if transition else change
        head_after = source.get("after" if transition else "head_after")
        before = source.get("before" if transition else "head_before")
        if isinstance(head_after, str) and head_after and head_after != before:
            moves.add(
                (
                    str(alias).split(audit_evidence_commits._WORKTREE_KEY_SEPARATOR, 1)[
                        0
                    ],
                    head_after,
                )
            )
    return moves


def _write_scope_session(*candidates: Any) -> str | None:
    """Name the session a write-scope record belongs to, first source wins.

    A guard record carries ``session_id`` as a string while a pause carries the
    numeric alias under ``session``, so both are normalized to text before they
    are matched against each other.
    """
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None
