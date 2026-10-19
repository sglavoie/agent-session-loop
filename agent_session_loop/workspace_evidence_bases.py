# ruff: noqa: F401, F403, F405, F821, F822, I001
"""What the registered checkouts look like, and whether a turn stayed inside them.

A session declares which repository aliases it may read and which it may write.
This module owns the projection that turns a raw workspace comparison -- two
snapshots of the registered checkouts, taken before and after a turn -- into the
verdict the write guard enforces: which repositories changed, which of those
changes the session's scope authorized, which are breaches, and which
repositories the guard could not observe at all.

It owns four things that the run loop used to hold inline:

* **The bases and the fail-closed lane.** :func:`_workspace_review_bases`
  projects the ordered per-repository HEAD and pre-existing dirty paths a review
  is judged against, and :func:`_workspace_snapshot_unavailable` names every
  registered repository a snapshot could not fingerprint.
* **The two guard projections.** :func:`_workspace_write_guard_evidence`
  classifies a workspace-vector comparison against one session's
  :class:`plans_session_scope.SessionScope`;
  :func:`_legacy_implementation_write_guard_evidence` projects the
  single-repository comparison a legacy run produces into the same schema, so
  both modes are read by one enforcement layer.
* **The two bounded pause suffixes.** :func:`_workspace_unavailable_summary` and
  :func:`_workspace_guard_violation_summary` name at most three subjects and a
  count, so a pause reason stays a sentence rather than a payload dump.
  :func:`_workspace_unavailable_message` supplies their shared explanation,
  distinguishing external write authority from repository capture failures;
  :func:`_legacy_write_guard_failure_message` applies the same explanation to
  a failed legacy verdict and names repository-set violations.
* **Scope-repair freshness.** :func:`_session_expansion_evidence` fingerprints an
  expansion file, :func:`_latest_workspace_scope_repair_requirement` recovers the
  latest uncleared scope violation for a session from the event log,
  :func:`_workspace_scope_repair_is_fresh` decides whether the operator actually
  re-expanded since that violation,
  :func:`_workspace_scope_repair_still_unauthorized` decides whether the aliases
  that violation named are still outside the scope now on disk,
  :func:`_workspace_scope_repair_restored` proves the unauthorized writes match
  their original evidence again without widening scope, and
  :func:`_workspace_scope_repair_comparison` acknowledges the topology a
  registry repair legitimately moved.

Durable records this boundary writes: exactly one.
:func:`_regenerated_repair_child_write_scope` rewrites a materialized repair
child's ``session-<id>.md`` in place to widen its persisted write scope. Nothing
else here opens a path for writing; every other function reads and returns a
payload for its caller to persist, and ``tests/test_asl_workspace_evidence.py``
pins that as a one-entry map rather than a prohibition, so a second writer
appearing in this module is a test failure rather than a review catch.

Nothing here imports the runner. The resolved workspace, the session scope, the
comparison, the plan directory, the event path and the session status all enter
as parameters, and every collaborator this boundary reaches --
``repository_evidence``, ``review_guard``, ``review_repair``, the ``plans_*`` modules,
``workspaces``, ``external_write_evidence``, ``run_errors`` -- is already
runner-free, so each is a direct import at its true owner and no seam is
required. This module is also what finally satisfies the
:class:`review_guard.WorkspaceEvidenceProbe` seam Session 9 declared for
``_repair_commit_preflight``: the runner now passes
:func:`_workspace_write_guard_evidence` into it.

Invariants that survive the move:

* **Unobservable is not innocent, and it is not a breach either.** A repository
  the snapshot could not fingerprint, a path the external-write pass could not
  classify, and a registered repository missing from the snapshot all land in
  ``unavailable``, which fails the guard closed without claiming the guard
  judged and rejected them. ``passed`` requires both lanes empty.
* **A linked worktree answers to its owner's alias.** Another checkout of a
  repository the session may write is not an unregistered repository, so
  :func:`repository_evidence.split_linked_worktree_key` resolves it back to the
  owning alias before the scope test, and adding or removing such a checkout is
  authorized work rather than topology breach.
* **The more specific reason wins a key collision.** A repository the comparison
  itself could not read outranks a path-level verdict about it, and a repository
  that failed to fingerprint outranks one that was never fingerprinted.
* **Every change is qualified by alias.** ``qualified_paths`` are rewritten to
  ``<alias-or-repository>:<path>`` so the drifted paths a preflight subtracts are
  qualified the same way as the retained paths it subtracts them from.
* **Widening only, and only for generated children.** A human-authored session's
  scope is its author's statement of intent and is never rewritten; every
  fail-open branch of :func:`_regenerated_repair_child_write_scope` returns
  ``None`` and leaves the durable file exactly as it was, rather than persisting
  a scope it could not fully derive.
* **A missing review base is an error, not an empty base.** A registered
  repository with no fingerprint raises :class:`RunError` rather than projecting
  a base that would silently exempt it from the review.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import (
    events,
    external_write_evidence,
    plans_errors,
    plans_models,
    plans_session_ids,
    plans_session_scope,
    repository_evidence,
    review_guard,
    review_repair,
    workspaces,
)
from .run_errors import RunError

__all__ = [
    "_annotate_history_transitions",
    "_legacy_write_guard_failure_message",
    "_unavailable_path_and_reason",
    "_workspace_review_bases",
    "_workspace_snapshot_unavailable",
    "_workspace_unavailable_message",
    "_workspace_unavailable_summary",
]


def _annotate_history_transitions(
    comparison: dict[str, Any],
    *,
    repository_roots: Mapping[str, Path],
) -> None:
    """Attach ancestry-backed HEAD telemetry or mark the guard unavailable."""
    raw_changed = comparison.get("changed")
    if not isinstance(raw_changed, Mapping):
        return
    unavailable = comparison.setdefault("unavailable", OrderedDict())
    for repository, raw_changes in raw_changed.items():
        if not isinstance(raw_changes, dict):
            continue
        before_head = raw_changes.get("head_before")
        after_head = raw_changes.get("head_after")
        if not isinstance(before_head, str) or not isinstance(after_head, str):
            unavailable[str(repository)] = {
                "after": {"reason": "history-transition-evidence-missing"}
            }
            continue
        if before_head == after_head:
            # ASI-19: a tip that did not move has no ancestry to probe, so it
            # needs no checkout root. Demanding one marked every unchanged
            # linked worktree unavailable purely because its composite key is
            # absent from the registered-root map -- a fail-closed verdict
            # about evidence that is already conclusive.
            raw_changes["head_transition"] = (
                repository_evidence.unchanged_head_transition(before_head)
            )
            continue
        root = repository_roots.get(str(repository))
        if root is None:
            linked = repository_evidence.split_linked_worktree_key(str(repository))
            if linked is not None:
                owner, worktree = linked
                if owner in repository_roots:
                    root = Path(worktree)
        if root is None:
            unavailable[str(repository)] = {
                "after": {"reason": "repository-scope-unresolved"}
            }
            continue
        try:
            raw_changes["head_transition"] = (
                repository_evidence.classify_head_transition(
                    root,
                    before_head,
                    after_head,
                    _run_git_fn=review_guard._run_git,
                )
            )
        except repository_evidence.RepositoryEvidenceError as error:
            unavailable[str(repository)] = {
                "after": {
                    "reason": "history-transition-unavailable",
                    "error": str(error),
                }
            }


def _workspace_review_bases(
    workspace: workspaces.ResolvedWorkspace,
    snapshot: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project ordered per-repository HEAD and pre-existing dirty paths."""
    bases: list[dict[str, Any]] = []
    for repository in workspace.repositories:
        relative_root = repository.repository.path
        fingerprint = snapshot.get(relative_root)
        state_payload = (
            repository_evidence.repository_state(fingerprint)
            if isinstance(fingerprint, Mapping)
            else None
        )
        if state_payload is None:
            raise RunError(
                f"review base unavailable for registered repository {repository.alias}"
            )
        dirty_paths = [
            f"{repository.alias}:{path}"
            for path in state_payload.get("dirty_paths", ())
        ]
        bases.append(
            {
                "alias": repository.alias,
                "path": relative_root,
                "origin": repository.origin,
                "head": state_payload.get("head"),
                "worktree_dirty": bool(state_payload.get("worktree_dirty")),
                "dirty_paths": dirty_paths,
            }
        )
    return bases


def _workspace_snapshot_unavailable(
    snapshot: Mapping[str, Mapping[str, Any]],
    *,
    required_repository_paths: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    """Return deterministic fail-closed details from a workspace snapshot."""
    unavailable: dict[str, dict[str, Any]] = {}
    for repository in sorted(set(required_repository_paths) - set(snapshot)):
        unavailable[repository] = {"reason": "registered-repository-missing"}
    for repository in sorted(snapshot):
        fingerprint = snapshot[repository]
        detail = repository_evidence.repository_evidence_unavailable(fingerprint)
        if detail is None and not fingerprint.get("git"):
            detail = {"reason": "repository-unavailable"}
        if detail is not None:
            unavailable[repository] = detail
    return unavailable


def _unavailable_path_and_reason(raw: Any) -> tuple[Any, Any]:
    """Read one unavailable row's failing node and reason.

    The top-level fields win: the external-write producer puts ``reason`` at
    the top and nests only a digest (no reason) under ``after``, so reading
    the nested snapshot first misfiles an external path as a repository
    capture failure (ASI-153). Repository rows that nest their detail under
    ``after``/``before`` still resolve through the fallback.
    """
    detail = raw if isinstance(raw, Mapping) else {}
    node = detail.get("path")
    reason = detail.get("reason")
    for phase in ("after", "before"):
        phase_detail = detail.get(phase)
        if isinstance(phase_detail, Mapping):
            if node is None:
                node = phase_detail.get("path")
            if reason is None:
                reason = phase_detail.get("reason")
            break
    return node, reason


def _workspace_unavailable_summary(
    unavailable: Mapping[str, Any],
    *,
    workspace: workspaces.ResolvedWorkspace | None,
) -> str:
    """Name repositories and failing nodes in a bounded pause suffix.

    A legacy run has no resolved workspace, so its rows are named by key.
    """
    aliases = (
        {
            repository.repository.path: repository.alias
            for repository in workspace.repositories
        }
        if workspace is not None
        else {}
    )
    named: list[str] = []
    for repository in sorted(unavailable):
        node, reason = _unavailable_path_and_reason(unavailable[repository])
        label = aliases.get(repository, repository)
        subject = f"{label}:{node}" if node else label
        named.append(f"{subject} ({reason})" if reason else subject)
    if not named:
        return ""
    suffix = f": {', '.join(named[:3])}"
    if len(named) > 3:
        suffix += f" (+{len(named) - 3} more)"
    return suffix


def _workspace_unavailable_message(
    unavailable: Mapping[str, Any],
    *,
    workspace: workspaces.ResolvedWorkspace | None,
) -> str:
    """Explain the guard's blindness without inventing a repository failure."""
    external = False
    repository = False
    for raw in unavailable.values():
        _node, reason = _unavailable_path_and_reason(raw)
        if reason == external_write_evidence.EXTERNAL_WRITE_UNCLASSIFIED_REASON:
            external = True
        else:
            repository = True
    explanations = []
    if external:
        explanations.append("external paths have unresolved write authority")
    if repository:
        explanations.append("repository evidence could not be captured safely")
    return "; ".join(explanations) + _workspace_unavailable_summary(
        unavailable, workspace=workspace
    )


def _legacy_write_guard_failure_message(evidence: Mapping[str, Any]) -> str:
    """Explain a failed legacy write-guard verdict from its recorded evidence.

    The legacy guard fails on unavailable rows (repository capture failures,
    unobserved repositories, or external paths with unresolved write
    authority) and on repository-set violations. Each is named as recorded,
    so an external write is never reported as a repository or history capture
    failure (ASI-153).
    """
    explanations: list[str] = []
    unavailable = evidence.get("unavailable")
    if isinstance(unavailable, Mapping) and unavailable:
        explanations.append(_workspace_unavailable_message(unavailable, workspace=None))
    violations = evidence.get("violations")
    if isinstance(violations, list) and violations:
        named = [
            f"{violation.get('repository')} ({violation.get('kind')})"
            for violation in violations
            if isinstance(violation, Mapping)
        ]
        suffix = f": {', '.join(named[:3])}" if named else ""
        if len(named) > 3:
            suffix += f" (+{len(named) - 3} more)"
        explanations.append(f"repository set changed outside the legacy scope{suffix}")
    if not explanations:
        return (
            "legacy repository scope or history evidence could not be captured safely"
        )
    return "; ".join(explanations)
