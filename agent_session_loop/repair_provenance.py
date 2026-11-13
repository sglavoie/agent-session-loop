"""What a run can attest as its own when a repair child's preflight finds dirt.

``review_guard._repair_commit_preflight`` refuses any dirty path a generated
repair child did not author, before the harness starts, because a legacy
child's commit is repository-wide. Two in-run transitions used to meet that
refusal with dirt the run had itself verifiably produced:

* **Review to repair (ASI-170).** With the plan directory tracked inside the
  repository, the review turn and the repair materialization that follows it
  leave the review output and artifact, the review session file, the progress
  journal, the manifest and every generated child dirty. Nothing commits after
  a review turn, so the first child's preflight refused the plan artifacts that
  created it.
* **Continuing an unfinished child (ASI-168).** A child turn that ends without
  completing -- a runner question the loop absorbs, a retried nonzero exit, a
  verification repair cycle -- keeps its work in the worktree and the loop
  continues the same child. The preflight ran again on that relaunch and
  refused the child's own retained work.

This ledger records, at the one moment each transition can be attested, a
digest of every dirty path the run can vouch for, and hands the preflight that
record so it can sanction a path whose content is still exactly what was
recorded:

* :meth:`RepairProvenance.record_review_cycle` runs once a review cycle has
  materialized its repair children and admits only paths inside the plan
  directory. The review turn's mutation guard already proved nothing outside
  that directory moved, and the plan artifacts were written by the review turn
  and by the runner, in that order, with nothing in between.
* :meth:`RepairProvenance.record_continuation` runs at the loop's one arming
  site, after every post-turn write guard cleared a repair child's turn and
  before any wait the turn's disposition enters. It admits the paths that
  turn's (cumulative) write guard observed it changing, the paths the child's
  previous record already vouched for, and plan-directory paths. The turn was
  admitted by a preflight, so every path dirty before it was already clean or
  sanctioned, and the guard observed everything that changed during it.

A dirty path the record does not admit is never recorded, so it still refuses.
A recorded path that is edited afterwards -- during the reply wait, say --
no longer matches its digest, so it refuses too.

The ledger is run-scoped and in-memory, mirrored into the run payload for audit
rather than read back from it, for the reason ``continuation_guards`` gives: a
new run over the same plan meets its own startup commit gate, and rehydrating a
path list across runs would let a stale record sanction a tree nothing in the
new process had observed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import plans_session_scope, review_guard_provenance, verification_models
from .run_errors import RunError

#: Where the run payload mirrors the ledger for audit.
REPAIR_PROVENANCE_PAYLOAD_KEY = "repair_provenance"

SOURCE_REVIEW_CYCLE = "review-cycle"
SOURCE_REPAIR_CONTINUATION = "repair-continuation"

#: The per-repository change categories of a write-guard record that name paths
#: the turn wrote. The ignore-source lanes are left out on purpose: they name
#: exclusion files rather than content a commit stages.
_GUARD_PATH_CATEGORIES: tuple[str, ...] = (
    "tracked",
    "untracked_added",
    "untracked_changed",
    "untracked_removed",
)


def _qualify(alias: str | None, path: str) -> str:
    """Name a path the way the repair-child preflight names it."""
    return f"{alias}:{path}" if alias is not None else path


def _repositories(
    cwd: Path,
    verification_context: verification_models.VerificationContext,
) -> list[tuple[str | None, Path]]:
    workspace = verification_context.workspace
    if workspace is None:
        return [(None, cwd)]
    return [
        (repository.alias, repository.root) for repository in workspace.repositories
    ]


def _plan_prefix(toplevel: Path, plan_dir: Path) -> str | None:
    """The plan directory relative to ``toplevel``, or ``None`` when outside it."""
    try:
        relative = plan_dir.resolve().relative_to(toplevel.resolve())
    except OSError, ValueError:
        return None
    text = relative.as_posix()
    return "" if text == "." else text


def _inside(path: str, prefix: str | None) -> bool:
    if prefix is None:
        return False
    if prefix == "":
        return True
    return path == prefix or path.startswith(prefix + "/")


def capture_attested_digests(
    repositories: Sequence[tuple[str | None, Path]],
    *,
    plan_dir: Path,
    admit: Callable[[str, bool], bool],
) -> tuple[dict[str, str], list[str]]:
    """Digest every dirty path ``admit`` vouches for, repository by repository.

    ``admit`` receives the qualified path and whether it lies inside the plan
    directory. A repository that cannot be measured records nothing and is
    named in the returned errors, so its dirt stays refused.
    """
    digests: dict[str, str] = {}
    errors: list[str] = []
    for alias, root in repositories:
        try:
            evidence = review_guard_provenance._dirty_path_evidence(root)
        except RunError as error:
            errors.append(f"{alias or '.'}: {error}")
            continue
        prefix = _plan_prefix(evidence.toplevel, plan_dir)
        for path in evidence.statuses:
            qualified = _qualify(alias, path)
            if not admit(qualified, _inside(path, prefix)):
                continue
            digest = evidence.digest(path)
            if digest is not None:
                digests[qualified] = digest
    return digests, errors


def guard_written_paths(
    guard: Mapping[str, Any] | None,
    *,
    registered: bool,
) -> frozenset[str] | None:
    """Qualified paths a passing write-guard record observed its turn changing.

    ``None`` when the record cannot vouch for anything: it is missing, it did
    not pass, it convicted or failed to observe something, or it is malformed.
    A linked worktree's paths are relative to that checkout rather than to the
    repository the preflight inspects, so they are left out rather than guessed
    at, and stay refused.
    """
    if not isinstance(guard, Mapping) or guard.get("passed") is not True:
        return None
    if guard.get("violations") or guard.get("unavailable"):
        return None
    changed = guard.get("changed")
    if not isinstance(changed, Mapping):
        return None
    paths: set[str] = set()
    for entry in changed.values():
        if not isinstance(entry, Mapping) or entry.get("worktree_of") is not None:
            continue
        alias = entry.get("alias") if registered else None
        if registered and not isinstance(alias, str):
            continue
        for category in _GUARD_PATH_CATEGORIES:
            values = entry.get(category)
            if isinstance(values, list):
                paths.update(_qualify(alias, str(value)) for value in values)
    return frozenset(paths)


class RepairProvenance:
    """The run's attested dirty-path digests, for repair-child preflight.

    Two records are kept: the control-plane record the latest review cycle left,
    which any repair child may read, and one continuation record per child,
    which only that child may read. A child's view merges both, its own record
    winning a path both name because it is the newer observation.
    """

    def __init__(self) -> None:
        self._control_plane: dict[str, str] = {}
        self._continuations: dict[str, dict[str, str]] = {}
        self._errors: dict[str, list[str]] = {}

    def baseline_for(self, session_id: str) -> dict[str, str]:
        """The digests the preflight of repair child ``session_id`` may sanction."""
        merged = dict(self._control_plane)
        merged.update(self._continuations.get(session_id, {}))
        return merged

    def record_review_cycle(
        self,
        *,
        cwd: Path,
        plan_dir: Path,
        verification_context: verification_models.VerificationContext,
    ) -> None:
        """Attest the plan artifacts a review cycle left after materializing repairs."""
        digests, errors = capture_attested_digests(
            _repositories(cwd, verification_context),
            plan_dir=plan_dir,
            admit=lambda _qualified, in_plan: in_plan,
        )
        self._control_plane = digests
        self._set_errors(SOURCE_REVIEW_CYCLE, errors)

    def record_continuation(
        self,
        *,
        session_id: str,
        cwd: Path,
        plan_dir: Path,
        verification_context: verification_models.VerificationContext,
        session_scope: plans_session_scope.SessionScope | None,
        guard: Mapping[str, Any] | None,
    ) -> None:
        """Attest what a repair child's cleared turn left behind for its next turn.

        A guard record that cannot vouch for its turn drops the child's record
        altogether, so nothing that turn left is carried into a relaunch.
        """
        registered = verification_context.workspace is not None
        written = guard_written_paths(guard, registered=registered)
        if written is None:
            self._continuations.pop(session_id, None)
            self._set_errors(f"{SOURCE_REPAIR_CONTINUATION}:{session_id}", [])
            return
        vouched = set(self.baseline_for(session_id)) | written
        repositories = _repositories(cwd, verification_context)
        if registered:
            writable = set(session_scope.writes) if session_scope is not None else set()
            repositories = [
                (alias, root) for alias, root in repositories if alias in writable
            ]
        digests, errors = capture_attested_digests(
            repositories,
            plan_dir=plan_dir,
            admit=lambda qualified, in_plan: in_plan or qualified in vouched,
        )
        self._continuations[session_id] = digests
        self._set_errors(f"{SOURCE_REPAIR_CONTINUATION}:{session_id}", errors)

    def retire(self, session_id: str) -> None:
        """Drop a completed child's record; its work is committed."""
        self._continuations.pop(session_id, None)
        self._errors.pop(f"{SOURCE_REPAIR_CONTINUATION}:{session_id}", None)

    def _set_errors(self, key: str, errors: Iterable[str]) -> None:
        listed = list(errors)
        if listed:
            self._errors[key] = listed
        else:
            self._errors.pop(key, None)

    def to_payload(self) -> dict[str, Any]:
        """Render the ledger as the plain JSON the run state record carries."""
        payload: dict[str, Any] = {
            SOURCE_REVIEW_CYCLE: dict(sorted(self._control_plane.items())),
            SOURCE_REPAIR_CONTINUATION: {
                session_id: dict(sorted(digests.items()))
                for session_id, digests in sorted(self._continuations.items())
            },
        }
        if self._errors:
            payload["errors"] = {
                key: list(value) for key, value in self._errors.items()
            }
        return payload
