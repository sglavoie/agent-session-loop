"""Commit evidence: recorded and observed commit sections and their reconciliation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import state
from .supervisor_audit_evidence_collector import _Collector, _projection
from .supervisor_audit_evidence_constants import (
    OBSERVED_COMMIT_ORIGIN,
    PAUSED_WORK_COMMIT_KEY,
    REASON_MALFORMED,
    REASON_MISSING,
)
from .supervisor_audit_evidence_failures import (
    _commit_record_label,
    _commit_shows_failure,
)
from .supervisor_audit_evidence_helpers import _mapping, _read_json_file, _sequence

#: Ref namespace holding a cache of some other repository's branches. It is
#: written by ``git fetch`` and by nothing a working tree does.
_REMOTE_TRACKING_REF_PREFIX = "refs/remotes/"


_COMMIT_STATE_KEYS = (
    "commit_mode",
    "repair_commit_mode",
    "commit_groups",
    "commit_failures",
    "unresolved_commit_groups",
    "commits",
    # The one commit a run makes that no accepted turn produced. Without it the
    # bundle shows a `session-paused` group and a commit with no explanation for
    # either; with it, the settlement's own verdict travels beside them.
    PAUSED_WORK_COMMIT_KEY,
)


#: A commit attempt's artifact directory, as ``run_commit`` names it:
#: ``commit-{index:04d}-{suffix}``. Matching the numbered prefix rather than
#: ``commit-`` alone keeps sibling directories such as ``commit-group-recovery``
#: (``run_commit.RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME``), which hold messages
#: rather than attempts, from reading as an attempt with no result (ASI-117).
_COMMIT_ATTEMPT_DIR_RE = re.compile(r"^commit-\d{4,}-")


def _is_commit_attempt_dir_name(name: str) -> bool:
    """Whether a run-directory entry name is a commit attempt's directory."""
    return _COMMIT_ATTEMPT_DIR_RE.match(name) is not None


#: How ``repository_evidence`` keys a linked worktree: the repository it
#: belongs to, then the worktree's own root.
_WORKTREE_KEY_SEPARATOR = "::worktree::"


def _declared_repository_alias(guard: Mapping[str, Any], key: str) -> str:
    """Name a guard's changed-repository key the way the plan declares it.

    The change map is keyed by workspace-relative repository path, and by
    ``<path>::worktree::<root>`` for a linked worktree, while session scopes --
    and therefore every write-scope question asked of them -- are keyed by
    registry alias. Comparing the two namespaces directly reads a commit to a
    declared repository as a commit to an undeclared one, which is what made a
    routine run report a scope breach it never committed (ASI-117).

    A linked worktree resolves to the repository it belongs to because it
    shares that repository's object store and refs: a commit made there lands
    in the registered repository, so the registered repository is what the
    declared scope has to answer for. A key that resolves to no registered
    repository is returned verbatim, so an unknown repository stays unknown.
    """
    registered = [
        _mapping(entry) for entry in _sequence(guard.get("registered_repositories"))
    ]
    aliases: set[str] = set()
    by_path: dict[str, str] = {}
    for entry in registered:
        alias = entry.get("alias")
        if not isinstance(alias, str) or not alias:
            continue
        aliases.add(alias)
        path = entry.get("path")
        if isinstance(path, str) and path:
            by_path[path] = alias
    root = key.split(_WORKTREE_KEY_SEPARATOR, 1)[0]
    if root in aliases:
        return root
    return by_path.get(root, key)


def _observed_repository_writes(
    turns: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Repository writes the write guard saw, whoever made them.

    ``commit_mode`` describes only the commits the runner itself was asked to
    make, so a worker that commits under a null commit mode leaves the commit
    projection empty while the guard records the move. Harvesting the guard
    here means the projection answers "what landed" rather than "what the
    runner intended", which is the question an auditor is asking.

    A moved ref counts even when HEAD stayed put: a commit written to a branch
    the worktree does not have checked out still landed. A moved *remote-tracking*
    ref does not, because it is a cache of another repository that ``git fetch``
    rewrites and nothing a turn writes touches. Projecting one as a commit put a
    commit the session never made in front of the checks that read this
    projection, and one of them read it as the write-scope guard having failed to
    hold a violation it did hold (ASI-9).
    """
    observed: list[dict[str, Any]] = []
    for turn in turns:
        result = _mapping(turn.get("result"))
        guard = _mapping(result.get("workspace_write_guard"))
        for key, raw_changes in _mapping(guard.get("changed")).items():
            changes = _mapping(raw_changes)
            refs = [
                str(_mapping(entry).get("ref"))
                for entry in _sequence(changes.get("refs_changed"))
                if _mapping(entry).get("ref")
                and not str(_mapping(entry)["ref"]).startswith(
                    _REMOTE_TRACKING_REF_PREFIX
                )
            ]
            head_changed = changes.get("head_changed") is True
            if not head_changed and not refs:
                continue
            observed.append(
                {
                    "turn": turn.get("name"),
                    "directory": turn.get("directory"),
                    "repository": _declared_repository_alias(guard, str(key)),
                    "repository_key": str(key),
                    "session_id": result.get("session_id"),
                    "head_changed": head_changed,
                    "transition": _mapping(changes.get("head_transition")) or None,
                    "head_before": changes.get("head_before"),
                    "head_after": changes.get("head_after"),
                    "refs_changed": refs,
                }
            )
    return observed


def _observed_head_transitions(
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The observed writes that moved HEAD, in the shape bundles record."""
    return [
        {
            "turn": entry.get("turn"),
            "repository": entry.get("repository"),
            "transition": entry.get("transition"),
        }
        for entry in observed
        if entry.get("head_changed") is True
    ]


def _recorded_commit_heads(section: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The repository and resulting head of every commit the run recorded."""
    recorded = [_mapping(item) for item in _sequence(section.get("commits"))]
    recorded.extend(
        _mapping(_mapping(item).get("result"))
        for item in _sequence(section.get("attempts"))
    )
    pairs: set[tuple[str, str]] = set()
    for entry in recorded:
        alias = (
            entry.get("repository_alias")
            or entry.get("repo")
            or entry.get("repository_key")
        )
        for key in ("head_after", "commit_sha"):
            head = entry.get(key)
            if isinstance(alias, str) and isinstance(head, str):
                pairs.add((alias, head))
    return pairs


def _reconcile_observed_commits(
    section: dict[str, Any], observed: Sequence[Mapping[str, Any]]
) -> None:
    """Project what the guard saw onto the commit records the run kept.

    A worker that commits on its own leaves ``attempts``, ``commit_groups`` and
    ``commits`` empty while the commits themselves sit in the repository, so
    the projections are derived here rather than left contradicting the guard.
    Every derived entry names its ``origin``, and one whose resulting head a
    recorded commit already claims is dropped instead of counted twice.
    """
    accounted = _recorded_commit_heads(section)
    commits: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    groups: dict[Any, dict[str, Any]] = {}
    for entry in observed:
        alias = entry.get("repository")
        transition = _mapping(entry.get("transition"))
        # A transition is one measured move, so its endpoints and its count
        # travel together. The entry's own heads are the session's baseline,
        # which a continuation merge can carry back past this turn's move;
        # pairing that baseline with the transition's count described a range
        # the count was never measured over (ASI-158).
        if transition:
            head_before = transition.get("before")
            head_after = transition.get("after")
        else:
            head_before = entry.get("head_before")
            head_after = entry.get("head_after")
        if (
            isinstance(alias, str)
            and isinstance(head_after, str)
            and (alias, head_after) in accounted
        ):
            continue
        commits_added = transition.get("commits_added")
        commit = {
            "origin": OBSERVED_COMMIT_ORIGIN,
            "turn": entry.get("turn"),
            "session_id": entry.get("session_id"),
            # The key stays verbatim so the exact repository the guard named is
            # never lost, while the alias is what a write-scope question reads.
            "repository_key": entry.get("repository_key") or alias,
            "repository_alias": alias,
            "repo": alias,
            "head_before": head_before,
            "head_after": head_after,
            "commit_sha": head_after if commits_added else None,
            "relation": transition.get("relation"),
            "commits_added": commits_added,
            "history_rewrite": transition.get("history_rewrite"),
            "refs_changed": list(entry.get("refs_changed") or ()),
        }
        commits.append(commit)
        attempts.append(
            {
                "name": entry.get("turn"),
                "reference": entry.get("directory"),
                "origin": OBSERVED_COMMIT_ORIGIN,
                "result": commit,
            }
        )
        session_id = entry.get("session_id")
        group = groups.setdefault(
            session_id if session_id is not None else entry.get("turn"),
            {
                "origin": OBSERVED_COMMIT_ORIGIN,
                "scope": "session" if session_id is not None else "end",
                "session_id": session_id,
                "ordered_aliases": [],
                "repositories": [],
            },
        )
        if alias not in group["ordered_aliases"]:
            group["ordered_aliases"].append(alias)
        group["repositories"].append(commit)
    if not commits:
        return
    section["attempts"] = list(_sequence(section.get("attempts"))) + attempts
    section["commits"] = list(_sequence(section.get("commits"))) + commits
    section["commit_groups"] = list(_sequence(section.get("commit_groups"))) + list(
        groups.values()
    )


def _reference_commit_streams(collector: _Collector, result: Mapping[str, Any]) -> None:
    """Index the retained streams a failed commit attempt points at.

    A failed attempt records the full ``git`` diagnostic in its own stdout and
    stderr files and carries only a bounded preview inline, so the paths are
    where the reason a commit was refused actually survives. Naming them in the
    result while indexing only the parent directory left the auditor unable to
    tell evidence it could read from a path it merely saw, and a path that is
    not on disk is recorded as an omission rather than passed over, so a missing
    stream is stated instead of being indistinguishable from one never written
    (ASI-137).
    """
    if not _commit_shows_failure(result):
        return
    for key in ("stderr_path", "stdout_path"):
        path = result.get(key)
        if not isinstance(path, str) or not path:
            continue
        stream = key.removesuffix("_path")
        if not Path(path).is_file():
            collector.omissions.record("commits", REASON_MISSING, path)
            continue
        collector.reference(
            path, kind="commit", detail=f"{_commit_record_label(result)} {stream}"
        )


def _commit_section(
    collector: _Collector,
    *,
    run_dir: Path,
    state_payload: Mapping[str, Any],
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What the run committed, from state projection plus per-commit results."""
    section = _projection(collector, state_payload, _COMMIT_STATE_KEYS)
    observed = _observed_repository_writes(turns)
    section["observed_head_transitions"] = _observed_head_transitions(observed)
    attempts: list[dict[str, Any]] = []
    try:
        directories = sorted(
            entry
            for entry in run_dir.iterdir()
            if entry.is_dir() and _is_commit_attempt_dir_name(entry.name)
        )
    except OSError:
        directories = []
    for directory in directories:
        result, reason = _read_json_file(directory / state.RESULT_FILE_NAME)
        entry: dict[str, Any] = {
            "name": directory.name,
            "reference": collector.reference(directory, kind="commit"),
        }
        if isinstance(result, Mapping):
            entry["result"] = collector.redact(result)
            _reference_commit_streams(collector, result)
        else:
            collector.omissions.record(
                "commits", reason or REASON_MALFORMED, str(directory)
            )
            entry["result"] = {"available": False, "reason": reason or REASON_MALFORMED}
        attempts.append(entry)
    section["attempts"] = attempts
    _reconcile_observed_commits(section, observed)
    return section
