"""Focused implementation extracted from :mod:`.supervisor_audit`."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    assurance_models_artifacts,
    assurance_models_primitives,
    harness_failures,
    harnesses,
    repository_evidence,
    supervisor_audit_vocabulary,
)
from . import supervisor_audit_evidence_commits as audit_evidence_commits
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .assurance_models_summary import AuditRole
from .assurance_models_vocab import (
    AttemptOutcome,
    AuditState,
)
from .supervisor_audit_agent_models import (
    _availability_reason,
    _harness_tail,
    _redacted_classification,
)
from .supervisor_audit_config import (
    REASON_INTERRUPTED,
    REASON_TIMEOUT,
    REASON_WORKSPACE_CHANGED,
    REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
    AuditCancelled,
    audit_read_roots,
    audit_workdir,
)


@dataclass(frozen=True)
class AuditAttemptResult:
    """What one restricted audit attempt produced, before grading its content."""

    outcome: AttemptOutcome
    state: AuditState
    reason: str | None = None
    argv: tuple[str, ...] = ()
    returncode: int | None = None
    last_message: str = ""
    workspace_change: dict[str, Any] | None = None
    classification: harness_failures.FailureClassification | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


_MUTATION_CHANGE_KEYS = (
    "tracked",
    "untracked_added",
    "untracked_removed",
    "untracked_changed",
    "worktree_ignore",
    "ignore_config",
    "refs_added",
    "refs_removed",
    "refs_changed",
)


def _without_remote_tracking_refs(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Drop remote-tracking ref moves from the workspace consistency check.

    These cache refs can move whenever another checkout fetches, without
    changing a file, local branch, or commit in the audited workspace (ASI-13).
    Local branches and tags still invalidate the audit when they change.
    """
    filtered = dict(changes)
    for key in ("refs_added", "refs_removed", "refs_changed"):
        entries = audit_evidence_helpers._sequence(changes.get(key))
        if not entries:
            continue
        filtered[key] = [
            entry
            for entry in entries
            if not str(
                audit_evidence_helpers._mapping(entry).get("ref") or ""
            ).startswith(audit_evidence_commits._REMOTE_TRACKING_REF_PREFIX)
        ]
    return filtered


def _mutation_comparison(comparison: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the comparison when it proves a change, else ``None``.

    Unavailable repository evidence is not a mutation: it is the absence of
    proof either way, and treating it as one would turn every transient git
    failure into a fabricated integrity issue. A repository whose only remaining
    difference is a remote-tracking ref is dropped for the same reason -- see
    :func:`_without_remote_tracking_refs` -- and a comparison left with nothing
    else proves no mutation at all.
    """
    changed = {
        repository: filtered
        for repository, raw in audit_evidence_helpers._mapping(
            comparison.get("changed")
        ).items()
        for filtered in (
            _without_remote_tracking_refs(audit_evidence_helpers._mapping(raw)),
        )
        if filtered.get("head_changed") is True
        or any(filtered.get(key) for key in _MUTATION_CHANGE_KEYS)
    }
    added = comparison.get("added") or []
    removed = comparison.get("removed") or []
    if not (changed or added or removed):
        return None
    return {
        "changed": changed,
        "added": list(added),
        "removed": list(removed),
        "unavailable": dict(comparison.get("unavailable") or {}),
    }


def _evidence_unprobeable(fingerprint: Mapping[str, Any] | None) -> bool:
    """Report whether one snapshot entry carries no comparable content evidence."""
    if not fingerprint:
        return True
    return (
        not fingerprint.get("git")
        or repository_evidence.repository_evidence_unavailable(fingerprint) is not None
    )


def _snapshot_omits_registered(
    snapshot: Mapping[str, Mapping[str, Any]],
    repositories: Sequence[str],
) -> bool:
    """Report whether the snapshot has no entry for some registered repository.

    ``capture_registered_snapshot`` omits a declared path that is missing, is
    not a directory, or no longer names the node it declares. An omitted path
    contributes nothing to the comparison, so a repository omitted from both
    sides — a checkout that was already a symlink when the audit started — would
    otherwise compare as silently unchanged.
    """
    return any(relative not in snapshot for relative in repositories)


def _registered_evidence_unavailable(
    snapshot: Mapping[str, Mapping[str, Any]],
    repositories: Sequence[str],
) -> bool:
    """Report whether some registered repository is present but unfingerprintable.

    ``capture_registered_snapshot`` records fail-closed ``repository-disappeared``
    evidence for a declared directory that is not its own Git worktree root, so a
    checkout swapped for a plain directory keeps its key while carrying nothing
    the comparison can read.
    """
    return any(
        _evidence_unprobeable(snapshot.get(relative)) for relative in repositories
    )


def _observability_changed(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    repositories: Sequence[str],
) -> bool:
    """Report whether any registered repository stopped or started being probeable.

    A repository the audit could fingerprint before the turn and not after it
    (or the reverse) has no comparable evidence, which is silence rather than
    proof of an intact boundary.
    """
    return any(
        _evidence_unprobeable(before.get(relative))
        != _evidence_unprobeable(after.get(relative))
        for relative in repositories
    )


def _record_workspace_change(
    comparison: Mapping[str, Any],
    *,
    role: AuditRole,
    run_dir: Path,
    run_id: str,
    observed_at: str,
) -> None:
    """Record observed activity without inventing a writer's identity.

    Snapshots prove a change, not who caused it. Recording is best effort:
    losing this artifact cannot turn the already-unavailable audit into a
    usable verdict. The attempt archive also retains the comparison.
    """
    with contextlib.suppress(OSError, assurance_models_primitives.AssuranceError):
        assurance_models_artifacts.write_audit_json(
            run_dir,
            supervisor_audit_vocabulary.WORKSPACE_CHANGE_ARTIFACT_NAME,
            {
                "run_id": run_id,
                "observed_at": observed_at,
                "role": role.to_dict(),
                "attribution": {
                    "status": "unattributed",
                    "reason": "snapshot-comparison-has-no-writer-identity",
                },
                "comparison": comparison,
            },
        )


def run_restricted_audit(
    role: AuditRole,
    *,
    run_dir: Path | str,
    run_id: str,
    workspace_root: Path | str,
    plan_dir: Path | str,
    prompt: str,
    repositories: Sequence[str],
    registry_declared: bool = True,
    state_dir: Path | str | None = None,
    timeout: int | None = None,
    stream_callback: harnesses.StreamCallback | None = None,
    abort: harnesses.AbortSignal | None = None,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
) -> AuditAttemptResult:
    """Run read-only and reject a verdict over a changing live workspace.

    ``repositories`` holds the workspace-relative paths the plan registered,
    and they alone are fingerprinted on both sides of the turn. Any change to
    one of them makes the audit unavailable regardless of what the auditor
    reported. Snapshots establish activity, never a writer's identity: an
    auditor escaping its profile and an unrelated concurrent edit produce the
    same evidence, so neither is accepted as a stable audit or charged to the
    auditor as a critical breach. A registered repository the snapshot cannot
    name at all, one whose evidence is already
    unavailable before the turn, or one whose evidence stops (or starts) being
    probeable across the turn, makes the audit unavailable for the same reason:
    silence about a repository is not proof that it was left alone, and no
    verdict may be accepted over it. Worktrees the plan never registered are
    outside that boundary: concurrent runs write to them routinely, so reading
    their state as this auditor's doing would report a breach nobody committed.

    ``registry_declared`` is the caller's statement that ``repositories`` came
    from a plan registry. A run whose plan registered nothing is audited over a
    synthesized run root instead, which is routinely not a worktree root and
    therefore carries permanently unavailable evidence; passing ``False`` keeps
    that compatibility case auditable without extending the same tolerance to a
    repository a plan really registered.

    The comparison always covers the live workspace, including linked worktrees.
    A private pin the auditor cannot reach observes no writer and would miss
    escaped writes to the live tree, even when the run recorded commits.

    ``abort`` lets a supervisor cancel the attempt mid-turn; the cancel is
    raised as :class:`AuditCancelled` rather than returned, so no caller can
    mistake it for a failed attempt worth another try.
    """
    run_dir = Path(run_dir)
    unavailable = harnesses.audit_read_only_unavailable_reason(role.harness)
    if unavailable is not None:
        return AuditAttemptResult(
            outcome="unavailable", state="unavailable", reason=unavailable
        )

    def snapshot() -> repository_evidence.WorkspaceSnapshot:
        return repository_evidence.capture_registered_snapshot(
            Path(workspace_root),
            plan_dir,
            tuple(repositories),
            include_linked_worktrees=True,
        )

    try:
        before = snapshot()
    except repository_evidence.RepositoryEvidenceError:
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
        )
    if _snapshot_omits_registered(before, repositories) or (
        registry_declared and _registered_evidence_unavailable(before, repositories)
    ):
        # Nothing is launched: a registered path the snapshot cannot name, or
        # one it can name but not fingerprint, contributes the same evidence to
        # both sides of the comparison, so the boundary check would pass over it
        # no matter what the auditor did.
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
        )

    workdir = audit_workdir(run_dir)
    last_message_path = workdir / "last-message.txt"
    run_harness = harnesses.run_harness if _run_harness_fn is None else _run_harness_fn
    try:
        result = run_harness(
            role.harness,
            cwd=workdir,
            plan_dir=None,
            prompt=prompt,
            unsafe_bypass=False,
            last_message_path=last_message_path,
            timeout=int(role.timeout_seconds) if timeout is None else timeout,
            stream_callback=stream_callback,
            model=role.model,
            effort=role.effort,
            access=harnesses.audit_read_only_access(audit_read_roots(run_dir)),
            abort=abort,
        )
    except harnesses.HarnessError as error:
        return AuditAttemptResult(
            outcome="unavailable", state="unavailable", reason=str(error)
        )

    argv = tuple(result.argv)
    try:
        after = snapshot()
    except repository_evidence.RepositoryEvidenceError:
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )

    change = _mutation_comparison(
        repository_evidence.compare_workspace_snapshots(before, after)
    )
    if change is not None:
        change["workspace"] = {"root": str(Path(workspace_root))}
        clock = datetime.now(UTC) if now is None else now()
        _record_workspace_change(
            change,
            role=role,
            run_dir=run_dir,
            run_id=run_id,
            observed_at=clock.isoformat(),
        )
        return AuditAttemptResult(
            outcome="workspace-changed",
            state="unavailable",
            reason=REASON_WORKSPACE_CHANGED,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
            workspace_change=change,
        )

    if _observability_changed(before, after, repositories):
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )

    if result.aborted:
        # After the consistency check, so workspace activity during a cancelled
        # attempt is still reported without attribution; before every failure branch,
        # so the kill is never read as a transport error worth retrying.
        raise AuditCancelled(REASON_INTERRUPTED)
    if result.interrupted:
        # Ahead of the exit code, because the harness reports an interrupt as a
        # nonzero return: reading it as a transport failure would answer the
        # operator's first Ctrl+C with a second full audit turn.
        return AuditAttemptResult(
            outcome="interrupted",
            state="unavailable",
            reason=REASON_INTERRUPTED,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )
    if result.timed_out:
        return AuditAttemptResult(
            outcome="timeout",
            state="unavailable",
            reason=REASON_TIMEOUT,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )
    if result.returncode != 0:
        # A bare "harness-failed" is unreadable a week later, so the launch is
        # classified with the same rules the session loop uses and the streams
        # that carried the provider's own words are kept alongside it. A few of
        # those classifications name a condition that is not a failure of the
        # audit at all, and those keep an availability class of their own rather
        # than being filed as one -- see `_DISTINCT_AVAILABILITY_REASONS`.
        classification = _redacted_classification(result)
        return AuditAttemptResult(
            outcome="transport-error",
            state="unavailable",
            reason=_availability_reason(classification),
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
            classification=classification,
            stdout_tail=_harness_tail(result.stdout),
            stderr_tail=_harness_tail(result.stderr),
        )
    return AuditAttemptResult(
        outcome="ok",
        state="pending",
        argv=argv,
        returncode=result.returncode,
        last_message=result.last_message,
    )
