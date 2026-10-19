"""Attribute repository changes to a stopped run and read its verification."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    commit_message,
    overseer_context,
    overseer_discovery,
    overseer_policy_client,
    state,
)
from .overseer_checkpoint_git import dirty_paths
from .overseer_checkpoint_models import (
    _CHANGED_KEY,
    _WRITE_GUARD_KEY,
    LEGACY_REPOSITORY_KEY,
    Activity,
    Attribution,
    Refusal,
    Verification,
    VerificationState,
)


def declared_repositories(
    plan_dir: str,
    *,
    state_dir: str | Path | None = None,
) -> tuple[overseer_policy_client.Repository, ...]:
    """The repositories a plan's own record declares, resolved on this host.

    Read from discovery rather than from a repository the caller names, because
    the case this exists for is reconciliation: the ledger row carries a plan
    directory and nothing about where that plan's code lives.
    """
    inventory = overseer_discovery.build_inventory(state_dir=state_dir)
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return overseer_policy_client.repositories_for(candidate.repositories)
    return ()


def read_activity(
    *,
    plan_dir: str,
    run_id: str,
    alias: str,
    state_dir: str | Path | None = None,
) -> Activity:
    """Read one run's own account of what it did to one repository.

    Both artifact families are read because they record the same fact at
    different moments: a ``commit-`` record names the head the run's last commit
    produced, and a ``turn-`` record's write-guard evidence names the head a turn
    ran against together with the paths that turn changed. Oldest to newest, so
    the last head seen is the newest one recorded.
    """
    baseline = ""
    paths: list[str] = []
    records = 0
    for record in _run_records(plan_dir=plan_dir, run_id=run_id, state_dir=state_dir):
        head, changed = _repository_evidence(record, alias)
        if head == "" and not changed:
            continue
        records += 1
        if head:
            baseline = head
        paths.extend(changed)
    return Activity(
        alias=alias,
        baseline=baseline,
        paths=tuple(dict.fromkeys(paths)),
        records=records,
    )


def read_verification(
    *,
    plan_dir: str,
    run_id: str,
    state_dir: str | Path | None = None,
) -> Verification:
    """Read the run's newest recorded verification, without running anything.

    Newest-recorded rather than newest-turn: a turn that paused before it
    verified writes no verification block, and reading that absence as "no
    verification" would describe the artifact layout rather than the run.
    """
    newest: Mapping[str, Any] | None = None
    for record in _run_records(plan_dir=plan_dir, run_id=run_id, state_dir=state_dir):
        block = record.get("verification")
        if isinstance(block, Mapping):
            newest = block
    if newest is None:
        return Verification(state="unrecorded")
    passed = newest.get("passed")
    state_name: VerificationState = "unresolved"
    if passed is True:
        state_name = "passed"
    elif passed is False:
        state_name = "failed"
    return Verification(
        state=state_name,
        session=_text(newest.get("session")),
        reason=_text(newest.get("reason")),
        failures=_names(newest.get("failures")),
    )


def attribute(
    *,
    plan_dir: str,
    run_id: str,
    repositories: Sequence[overseer_policy_client.Repository],
    state_dir: str | Path | None = None,
) -> Attribution:
    """Decide whether the changes in the declared repository belong to this run.

    The order of the questions is the order they can be answered in: is there
    exactly one repository, can it be read, is there anything to commit at all,
    does the run's evidence establish a baseline, is HEAD still on it, and only
    then is every dirty path accounted for. A clean repository is answered
    before ownership because a checkpoint of nothing cannot be a checkpoint of
    somebody else's work.
    """
    if len(repositories) != 1:
        return Attribution(
            alias="",
            root="",
            baseline="",
            head="",
            refusal="repository_ambiguous",
            detail=(
                f"a checkpoint isolates one repository; this action names "
                f"{len(repositories)}"
            ),
        )
    repository = repositories[0]
    root = Path(repository.path)
    head = commit_message.head_sha(root)
    dirty = dirty_paths(root)
    if not head or dirty is None:
        return Attribution(
            alias=repository.alias,
            root=repository.path,
            baseline="",
            head=head,
            refusal="repository_unreadable",
            detail=f"{repository.path} could not be read as a git worktree",
        )
    activity = read_activity(
        plan_dir=plan_dir,
        run_id=run_id,
        alias=repository.alias,
        state_dir=state_dir,
    )
    partial = Attribution(
        alias=repository.alias,
        root=repository.path,
        baseline=activity.baseline,
        head=head,
        dirty=dirty,
        activity=activity.paths,
    )
    if not dirty:
        return _refuse(partial, "nothing_to_commit", "the worktree is already clean")
    if not activity.observed:
        return _refuse(
            partial,
            "baseline_unknown",
            "this run's records name no head to measure the worktree from",
        )
    if activity.baseline != head:
        return _refuse(
            partial,
            "baseline_moved",
            f"the run recorded {activity.baseline} but HEAD is {head}",
        )
    owned = tuple(path for path in dirty if path in set(activity.paths))
    unattributed = tuple(path for path in dirty if path not in set(activity.paths))
    accounted = Attribution(
        alias=repository.alias,
        root=repository.path,
        baseline=activity.baseline,
        head=head,
        dirty=dirty,
        owned=owned,
        unattributed=unattributed,
        activity=activity.paths,
    )
    if unattributed:
        return _refuse(
            accounted,
            "changes_unattributed",
            "this run's records do not account for "
            + ", ".join(sorted(unattributed)[:_NAMED_LIMIT]),
        )
    return accounted


# -- internals -------------------------------------------------------------

#: How many paths a refusal names before it stops. A refusal is a sentence an
#: operator reads, not the change set it is about.
_NAMED_LIMIT = 5


def _run_records(
    *,
    plan_dir: str,
    run_id: str,
    state_dir: str | Path | None,
) -> tuple[dict[str, Any], ...]:
    """Every artifact record this run published, oldest first.

    Sorted by directory name, which orders them by the zero-padded index the
    runner writes, so "newest" needs no timestamps and no ``stat`` call.
    """
    run_dir = state.plan_state_dir(plan_dir, state_dir) / state.RUNS_DIR_NAME / run_id
    try:
        entries = sorted(
            (entry for entry in run_dir.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
    except OSError:
        return ()
    prefixes = (overseer_context.TURN_PREFIX, overseer_context.COMMIT_PREFIX)
    records: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.name.startswith(prefixes):
            continue
        record = _read_record(entry / state.RESULT_FILE_NAME)
        if record is not None:
            records.append(record)
    return tuple(records)


def _read_record(path: Path) -> dict[str, Any] | None:
    """One JSON object a run published, or ``None`` when it cannot be read.

    Unreadable and absent are the same answer here on purpose: both mean this
    artifact contributes no evidence, and neither may contribute a *default*.
    An artifact that cannot be read simply does not attribute anything, which
    leaves the checkpoint refusing for want of evidence rather than proceeding
    on an assumption.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _repository_evidence(
    record: Mapping[str, Any], alias: str
) -> tuple[str, tuple[str, ...]]:
    """One record's head and changed paths for one repository, or nothing.

    Two shapes because the run writes two: a commit record names the repository
    it committed in at the top level, and a turn record names every repository
    its write guard compared inside the guard's ``changed`` map.
    """
    if "commit_index" in record:
        if not _names_repository(record.get("repository_alias"), alias):
            return ("", ())
        head = _text(record.get("head_after")) or _text(record.get("head_before"))
        return (head, _relative_paths(record.get("qualified_paths"), alias))
    guard = record.get(_WRITE_GUARD_KEY)
    if not isinstance(guard, Mapping):
        return ("", ())
    changed = guard.get(_CHANGED_KEY)
    if not isinstance(changed, Mapping):
        return ("", ())
    head = ""
    paths: list[str] = []
    for key, entry in changed.items():
        if not _names_repository(key, alias) or not isinstance(entry, Mapping):
            continue
        head = _text(entry.get("head_after")) or _text(entry.get("head_before")) or head
        paths.extend(_relative_paths(entry.get("qualified_paths"), alias))
    return (head, tuple(dict.fromkeys(paths)))


def _names_repository(key: object, alias: str) -> bool:
    """Whether an evidence key is about the repository ``alias`` names."""
    return isinstance(key, str) and key in (alias, LEGACY_REPOSITORY_KEY)


def _relative_paths(value: object, alias: str) -> tuple[str, ...]:
    """Strip the qualifying prefix from every entry that names this repository.

    Evidence is written as ``<alias-or-dot>:<path>``. An entry qualified by some
    other repository is dropped rather than dequalified, because a path that
    belongs to a repository this checkpoint is not committing is not part of
    what it may own.
    """
    if not isinstance(value, list):
        return ()
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        prefix, separator, remainder = item.partition(":")
        if not separator:
            paths.append(item)
            continue
        if _names_repository(prefix, alias) and remainder:
            paths.append(remainder)
    return tuple(dict.fromkeys(paths))


def _refuse(attribution: Attribution, refusal: Refusal, detail: str) -> Attribution:
    """Return the same attribution, refused, so the evidence survives the answer."""
    return Attribution(
        alias=attribution.alias,
        root=attribution.root,
        baseline=attribution.baseline,
        head=attribution.head,
        dirty=attribution.dirty,
        owned=attribution.owned,
        unattributed=attribution.unattributed,
        activity=attribution.activity,
        refusal=refusal,
        detail=detail,
    )


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
