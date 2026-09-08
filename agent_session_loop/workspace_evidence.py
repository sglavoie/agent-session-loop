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
  :class:`plans.SessionScope`; :func:`_legacy_implementation_write_guard_evidence`
  projects the single-repository comparison a legacy run produces into the same
  schema, so both modes are read by one enforcement layer.
* **The two bounded pause suffixes.** :func:`_workspace_unavailable_summary` and
  :func:`_workspace_guard_violation_summary` name at most three subjects and a
  count, so a pause reason stays a sentence rather than a payload dump.
* **Scope-repair freshness.** :func:`_session_expansion_evidence` fingerprints an
  expansion file, :func:`_latest_workspace_scope_repair_requirement` recovers the
  latest uncleared scope violation for a session from the event log,
  :func:`_workspace_scope_repair_is_fresh` decides whether the operator actually
  re-expanded since that violation,
  :func:`_workspace_scope_repair_still_unauthorized` decides whether the aliases
  that violation named are still outside the scope now on disk, and
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
``repository_evidence``, ``review_guard``, ``review_repair``, ``plans``,
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
    plans,
    repository_evidence,
    review_guard,
    review_repair,
    workspaces,
)
from .run_errors import RunError


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


def _workspace_unavailable_summary(
    unavailable: Mapping[str, Any],
    *,
    workspace: workspaces.ResolvedWorkspace,
) -> str:
    """Name repositories and failing nodes in a bounded pause suffix."""
    aliases = {
        repository.repository.path: repository.alias
        for repository in workspace.repositories
    }
    named: list[str] = []
    for repository in sorted(unavailable):
        raw_detail = unavailable[repository]
        detail = raw_detail if isinstance(raw_detail, Mapping) else {}
        for phase in ("after", "before"):
            phase_detail = detail.get(phase)
            if isinstance(phase_detail, Mapping):
                detail = phase_detail
                break
        label = aliases.get(repository, repository)
        node = detail.get("path")
        reason = detail.get("reason")
        subject = f"{label}:{node}" if node else label
        named.append(f"{subject} ({reason})" if reason else subject)
    if not named:
        return ""
    suffix = f": {', '.join(named[:3])}"
    if len(named) > 3:
        suffix += f" (+{len(named) - 3} more)"
    return suffix


def _workspace_write_guard_evidence(
    *,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans.SessionScope,
    comparison: Mapping[str, Any],
    exclude: tuple[str, ...],
    include_ignored: bool,
    external_writes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify workspace-vector changes against one session's write scope.

    ``external_writes`` carries the per-path verdict from
    :func:`external_write_evidence._external_write_evidence`. Its unclassified rows join
    ``unavailable`` rather than ``violations`` for the same reason an
    unobserved repository does: the guard has no observation to classify.
    """
    registered = {
        repository.repository.path: repository.alias
        for repository in workspace.repositories
    }
    changed_payload: dict[str, Any] = {}
    violations: list[dict[str, Any]] = []
    authorized: list[str] = []

    raw_changed = comparison.get("changed")
    if isinstance(raw_changed, Mapping):
        for repository in sorted(raw_changed):
            raw_changes = raw_changed[repository]
            changes = dict(raw_changes) if isinstance(raw_changes, Mapping) else {}
            linked = repository_evidence.split_linked_worktree_key(repository)
            if linked is not None:
                changes["worktree_of"] = linked[0]
            # A linked worktree is another checkout of the repository that owns
            # it, so it answers to that repository's alias rather than to none.
            alias = registered.get(repository if linked is None else linked[0])
            qualifier = alias or repository
            qualified_paths = [
                f"{qualifier}:{path.split(':', 1)[1]}"
                if isinstance(path, str) and ":" in path
                else f"{qualifier}:{path}"
                for path in changes.get("qualified_paths", ())
            ]
            changes["qualified_paths"] = qualified_paths
            record = {
                "repository": repository,
                "alias": alias,
                **changes,
            }
            changed_payload[repository] = record
            if alias is not None and alias in scope.writes:
                authorized.append(alias)
                continue
            if alias in scope.reads:
                kind = "read-only-repository"
            else:
                kind = "outside-read-scope"
            violations.append(
                {
                    "kind": kind,
                    "repository": repository,
                    "alias": alias,
                    "qualified_paths": qualified_paths,
                }
            )

    for topology_kind in ("added", "removed"):
        roots = comparison.get(topology_kind)
        if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)):
            continue
        for repository in sorted(str(root) for root in roots):
            linked = repository_evidence.split_linked_worktree_key(repository)
            alias = registered.get(repository if linked is None else linked[0])
            # Adding or removing a checkout of a repository the session may
            # write is work it was authorized to do; the topology change is
            # still recorded, it just is not a breach on its own.
            if linked is not None and alias in scope.writes:
                continue
            violations.append(
                {
                    "kind": f"repository-{topology_kind}",
                    "repository": repository,
                    "alias": alias,
                    "qualified_paths": [],
                }
            )

    unavailable = comparison.get("unavailable")
    unavailable_payload: dict[str, Any] = (
        external_write_evidence._external_write_unavailable(external_writes)
        if external_writes is not None
        else {}
    )
    if isinstance(unavailable, Mapping):
        # A repository the comparison itself could not read has the more
        # specific reason, so it wins a key collision with a path verdict.
        unavailable_payload.update(
            {str(key): value for key, value in unavailable.items()}
        )
    authorized_set = set(authorized)
    return {
        "passed": not violations and not unavailable_payload,
        "external_writes": dict(external_writes) if external_writes is not None else {},
        "read_aliases": list(scope.reads),
        "write_aliases": list(scope.writes),
        "registered_repositories": [
            {
                "alias": repository.alias,
                "path": repository.repository.path,
            }
            for repository in workspace.repositories
        ],
        "changed": changed_payload,
        "authorized_aliases": [
            repository.alias
            for repository in workspace.repositories
            if repository.alias in authorized_set
        ],
        "added": list(comparison.get("added") or ()),
        "removed": list(comparison.get("removed") or ()),
        "unavailable": unavailable_payload,
        "violations": violations,
        "excluded": list(exclude),
        "include_ignored": include_ignored,
    }


def _legacy_implementation_write_guard_evidence(
    *,
    cwd: Path,
    comparison: Mapping[str, Any],
    exclude: tuple[str, ...],
    include_ignored: bool,
    unobserved: Mapping[str, Mapping[str, Any]] = MappingProxyType({}),
    external_writes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a legacy single-repository comparison into the guard schema.

    ``unobserved`` carries the repositories this turn reached that the
    single-repository snapshot never fingerprinted. They join ``unavailable``
    rather than ``violations`` because the guard has no observation to
    classify: it did not see the repository at all, which is the fail-closed
    lane's exact meaning. The comparison's own entries win a key collision --
    a repository that failed to fingerprint has a more specific reason than
    one that was never fingerprinted.

    ``external_writes`` carries the same judgement one level down, over paths
    rather than repositories, and contributes its unclassified rows to the same
    lane.
    """
    raw_changed = comparison.get("changed")
    changed = (
        {str(key): dict(value) for key, value in raw_changed.items()}
        if isinstance(raw_changed, Mapping)
        else {}
    )
    unavailable = comparison.get("unavailable")
    unavailable_payload: dict[str, Any] = {
        str(key): dict(value) for key, value in unobserved.items()
    }
    if external_writes is not None:
        unavailable_payload.update(
            external_write_evidence._external_write_unavailable(external_writes)
        )
    if isinstance(unavailable, Mapping):
        unavailable_payload.update(
            {str(key): value for key, value in unavailable.items()}
        )
    added = list(comparison.get("added") or ())
    removed = list(comparison.get("removed") or ())
    violations = [
        {
            "kind": f"repository-{kind}",
            "repository": str(repository),
            "alias": "legacy",
            "qualified_paths": [],
        }
        for kind, repositories in (("added", added), ("removed", removed))
        for repository in repositories
    ]
    return {
        "passed": not unavailable_payload and not violations,
        "scope_mode": "legacy-single-repository",
        "external_writes": dict(external_writes) if external_writes is not None else {},
        "repository_root": str(cwd),
        "read_aliases": ["legacy"],
        "write_aliases": ["legacy"],
        "registered_repositories": [],
        "changed": changed,
        "authorized_aliases": ["legacy"] if changed else [],
        "added": added,
        "removed": removed,
        "unavailable": unavailable_payload,
        "violations": violations,
        "excluded": list(exclude),
        "include_ignored": include_ignored,
    }


def _workspace_guard_violation_summary(evidence: Mapping[str, Any]) -> str:
    named: list[str] = []
    violations = evidence.get("violations")
    if isinstance(violations, Sequence):
        for violation in violations:
            if not isinstance(violation, Mapping):
                continue
            qualified = violation.get("qualified_paths")
            if isinstance(qualified, Sequence) and not isinstance(
                qualified, (str, bytes)
            ):
                named.extend(str(path) for path in qualified)
            if not qualified:
                alias = violation.get("alias")
                repository = violation.get("repository")
                named.append(str(alias or repository))
    named = list(dict.fromkeys(named))
    if not named:
        return ""
    suffix = f": {', '.join(named[:3])}"
    if len(named) > 3:
        suffix += f" (+{len(named) - 3} more)"
    return suffix


def _session_expansion_evidence(plan_dir: Path, session_id: str) -> dict[str, Any]:
    path = plan_dir / plans.session_id_to_expansion_filename(session_id)
    try:
        stat_result = path.stat()
        content = path.read_bytes()
    except FileNotFoundError:
        return {"path": str(path), "present": False}
    except OSError as error:
        return {
            "path": str(path),
            "present": True,
            "unavailable": str(error),
        }
    return {
        "path": str(path),
        "present": True,
        "mtime_ns": stat_result.st_mtime_ns,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _latest_workspace_scope_repair_requirement(
    event_path: Path, session_id: str
) -> dict[str, Any] | None:
    """Return the latest uncleared retained-mutation record for a session."""
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError:
        # Event history is operational evidence. If it cannot be read, the
        # ordinary readiness and pre-turn evidence gates still apply.
        return None
    accepted_guard_hashes: set[str] = set()
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except TypeError, json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        details = payload.get("details")
        if not isinstance(details, dict):
            continue
        guard = details.get("workspace_write_guard")
        guard_session_id = guard.get("session_id") if isinstance(guard, dict) else None
        detail_session_id = details.get("session_id")
        detail_session = details.get("session")
        if (
            guard_session_id != session_id
            and detail_session_id != session_id
            and str(detail_session) != session_id
        ):
            continue
        if record.get("name") == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT:
            return None
        if record.get("name") == events.WORKSPACE_REF_UPDATES_ACCEPTED_EVENT:
            acceptance = details.get("acceptance")
            if (
                isinstance(acceptance, dict)
                and acceptance.get("session_id") == session_id
                and isinstance(acceptance.get("retained_guard_sha256"), str)
                and isinstance(payload.get("reason"), str)
                and payload["reason"].strip()
            ):
                accepted_guard_hashes.add(acceptance["retained_guard_sha256"])
            continue
        if (
            record.get("name") == events.RUN_PAUSED_EVENT
            and payload.get("reason")
            == events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
        ):
            if isinstance(guard, dict):
                guard_hash = hashlib.sha256(
                    json.dumps(guard, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                return None if guard_hash in accepted_guard_hashes else dict(guard)
            return None
    return None


def _workspace_scope_repair_still_unauthorized(
    record: Mapping[str, Any],
    scope: plans.SessionScope | None,
) -> bool:
    """Whether a retained scope violation is still outside the session's scope.

    The retained record freezes the aliases the session held when the guard
    rejected the turn, and the pause it raises tells the operator to widen the
    durable scope. Re-reading that record alone can therefore never observe
    that they did (ASI-134): the violating aliases are still listed exactly as
    they were, so a run-start refusal built on it refuses again on evidence
    that describes only the past -- and it does so while the retained changes
    are what keep the readiness gate red, which is a deadlock rather than a
    diagnosis.

    Reconciliation is judged per violation against the scope on disk now. A
    violation naming an alias the session may now write is answered here, and
    only far enough to let the run reach the turn-level repair check, which
    re-derives the whole verdict from freshly captured evidence and still
    pauses when the retained changes really are out of scope.

    Everything else keeps the refusal: a scope that could not be read at all,
    an unregistered repository, which no scope edit can authorize; a record
    whose observation was incomplete; and a record carrying no violations to
    judge at all.
    """
    if scope is None:
        return True
    if record.get("unavailable"):
        return True
    raw_violations = record.get("violations")
    if not isinstance(raw_violations, Sequence) or isinstance(
        raw_violations, (str, bytes)
    ):
        return True
    violations = [entry for entry in raw_violations if isinstance(entry, Mapping)]
    if not violations:
        return True
    writable = frozenset(scope.writes)
    return any(
        not isinstance(entry.get("alias"), str) or entry["alias"] not in writable
        for entry in violations
    )


def _workspace_scope_repair_is_fresh(
    prior: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    if current.get("present") is not True or current.get("unavailable"):
        return False
    if prior.get("present") is not True:
        return True
    prior_mtime = prior.get("mtime_ns")
    current_mtime = current.get("mtime_ns")
    return (
        isinstance(prior_mtime, int)
        and isinstance(current_mtime, int)
        and current_mtime > prior_mtime
    )


def _workspace_scope_repair_comparison(
    comparison: Mapping[str, Any],
    registered_paths: frozenset[str],
) -> tuple[dict[str, Any], list[str]]:
    """Acknowledge topology moved by a repair that changed the registry.

    A retained-scope repair compares the original pre-turn snapshot with a
    newly captured baseline. Both snapshots cover only the repositories the
    plan registered when each was taken, so the sanctioned repair of
    registering the repository that was written to makes that repository
    appear from nothing, and deregistering one makes it vanish. Reading either
    as real topology change would make a registry repair permanently
    unpassable.

    A repository that is registered now cannot have been added by the harness
    and a repository that is not registered now is outside the guard's domain,
    so neither suppression can hide topology change to an in-scope repository.
    """
    repaired = dict(comparison)
    suppressed: list[str] = []
    for kind, suppress_registered in (("added", True), ("removed", False)):
        raw_roots = comparison.get(kind)
        if not isinstance(raw_roots, Sequence) or isinstance(raw_roots, (str, bytes)):
            continue
        retained: list[str] = []
        for raw_repository in raw_roots:
            repository = str(raw_repository)
            if (repository in registered_paths) == suppress_registered:
                suppressed.append(repository)
            else:
                retained.append(repository)
        repaired[kind] = retained
    return repaired, suppressed


def _regenerated_repair_child_write_scope(
    *,
    session: plans.SessionStatus,
    plan_dir: Path,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans.SessionScope,
) -> dict[str, Any] | None:
    """Widen a materialized repair child's persisted write scope in place.

    Authoring already derives a child's write scope from the union its own
    findings attribute, so a materialized child can only be too narrow when the
    findings were recomputed — by a re-expansion or a reopen — after the file
    was written. Recomputing from that same union rewrites the durable file
    rather than the value in hand, so the file stays the single description of
    what the child may write and `run_prompts._session_scope` remains its only
    reader.

    Widening only, and only for ASL-generated children. A human-authored
    session's scope is its author's statement of intent and is never rewritten;
    neither is an alias the current findings no longer require. ``None`` means
    the file was left exactly as it was, which every fail-open branch here
    returns rather than persisting a scope it could not fully derive.
    """
    if not review_repair.is_generated_review_repair_session_file(session.path):
        return None
    try:
        text = session.path.read_text(encoding="utf-8")
    except OSError:
        return None
    group = review_repair.repair_group_from_session_text(text)
    if group is None:
        return None
    planning = review_repair.plan_review_repair_sessions(
        plan_dir,
        # Children hang beneath their review's top-level id, which is how
        # `review_repair` itself recovers a review's existing children.
        review_session_id=session.session_id.components[0],
    )
    grouped = dict(
        review_repair.group_findings_by_repair_group(list(planning.validated_findings))
    )
    group_findings = grouped.get(group)
    if not group_findings:
        # A group the current artifact no longer carries has no derivable
        # scope; leaving the child untouched keeps the artifact's own
        # diagnosis rather than replacing it with a narrower one.
        return None
    try:
        required = review_repair.required_write_repositories(
            group,
            group_findings,
            repository_registry=workspace.registry,
        )
    except review_repair.ReviewRepairError:
        # Findings this malformed fail closed where they are validated.
        return None
    added = tuple(alias for alias in required if alias not in scope.writes)
    if not added:
        return None
    aliases = workspace.registry.aliases
    reads = frozenset(scope.reads) | frozenset(required)
    writes = frozenset(scope.writes) | frozenset(required)
    try:
        widened = plans.SessionScope(
            reads=tuple(alias for alias in aliases if alias in reads),
            # Writes must stay a subset of reads, so a child whose reads were
            # narrowed by hand widens both rather than persisting an
            # unparseable scope.
            writes=tuple(alias for alias in aliases if alias in writes),
        ).validate_registry(aliases)
        rewritten = plans.replaced_session_scope_text(text, widened)
        plans.parse_session_scope(rewritten, aliases)
    except plans.PlanError, ValueError:
        return None
    try:
        session.path.write_text(rewritten, encoding="utf-8")
    except OSError:
        # A child the runner cannot rewrite keeps its narrow durable scope, and
        # the caller's revalidation is what turns that into a refusal to launch
        # rather than a crash on the way to one.
        return None
    return {
        "session_id": session.id,
        "repair_group": group,
        "durable_write_repositories": list(scope.writes),
        "required_write_repositories": list(required),
        "added_write_repositories": list(added),
        "write_repositories": list(widened.writes),
        "read_repositories": list(widened.reads),
    }
