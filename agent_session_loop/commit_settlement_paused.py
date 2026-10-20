# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Settlement for retained work when a turn pauses recoverably."""

from . import commit_settlement_groups as _groups
from . import commit_settlement_ledger as _ledger
from . import commit_settlement_targets as _targets
from . import commit_settlement_types as _types

for _module in (_types, _ledger, _targets, _groups):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


def _paused_work_commit_targets(
    verification_context: verification_models.VerificationContext,
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

    ``None`` for a target this probe found clean, and a row for every other
    answer. The staging a session commit uses is derived from the write guard's
    evidence, and a linked worktree alias falls back to a repository-wide stage
    instead, so neither shape is guaranteed to have taken everything in the
    tree with it — and a commit that refused took nothing at all. The
    relaunch's startup gate reads the whole tree, so anything left behind is
    what will still block it; naming it in the record is what keeps the
    settlement from reporting an outcome the next process disagrees with.

    Three row shapes, because the probe has three answers rather than two.
    Clean is ``None`` and is reported by omission. Dirty carries ``clean:
    False`` and the repository-qualified ``dirty_paths``. And git failing to
    answer carries ``clean: None`` with the failure in ``error``: the probe
    runs after a commit that may itself have failed *because* the repository
    is unreadable, and the settlement is on its way to a pause it must not
    replace with a crash, so an unanswerable target is recorded as
    undetermined rather than raised or silently read as clean. ``error`` is
    ``None`` on the other rows so a reader never has to ask whether the key
    exists.

    ``_commit_target_is_clean`` is the cleanliness verdict rather than the
    NUL-framed listing below it, because it is the same measurement the target
    resolvers and the group's own skip-clean check make; the second call is
    what names the paths the first only counted.
    """
    alias = target.get("alias")
    try:
        if _commit_target_is_clean(target):
            return None
        status = review_guard._run_git(
            Path(str(target["root"])),
            ["status", "--porcelain", "-z", "--untracked-files=all"],
        )
        if status.returncode != 0:
            detail = status.stderr.strip() or status.stdout.strip()
            raise RunError(
                f"could not inspect git status: {detail}"
                if detail
                else "could not inspect git status"
            )
    except RunError as failure:
        return {
            "alias": alias,
            "clean": None,
            "dirty_paths": [],
            "error": str(failure),
        }
    paths = review_guard._porcelain_dirty_paths(status.stdout)
    return {
        "alias": alias,
        "clean": False,
        "dirty_paths": [
            f"{alias}:{path}" if alias is not None else path for path in paths
        ],
        "error": None,
    }


#: The ``status_result`` values that mean the commit itself landed even though
#: the commit boundary still called the attempt unsuccessful.
#: ``run_commit._commit_status_result`` reaches either of them only after a zero
#: exit that moved ``HEAD``: ``dirty-after-commit`` is a moved head over a tree
#: that still holds something, and ``status-unavailable`` is a moved head whose
#: tree could not be re-read. Every other unsuccessful classification —
#: ``nonzero-exit``, ``missing-head``, ``unchanged-head`` — means nothing
#: reached the history. The classification is read rather than re-derived from
#: ``head_before`` and ``head_after`` so the settlement and the commit boundary
#: never disagree about what landed; a new value there belongs in this set or
#: deliberately outside it.
LANDED_COMMIT_STATUS_RESULTS: frozenset[str] = frozenset(
    {"dirty-after-commit", "status-unavailable"}
)

#: How many residue paths one soft-failure message names before it stops
#: listing and points at the record instead. That message is durable — it is
#: what ``commit_failures`` carries and what the supervisor audit bundle
#: shows — so it names enough paths to act on and leaves the rest to
#: ``paused_work_commit.residue``, which is complete by construction.
MAX_NAMED_RESIDUE_PATHS = 5

#: How many repositories the same message names before it points at
#: ``commit_group.successful_repositories`` instead. A workspace may declare
#: many repositories and a partial failure may have committed most of them; the
#: sentence exists to correct the claim that *none* of the work landed, which
#: the first few names already do.
MAX_NAMED_COMMITTED_REPOSITORIES = 3


def _paused_work_residue_paths(residue: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every repository-qualified path the residue probe named, in probe order."""
    return [str(path) for row in residue for path in row.get("dirty_paths") or ()]


def _repository_label(row: Mapping[str, Any]) -> str | None:
    """The name an operator knows one commit-group repository row by.

    The alias when the workspace registered one, and the row's key otherwise —
    which is :data:`commit_groups.LEGACY_REPOSITORY_KEY` for the single
    unregistered repository, the only case where the two differ.
    """
    alias = row.get("alias")
    if isinstance(alias, str) and alias:
        return alias
    key = row.get("key")
    return key if isinstance(key, str) and key else None


def _committed_repository_labels(group: Mapping[str, Any] | None) -> list[str]:
    """The repositories a group already committed, in the order it committed them.

    Read from the group's own ``successful_repositories`` rather than
    re-derived, so the sentence and the structured record cannot disagree about
    which repositories moved. A settlement that failed before any group existed
    has no rows to read and answers with none, which is exactly what it knows.
    """
    rows = (group or {}).get("successful_repositories")
    if not isinstance(rows, list):
        return []
    return [
        label
        for row in rows
        if isinstance(row, Mapping) and (label := _repository_label(row)) is not None
    ]


def _failed_repository_label(group: Mapping[str, Any] | None) -> str | None:
    """The repository a group stopped on, named as the operator knows it.

    ``failed_alias`` is a repository *key*, so it is resolved through the
    group's own repository rows rather than printed raw.
    """
    failed_alias = (group or {}).get("failed_alias")
    if not isinstance(failed_alias, str) or not failed_alias:
        return None
    rows = (group or {}).get("repositories")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping) and row.get("key") == failed_alias:
                return _repository_label(row) or failed_alias
    return failed_alias


def _named_with_overflow(values: Sequence[str], *, limit: int, record: str) -> str:
    """List at most ``limit`` names and point at ``record`` for the rest."""
    named = ", ".join(values[:limit])
    remaining = len(values) - limit
    if remaining > 0:
        named = f"{named} and {remaining} more in {record}"
    return named


def _residue_clause(residue: Sequence[Mapping[str, Any]]) -> str | None:
    """What the worktree still holds, or ``None`` when the probe named nothing."""
    paths = _paused_work_residue_paths(residue)
    if not paths:
        return None
    named = _named_with_overflow(
        paths,
        limit=MAX_NAMED_RESIDUE_PATHS,
        record="paused_work_commit.residue",
    )
    return f"the worktree still holds {named}"


def _paused_work_failure_message(
    *,
    pause_reason: str,
    session_label: str,
    group: Mapping[str, Any] | None,
    commit: Mapping[str, Any] | None,
    residue: Sequence[Mapping[str, Any]],
) -> str:
    """Say what a failed settlement actually did to the repository.

    ``result: "failed"`` is one word about the group, and the sentence beside
    it is what an operator acts on: it is the ``message`` on the durable
    ``commit_failures`` entry, the text the progress line prints, and the text
    the supervisor audit bundle carries. Three different failures produce that
    one word and they ask for different things, so the sentence is selected
    from two independent facts: whether the failing commit itself landed, and
    whether the *group* had already committed other repositories before it.

    A commit that never ran, exited nonzero, or left ``HEAD`` where it found it
    wrote nothing. When it was also the group's first repository, nothing at
    all reached the history and "could not be committed" is exactly true.

    A commit whose head advanced *did* write the turn's intended path into the
    history and left residue behind it; telling the operator that work could
    not be committed sends them looking for changes that are already in ``git
    log``, and says nothing about the paths the relaunch's startup gate is
    about to refuse over. Which of the two happened is the commit's own
    classification (:data:`LANDED_COMMIT_STATUS_RESULTS`), never a second
    reading of the repository taken here.

    And a group spanning several repositories can commit some of them cleanly
    before a later one refuses. The group's verdict is still ``failed`` and the
    refusing commit still landed nothing, but the all-or-nothing sentence would
    deny commits that are already in ``git log`` — the same misreport the
    landed branch exists to prevent, one repository up. So a refusal that
    follows successful repositories names them, names the repository the group
    stopped on, and lets ``commit_group.successful_repositories`` carry the
    rest.

    The residue the probe recorded is appended wherever it exists, because
    those paths are the operator's next action. The landed branch points at the
    record instead when the probe named none — an undetermined target, or a
    tree that no longer held what the commit boundary saw. A pointer rather
    than silence: a landed commit with no named paths is the one case where the
    record knows more than the sentence does.
    """
    status_result = commit.get("status_result") if commit is not None else None
    if status_result not in LANDED_COMMIT_STATUS_RESULTS:
        committed = _committed_repository_labels(group)
        if not committed:
            return (
                f"the work retained by the {pause_reason} pause of "
                f"{session_label} could not be committed"
            )
        named = _named_with_overflow(
            committed,
            limit=MAX_NAMED_COMMITTED_REPOSITORIES,
            record="commit_group.successful_repositories",
        )
        failed = _failed_repository_label(group)
        partial = (
            f"the {pause_reason} pause of {session_label} committed the work "
            f"it retained in {named}, but the work it retained in "
            f"{failed} could not be committed"
            if failed is not None
            else (
                f"the {pause_reason} pause of {session_label} committed the "
                f"work it retained in {named}, but not all of it; see "
                "commit_group.remaining_aliases for what is still owed"
            )
        )
        clause = _residue_clause(residue)
        return partial if clause is None else f"{partial}; {clause}"
    landed = (
        f"the {pause_reason} pause of {session_label} committed the work it "
        "retained, but"
    )
    clause = _residue_clause(residue)
    if clause is None:
        return (
            f"{landed} the commit was recorded {status_result}; see "
            "paused_work_commit.residue for what the worktree still holds"
        )
    return f"{landed} {clause}"


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
    verification_context: verification_models.VerificationContext,
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

    A failure still names its residue, because ``result: "failed"`` says the
    group did not finish rather than that nothing moved. Every resolved target
    is re-probed on every outcome: a commit that refused outright left all of
    its target's dirt behind, a commit whose head advanced onto a still-dirty
    tree left part of it, and a partial multi-repository failure left one
    target dirty beside targets it had already committed clean. The group's
    verdict stays the group's; which paths the next process's startup gate
    will refuse over is knowable here, and only here.

    The sentence handed to ``soft_fail`` is rendered from those same facts —
    the group, the failing commit and the residue — by
    :func:`_paused_work_failure_message` rather than being one fixed string,
    because a failed settlement may still have written to the history: a commit
    whose head advanced onto a dirty tree committed the turn's intended path,
    and a group that stopped on its second repository committed its first. A
    message that told the operator otherwise would be the only part of the
    record that disagreed with the history.
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
            message = run_commit_paused._paused_work_commit_message(
                session_label=retained.session_label,
                pause_reason=pause_reason,
                commit_mode=commit_mode,
                repair_attempt=repair_attempt,
                repair_max_attempts=repair_max_attempts,
                run_id=run_id,
            )
            message_file = run_commit_paused._write_paused_work_message(
                retained.artifact_dir, message
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
                verbatim_message=message,
                persistence=persistence,
            )
    except (RunError, commit_groups.CommitGroupError) as failure:
        error = str(failure)

    failed = error is not None or failed_commit is not None
    residue = [
        row
        for row in (_commit_target_residue(target) for target in targets)
        if row is not None
    ]
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
            message=_paused_work_failure_message(
                pause_reason=pause_reason,
                session_label=retained.session_label,
                group=group,
                commit=failed_commit,
                residue=residue,
            ),
        )
    return record
