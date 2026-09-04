"""What a run owes in commits, and how it settles the account before it ends.

A run commits more than once — once per accepted session and once more at the
end — and each commit may span several repositories that have to move together
or not at all. This boundary owns that ledger. It decides *which* repositories
a commit group covers, drives the group through
:mod:`agent_session_loop.commit_groups`, records every attempt that failed
without stopping the run, and finally closes the run out.

Four things live here:

* **Commit target resolution.** :func:`_commit_targets` turns a verification
  context into the repository rows a group commits, and the four resolvers
  above it decide which of those rows a particular boundary actually owes:
  :func:`_dirty_commit_targets` for an unregistered workspace,
  :func:`_registered_session_commit_targets` for a session commit whose scope
  is whatever the workspace write guard authorized,
  :func:`_registered_end_commit_targets` for the end-of-run sweep, and
  :func:`_resolve_recovered_commit_targets` for a group a previous run left
  owed.
* **The commit group lifecycle.** :func:`_run_commit_group` builds or resumes a
  group, runs one ``asl commit`` per target through
  :mod:`agent_session_loop.run_commit`, and persists the group at every phase.
  :func:`_commit_failure_record` and :func:`_unresolved_commit_groups` are the
  other half of the same story: what the run admits it failed to commit.
* **The settlement of a paused turn's retained work.**
  :func:`_settle_paused_retained_work` is the one commit this ledger makes for
  work that was *not* accepted: :func:`_paused_work_commit_targets` picks the
  repositories, the group runs under the ``session-paused`` scope, and the
  outcome is recorded under :data:`PAUSED_WORK_COMMIT_KEY`.
* **Run finish.** :func:`_finish_run` writes the run's own ending — its finish
  timestamp, its duration, the cleared live-action fields — appends
  ``run_finished`` and notifies the AgentMux loop.

The durable records this boundary owns are the ``commit_groups`` list in
``state.json`` (through :func:`_upsert_commit_group`), the
:data:`COMMIT_FAILURES_KEY` and :data:`UNRESOLVED_COMMIT_GROUPS_KEY` payload
keys, the three commit-group events, and the ``run_finished`` event. Every one
of them is written through the ``_RunPersistence`` handle the caller supplies
and under that handle's lock; this module opens no file of its own. The commit
subprocess itself is `run_commit`'s, and it deliberately runs *outside* the
lock: the started and finished payloads come back from that boundary and are
appended here, under the lock, once the subprocess is done.

Five invariants survive the move:

* **A commit is only launched against a repository that has something to
  commit.** ``asl commit`` refuses an empty commit and exits nonzero, so a
  target that is already clean would record a failed attempt that the next run
  reads back as ``skipped-clean`` and finalizes as complete (ASI-19). Every
  resolver here filters on :func:`_commit_target_is_clean` before it returns,
  which is what keeps a group's record honest.
* **The write guard's evidence is the session commit's scope, and it is
  validated rather than trusted.** :func:`_registered_session_commit_targets`
  refuses a guard that did not pass, that carries added, removed, unavailable
  or violating evidence, whose authorized aliases are malformed or duplicated,
  or whose authorized aliases disagree with the aliases its own change records
  name. Only :data:`STAGEABLE_CHANGE_CATEGORIES` become pathspecs; a linked
  worktree's paths are relative to that checkout rather than to the root the
  commit runs in, so such an alias falls back to a repository-wide stage
  instead of naming paths git would reject.
* **A commit failure is recorded, never silent.** A group that ended ``failed``
  or ``pending`` is named by its own durable row, and an attempt that failed
  *before* any group existed leaves no row at all — so
  :func:`_unresolved_commit_groups` summarizes that failure in place. Otherwise
  the one commit failure with no durable evidence would also be the only one a
  run could end silent about.
* **Retries are bounded and the loss is inherited rather than looped on.**
  :data:`MAX_COMMIT_GROUP_RETRIES` caps how many times one group is retried
  inside a single run; past that the group is left owed, in ``failed``, for a
  later run to recover. Each attempt is a real harness call that writes a
  commit message, which is why the budget is small.
* **A paused run does not announce that it finished.** :func:`_finish_run`
  emits the AgentMux Finished notification only when the payload is not
  paused — a paused run already notified Paused, and Finished would release the
  daemon's run lease and overwrite the paused attention icon.

One more invariant arrived after them, because the ledger now settles a
*paused* turn's retained work as well as an accepted one's, and the two commits
make different claims. An accepted session's commit says the session completed
and its verification passed. The retained-work commit a *recoverable* pause
makes says only that the turn's changes were written down before the process
exited: the session it names is back to ``pending``, nothing verified the work,
and the next attempt may replace all of it. Three things keep that weaker claim
from reading as the stronger one. The group is scoped ``session-paused`` rather
than ``session``, so it is a different identity from the accepted commit the
same session may still earn later and cannot overwrite its row. The message is
rendered by :func:`run_commit._paused_work_commit_message` and committed with
``--message-file`` rather than generated, both because the pause may *be* the
harness failing and because a marking that says the work is unverified must not
be rewordable by a model. And the record under :data:`PAUSED_WORK_COMMIT_KEY`
names, in ``residue``, whatever the commit failed to take with it — the
settlement re-probes every target rather than assuming a commit it launched
left the tree clean. The commit exists so the relaunch starts on a clean tree,
not so the run can claim the work was good.

The module never imports the runner. Everything it needs is either a leaf it
reads at the owner — ``clock``, ``commit_groups``, ``events``,
``turn_execution`` — or a boundary extracted before it: the git shell in
``review_guard``, the run's write handle in ``run_persistence``, the progress
voice in ``run_reporting``, and the commit subprocess in ``run_commit``. The
two things it still needs from the loop enter as callbacks rather than as
imports: the readiness lifecycle :func:`_finish_run` closes, which Session 15
owns and which arrives as the :data:`RunFinalizer` callback, and the commit
soft-fail whose retry budget lives on the run context, which arrives as
:class:`CommitSoftFail`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import (
    clock,
    commit_groups,
    events,
    pause_policy,
    review_guard,
    run_commit,
    run_persistence,
    run_reporting,
    run_selection,
    turn_execution,
    verification,
)
from .run_errors import RunError

#: The write-guard change categories `git add --` can stage. The other three
#: `repository_evidence` categories cannot: `untracked_removed` names paths that
#: no longer exist, `worktree_ignore` names paths git refuses to add outright,
#: and `ignore_config` names exclusion sources that may live outside the
#: worktree altogether. An allow-list is what keeps a new category from
#: silently becoming stageable.
STAGEABLE_CHANGE_CATEGORIES: tuple[str, ...] = (
    "tracked",
    "untracked_added",
    "untracked_changed",
)

#: Closes the readiness lifecycle a finished run leaves behind. The
#: implementation is Session 15's ``_finalize_readiness_lifecycle``; it enters
#: :func:`_finish_run` as this callback so the commit ledger never has to reach
#: back into the loop for it.
RunFinalizer = Callable[[dict[str, Any]], None]


class CommitSoftFail(Protocol):
    """Absorb a commit failure and keep the run going.

    The loop keeps the implementation — its own ``_soft_fail_commit`` — because
    the retry budget it spends lives on the run context, and because the
    owed-group settlement at the top of the loop shares it with every other
    commit boundary. It arrives here already bound to that context, as
    ``functools.partial(_soft_fail_commit, ctx)``, exactly as it already does
    for ``turn_settlement.SoftFailCommit``; that is what lets this module
    record a loss only the loop can retry without importing the loop.

    It never raises: a commit that cannot be made is a record and a warning,
    not an ending. For the paused-work settlement that matters twice over,
    since the run is already on its way to a pause and a raise here would
    replace a recoverable pause with a crash.
    """

    def __call__(
        self,
        *,
        scope: str,
        session_id: str | None,
        group: Mapping[str, Any] | None,
        commit: Mapping[str, Any] | None,
        error: str | None,
        message: str,
    ) -> None: ...


@dataclasses.dataclass(frozen=True)
class RetainedWorkContext:
    """The paused turn whose retained work the settlement is about to commit.

    Everything the settlement needs to know about the turn, gathered by the
    caller so this module never reads the loop's context. ``session_label`` is
    :func:`plans.session_label`'s rendering rather than a bare id, because the
    commit message opens a sentence with it. ``turn_payload`` is the live
    dictionary the run mutates in place: the settlement writes its record onto
    it under :data:`PAUSED_WORK_COMMIT_KEY`, and reads ``workspace_write_guard``
    off it for the registered staging scope. ``artifact_dir`` is where the
    deterministic commit message is written, and ``result_path`` is the turn's
    own durable artifact — re-persisting it after the settlement returns
    belongs to the caller, which owns the file writes this module does not
    make.

    The two flags decide whether the pause owes a commit at all. A review
    session is excluded, matching the accepted path, which never commits one
    either. A repair session with no commit mode owns its own commit, so it is
    included where a plain session with no commit mode is not.
    """

    session_id: str
    session_label: str
    turn_payload: dict[str, Any]
    result_path: Path
    artifact_dir: Path
    is_review_session: bool
    is_repair_session: bool


def _upsert_commit_group(payload: dict[str, Any], group: Mapping[str, Any]) -> None:
    groups = payload.setdefault("commit_groups", [])
    group_id = group.get("group_id")
    for index, existing in enumerate(groups):
        if isinstance(existing, Mapping) and existing.get("group_id") == group_id:
            groups[index] = dict(group)
            return
    groups.append(dict(group))


#: Every soft-failed commit attempt this run made, in the order it made them.
#: A commit failure no longer pauses the run, so this is the durable trace that
#: one happened at all — including the attempt that failed before a durable
#: group existed, which leaves no ``commit_groups`` row behind.
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
#: is small on purpose.
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
    session_id = summary.get("session_id")
    scope = "end-of-run" if session_id is None else f"session {session_id}"
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


def _commit_target_head(target: Mapping[str, Any]) -> str | None:
    return review_guard._read_git_head(Path(str(target["root"]))).get("head")


def _commit_target_is_clean(target: Mapping[str, Any]) -> bool:
    return review_guard._git_status_is_clean(Path(str(target["root"])))


def _commit_targets(
    verification_context: verification.VerificationContext,
    *,
    aliases: Sequence[str] | None = None,
    qualified_paths: Mapping[str, Sequence[str]] | None = None,
    staging_paths: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    workspace = verification_context.workspace
    if workspace is None:
        return [
            commit_groups.create_target(
                alias=None,
                root=verification_context.root,
                repository_path=".",
            )
        ]
    selected = set(workspace.registry.aliases if aliases is None else aliases)
    unknown = selected - set(workspace.registry.aliases)
    if unknown:
        raise RunError(
            "commit group references unknown repositories: "
            + ", ".join(sorted(unknown))
        )
    path_map = qualified_paths or {}
    staging_map = staging_paths or {}
    return [
        commit_groups.create_target(
            alias=repository.alias,
            root=repository.root,
            repository_path=repository.repository.path,
            qualified_paths=path_map.get(repository.alias, ()),
            staging_paths=staging_map.get(repository.alias, ()),
        )
        for repository in workspace.repositories
        if repository.alias in selected
    ]


def _dirty_commit_targets(
    verification_context: verification.VerificationContext,
) -> list[dict[str, Any]]:
    """Commit targets that actually have something to commit.

    An unregistered workspace resolves to exactly one target, and until this
    filter existed nothing asked whether that target was dirty before the
    commit was launched. `asl commit` refuses a commit with nothing staged and
    exits nonzero, so a session that wrote nothing into the bound repository
    recorded a failed commit attempt with empty staging and qualified paths --
    and the next run read the same still-clean tree back as `skipped-clean`
    and finalized the failed group as complete (ASI-19). Not launching a
    commit there is what keeps the group's record honest, and it is the rule
    `_registered_session_commit_targets` has always applied to a registered
    workspace.
    """
    return [
        target
        for target in _commit_targets(verification_context)
        if not _commit_target_is_clean(target)
    ]


def _registered_session_commit_targets(
    verification_context: verification.VerificationContext,
    guard: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if guard.get("passed") is not True:
        raise RunError("workspace write guard did not pass")
    for guard_field in ("added", "removed", "violations"):
        if guard.get(guard_field):
            raise RunError(f"workspace write guard has unsafe {guard_field} evidence")
    if guard.get("unavailable"):
        raise RunError("workspace write guard evidence is unavailable")
    aliases = guard.get("authorized_aliases")
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) for alias in aliases
    ):
        raise RunError("workspace write guard authorized aliases are malformed")
    if len(set(aliases)) != len(aliases):
        raise RunError("workspace write guard authorized aliases are duplicated")
    qualified: dict[str, Sequence[str]] = {}
    staging: dict[str, list[str]] = {}
    unscoped_aliases: set[str] = set()
    changed_aliases: set[str] = set()
    changed = guard.get("changed")
    if not isinstance(changed, Mapping):
        raise RunError("workspace write guard changed evidence is malformed")
    for raw in changed.values():
        if not isinstance(raw, Mapping):
            raise RunError("workspace write guard change record is malformed")
        alias = raw.get("alias")
        paths = raw.get("qualified_paths")
        if not isinstance(alias, str) or not isinstance(paths, list):
            raise RunError("workspace write guard change record is malformed")
        changed_aliases.add(alias)
        qualified[alias] = [str(path) for path in paths]
        bucket = staging.setdefault(alias, [])
        if raw.get("worktree_of") is not None:
            # A linked worktree's paths are relative to that checkout, not to
            # the owning repository's root the commit runs in, so none of them
            # can be named on a pathspec there. The alias keeps the
            # repository-wide `git add -A` fallback instead of staging a
            # subset that would leave the commit dirty afterwards.
            unscoped_aliases.add(alias)
            continue
        for category in STAGEABLE_CHANGE_CATEGORIES:
            values = raw.get(category)
            if not isinstance(values, list):
                continue
            bucket.extend(f"{alias}:{path}" for path in values)
    for alias in unscoped_aliases:
        staging[alias] = []
    workspace = verification_context.workspace
    assert workspace is not None
    expected_aliases = [
        repository.alias
        for repository in workspace.repositories
        if repository.alias in changed_aliases
    ]
    if aliases != expected_aliases:
        raise RunError("workspace write guard changed and authorized aliases disagree")
    targets = _commit_targets(
        verification_context,
        aliases=aliases,
        qualified_paths=qualified,
        staging_paths={
            alias: list(dict.fromkeys(paths)) for alias, paths in staging.items()
        },
    )
    return [target for target in targets if not _commit_target_is_clean(target)]


def _registered_end_commit_targets(
    verification_context: verification.VerificationContext,
) -> list[dict[str, Any]]:
    workspace = verification_context.workspace
    if workspace is None:
        return _dirty_commit_targets(verification_context)
    aliases = [
        repository.alias
        for repository in workspace.repositories
        if not review_guard._git_status_is_clean(repository.root)
    ]
    return _commit_targets(verification_context, aliases=aliases)


def _resolve_recovered_commit_targets(
    group: Mapping[str, Any],
    verification_context: verification.VerificationContext,
) -> list[dict[str, Any]]:
    keys = group.get("ordered_aliases")
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise RunError("unresolved commit group has malformed aliases")
    rows = group.get("repositories")
    if not isinstance(rows, list):
        raise RunError("unresolved commit group has malformed repositories")
    qualified = {
        str(row.get("key")): [str(path) for path in row.get("qualified_paths", ())]
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("key"), str)
    }
    # A group persisted before staging paths existed has no `staging_paths`
    # row, which reads as an empty list and restores the repository-wide
    # `git add -A` this recovery always used.
    staging = {
        str(row.get("key")): [str(path) for path in row.get("staging_paths", ())]
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("key"), str)
    }
    if verification_context.workspace is None:
        if keys != [commit_groups.LEGACY_REPOSITORY_KEY]:
            raise RunError("unresolved commit group does not match the repository")
        return _commit_targets(verification_context)
    if commit_groups.LEGACY_REPOSITORY_KEY in keys:
        raise RunError("legacy commit group cannot be resumed in a workspace")
    return _commit_targets(
        verification_context,
        aliases=keys,
        qualified_paths=qualified,
        staging_paths=staging,
    )


def _run_commit_group(
    *,
    event_path: Path,
    payload: dict[str, Any],
    progress: run_reporting._ProgressReporter,
    verification_context: verification.VerificationContext,
    run_dir: Path,
    run_id: str,
    session_id: str | None,
    targets: Sequence[Mapping[str, Any]],
    generation: run_commit._CommitGeneration,
    scope: str | None = None,
    recovered_group: Mapping[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Build or resume one commit group and drive it to its first stop.

    ``scope`` names the boundary the group is owed at. Left unset it is
    derived from ``session_id`` exactly as it always was — a session commit
    when a session owns it, the end-of-run sweep otherwise — so a caller that
    does not care keeps today's behaviour. A caller that owes a session a
    second, differently-scoped commit passes the scope explicitly, because the
    scope is part of the group's identity and therefore of its ``group_id``.
    """
    persistence = run_persistence._run_persistence(persistence)
    if recovered_group is None:
        initial_heads = {
            str(target["key"]): _commit_target_head(target) for target in targets
        }
        group = commit_groups.create_group(
            scope=(
                scope
                if scope is not None
                else (
                    commit_groups.GROUP_SCOPE_SESSION
                    if session_id is not None
                    else commit_groups.GROUP_SCOPE_END
                )
            ),
            session_id=session_id,
            targets=targets,
            initial_heads=initial_heads,
        )
        _persist_commit_group(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            run_id=run_id,
            group=group,
            phase="started",
        )
        skip_clean = False
    else:
        group = dict(recovered_group)
        group["repositories"] = [dict(target) for target in targets]
        skip_clean = True

    def persist_group(current: dict[str, Any], phase: str) -> None:
        _persist_commit_group(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            run_id=run_id,
            group=current,
            phase=phase,
        )

    def run_target(target: Mapping[str, Any], group_id: str) -> Mapping[str, Any]:
        with persistence.lock:
            commit_index = len(payload["commits"]) + 1
        repository_alias = target.get("alias")
        commit_started = run_commit._commit_started_payload(
            run_id=run_id,
            cwd=Path(str(target["root"])),
            run_dir=run_dir,
            commit_index=commit_index,
            session_id=session_id,
            group_id=group_id,
            repository_key=str(target["key"]),
            repository_alias=(
                str(repository_alias) if repository_alias is not None else None
            ),
            repository_path=str(target["repository_path"]),
            qualified_paths=[str(path) for path in target.get("qualified_paths", ())],
            staging_paths=[str(path) for path in target.get("staging_paths", ())],
            generation=generation,
        )
        with persistence.lock:
            persistence.append_event(
                event_path, events.COMMIT_STARTED_EVENT, commit_started
            )
            persistence.write_state(payload)
        progress.commit_started(commit_started)

        commit_payload = run_commit._run_asl_commit(
            cwd=Path(str(target["root"])),
            run_dir=run_dir,
            commit_index=commit_index,
            session_id=session_id,
            group_id=group_id,
            repository_key=str(target["key"]),
            repository_alias=(
                str(repository_alias) if repository_alias is not None else None
            ),
            repository_path=str(target["repository_path"]),
            qualified_paths=[str(path) for path in target.get("qualified_paths", ())],
            staging_paths=[str(path) for path in target.get("staging_paths", ())],
            generation=generation,
        )
        commit_finished = {"run_id": run_id, **commit_payload}
        with persistence.lock:
            payload["commits"].append(commit_payload)
            persistence.append_event(
                event_path, events.COMMIT_FINISHED_EVENT, commit_finished
            )
            persistence.write_state(payload)
        progress.commit_finished(commit_finished)
        return commit_payload

    try:
        return commit_groups.coordinate_group(
            group,
            is_clean=_commit_target_is_clean,
            read_head=_commit_target_head,
            run_commit=run_target,
            persist=persist_group,
            skip_clean=skip_clean,
        )
    except commit_groups.CommitGroupError as error:
        raise RunError(f"invalid commit group: {error}") from error


def _paused_work_commit_targets(
    verification_context: verification.VerificationContext,
    *,
    guard: Mapping[str, Any],
    whole_workspace: bool,
) -> list[dict[str, Any]]:
    """Which repositories a recoverable pause owes a retained-work commit in.

    No staging is invented here. The retained work is settled with exactly the
    selection the accepted path would have used for the same boundary, because
    a pause that stages *more* than the accepted commit would have would sweep
    up whatever else is in the tree under a message that claims to describe one
    turn.

    Under ``--commit-at-end`` (``whole_workspace``) that selection is the
    end-of-run sweep, which already falls back to the single dirty target when
    there is no workspace, so the legacy case needs no branch of its own.
    Otherwise it is the session selection: the write guard's evidence in a
    registered workspace, and the one dirty repository in a legacy one.

    ``guard`` is the paused turn's ``workspace_write_guard`` evidence. It is
    read only on the registered session branch, and it is validated rather than
    trusted there: a guard that did not pass raises before any group exists,
    which is what keeps a pause from committing changes no guard ever cleared.
    """
    if whole_workspace:
        return _registered_end_commit_targets(verification_context)
    if verification_context.workspace is None:
        return _dirty_commit_targets(verification_context)
    return _registered_session_commit_targets(verification_context, guard)


def _commit_target_residue(target: Mapping[str, Any]) -> dict[str, Any] | None:
    """Name what is still dirty in ``target`` after a commit ran against it.

    ``None`` for a target the commit left clean. The staging a session commit
    uses is derived from the write guard's evidence, and a linked worktree
    alias falls back to a repository-wide stage instead, so neither shape is
    guaranteed to have taken everything in the tree with it. The relaunch's
    startup gate reads the whole tree, so anything left behind is what will
    still block it — naming it in the record is what keeps the settlement from
    reporting a success the next process disagrees with.

    ``_commit_target_is_clean`` has already run ``git status`` in this same
    worktree and raised if git could not answer, so the second call is not
    re-guarded.
    """
    if _commit_target_is_clean(target):
        return None
    status = review_guard._run_git(
        Path(str(target["root"])),
        ["status", "--porcelain", "-z", "--untracked-files=all"],
    )
    alias = target.get("alias")
    paths = review_guard._porcelain_dirty_paths(status.stdout)
    return {
        "alias": alias,
        "clean": False,
        "dirty_paths": [
            f"{alias}:{path}" if alias is not None else path for path in paths
        ],
    }


def _commit_landed_but_left_dirt(commit: Mapping[str, Any] | None) -> bool:
    """Whether an unsuccessful commit is one that landed on a still-dirty tree.

    ``run_commit`` calls a commit unsuccessful when the worktree is still dirty
    after it, and that verdict stands: the group fails, the settlement records
    ``result: "failed"`` and soft-fails. But the commit itself happened — its
    head advanced — so what is left in the tree is residue in exactly the sense
    :func:`_commit_target_residue` names, and the record is the only place the
    relaunch's operator can read which paths will meet the startup gate.
    Without this the record says only that the commit failed, which reads as
    "nothing was committed" when in fact something was.

    ``clean_after`` is read rather than ``status_result`` because it is the
    measurement rather than a name for it: exactly ``False`` is git having
    answered that the tree is dirty, which is also what makes the residue probe
    below answerable at all. A commit whose status could not be read
    (``None``) is left alone, so nothing new can raise out of the settlement.
    """
    if not isinstance(commit, Mapping) or commit.get("clean_after") is not False:
        return False
    head_before = commit.get("head_before")
    head_after = commit.get("head_after")
    return (
        isinstance(head_before, str)
        and isinstance(head_after, str)
        and bool(head_after)
        and head_before != head_after
    )


def _repair_number(value: Any) -> int | None:
    """Read one repair counter out of the pause details, never recomputing it."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _repair_flag(value: Any) -> bool | None:
    """Read the repair-exhausted verdict out of the pause details."""
    return value if isinstance(value, bool) else None


def _paused_work_commit_is_owed(
    retained: RetainedWorkContext,
    *,
    pause_reason: str,
    commit_mode: str | None,
) -> bool:
    """Whether this pause owes the work its turn retained a commit.

    Three refusals, each of them the same rule the accepted path already
    applies. A review session is never auto-committed. A pause a machine cannot
    recover from is one a human is about to inspect, and the retention of its
    dirt is the whole point of the gate that caused it. And a run with no
    commit mode owes no commit at all — unless it is a repair child, which owns
    its own commit precisely because its parent's mode did not reach it.
    """
    if retained.is_review_session:
        return False
    if pause_reason not in pause_policy.RECOVERABLE_PAUSE_REASONS:
        return False
    return commit_mode is not None or retained.is_repair_session


def _settle_paused_retained_work(
    *,
    event_path: Path,
    payload: dict[str, Any],
    progress: run_reporting._ProgressReporter,
    verification_context: verification.VerificationContext,
    run_dir: Path,
    run_id: str,
    retained: RetainedWorkContext,
    pause_reason: str,
    pause_details: Mapping[str, Any],
    commit_mode: str | None,
    generation: run_commit._CommitGeneration,
    soft_fail: CommitSoftFail,
    persistence: run_persistence._RunPersistence | None = None,
) -> dict[str, Any] | None:
    """Commit the work a recoverable pause retained, and record what happened.

    ``None`` means this pause owed nothing (see
    :func:`_paused_work_commit_is_owed`); every other outcome — including
    finding nothing to commit — returns the record it wrote.

    The repair attempt numbers are *read* from ``pause_details`` rather than
    recomputed. ``session_verification`` has already adjudicated them against
    the session's failure history and merged them into the pause details, so a
    second count here would be a second opinion about a budget that has one
    owner. A value the details do not carry, or carry in the wrong type, reads
    as ``None`` rather than as a number this module invented.

    Nothing raises out of here. A resolution that refuses, a group that fails
    and a commit that comes back unsuccessful all land in the same record with
    ``result: "failed"`` and are handed to ``soft_fail``, which re-arms the
    group for a retry when there is a durable group to retry. When there is
    not — a guard that did not pass raises before any group is created — the
    failure is recorded with no group at all, and the retained work stays in
    the tree for the human that pause is already waiting on.

    A failure still names its residue when the commit landed and left the tree
    dirty (see :func:`_commit_landed_but_left_dirt`) — the verdict is the
    group's to keep, but which paths survived the commit is what the next
    process's startup gate will refuse over, and it is knowable here.
    """
    if not _paused_work_commit_is_owed(
        retained, pause_reason=pause_reason, commit_mode=commit_mode
    ):
        return None
    persistence = run_persistence._run_persistence(persistence)
    repair_attempt = _repair_number(pause_details.get("verification_repair_attempt"))
    repair_max_attempts = _repair_number(
        pause_details.get("verification_repair_max_attempts")
    )
    repair_exhausted = _repair_flag(pause_details.get("verification_repair_exhausted"))

    targets: list[dict[str, Any]] = []
    group: dict[str, Any] | None = None
    failed_commit: dict[str, Any] | None = None
    error: str | None = None
    try:
        targets = _paused_work_commit_targets(
            verification_context,
            guard=retained.turn_payload.get("workspace_write_guard") or {},
            whole_workspace=commit_mode == run_selection.COMMIT_MODE_END,
        )
        if targets:
            message_file = run_commit._write_paused_work_message(
                retained.artifact_dir,
                run_commit._paused_work_commit_message(
                    session_label=retained.session_label,
                    pause_reason=pause_reason,
                    commit_mode=commit_mode,
                    repair_attempt=repair_attempt,
                    repair_max_attempts=repair_max_attempts,
                    run_id=run_id,
                ),
            )
            group, failed_commit = _run_commit_group(
                event_path=event_path,
                payload=payload,
                progress=progress,
                verification_context=verification_context,
                run_dir=run_dir,
                run_id=run_id,
                session_id=retained.session_id,
                targets=targets,
                generation=dataclasses.replace(generation, message_file=message_file),
                scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                persistence=persistence,
            )
    except (RunError, commit_groups.CommitGroupError) as failure:
        error = str(failure)

    failed = error is not None or failed_commit is not None
    probe_residue = not failed or _commit_landed_but_left_dirt(failed_commit)
    residue = (
        [
            row
            for row in (_commit_target_residue(target) for target in targets)
            if row is not None
        ]
        if probe_residue
        else []
    )
    if failed:
        result = "failed"
    elif not targets:
        result = "no-changes"
    elif residue:
        result = "committed-with-residue"
    else:
        result = "committed"

    record: dict[str, Any] = {
        "guard": PAUSED_WORK_COMMIT_GUARD,
        "pause_reason": pause_reason,
        "commit_mode": commit_mode,
        "result": result,
        "verification_repair_attempt": repair_attempt,
        "verification_repair_max_attempts": repair_max_attempts,
        "verification_repair_exhausted": repair_exhausted,
        "commit_group": group,
        "commit": failed_commit,
        "error": error,
        "residue": {"repositories": residue},
    }
    with persistence.lock:
        retained.turn_payload[PAUSED_WORK_COMMIT_KEY] = record
        persistence.append_event(
            event_path,
            events.PAUSED_WORK_COMMITTED_EVENT,
            {"run_id": run_id, "session_id": retained.session_id, **record},
        )
        persistence.write_state(payload)
    if failed:
        soft_fail(
            scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
            session_id=retained.session_id,
            group=group,
            commit=failed_commit,
            error=error,
            message=(
                "the work retained by the "
                f"{pause_reason} pause of {retained.session_label} "
                "could not be committed"
            ),
        )
    return record


def _finish_run(
    event_path: Path,
    payload: dict[str, Any],
    progress: run_reporting._ProgressReporter | None = None,
    persistence: run_persistence._RunPersistence | None = None,
    *,
    finalize_readiness: RunFinalizer,
) -> None:
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        finished_at = clock.utc_now()
        payload["run_finished_at"] = clock.isoformat(finished_at)
        started_at = clock.parse_datetime(payload.get("run_started_at"))
        payload["duration_seconds"] = (
            clock.duration_seconds(started_at, finished_at)
            if started_at is not None
            else None
        )
        payload["active_turn"] = None
        payload["active_verification"] = None
        if not payload.get("paused"):
            turn_execution.clear_live_action_required(payload)
        finalize_readiness(payload)
        persistence.append_event(
            event_path,
            events.RUN_FINISHED_EVENT,
            run_persistence._run_finished_event_payload(payload),
        )
        persistence.write_state(payload)
    # A paused run already notified AgentSessionLoopPaused from _pause_run;
    # emitting Finished here would release the daemon's run lease and
    # overwrite the paused attention icon with the finished state.
    if not payload.get("paused"):
        run_persistence._notify_agent_mux_loop_finished(payload, persistence)
    if progress is not None:
        progress.run_finished(payload)
