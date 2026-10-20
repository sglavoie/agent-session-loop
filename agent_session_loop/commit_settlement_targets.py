# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Commit-target selection and recovered-group resolution."""

from . import commit_settlement_types as _types

globals().update(
    {name: value for name, value in vars(_types).items() if not name.startswith("__")}
)


def _commit_target_head(target: Mapping[str, Any]) -> str | None:
    return review_guard._read_git_head(Path(str(target["root"]))).get("head")


def _commit_target_is_clean(target: Mapping[str, Any]) -> bool:
    return review_guard._git_status_is_clean(Path(str(target["root"])))


def _commit_targets(
    verification_context: verification_models.VerificationContext,
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
    verification_context: verification_models.VerificationContext,
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
    verification_context: verification_models.VerificationContext,
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
    # The guard keeps recorded-but-not-written changes in `changed` too: an
    # --allow-external-changes alias someone else edited, and remote-ref-only
    # drift. Neither is the session's work, so neither is committed here.
    unowned = {
        str(key)
        for field in ("external_changes", "remote_ref_drift")
        if isinstance(guard.get(field), Mapping)
        for key in guard[field]
    }
    for key, raw in changed.items():
        if not isinstance(raw, Mapping):
            raise RunError("workspace write guard change record is malformed")
        alias = raw.get("alias")
        paths = raw.get("qualified_paths")
        if not isinstance(alias, str) or not isinstance(paths, list):
            raise RunError("workspace write guard change record is malformed")
        if key in unowned and alias not in aliases:
            continue
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
    verification_context: verification_models.VerificationContext,
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
    verification_context: verification_models.VerificationContext,
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


def _recovered_commit_generation(
    group: Mapping[str, Any],
    *,
    generation: run_commit._CommitGeneration,
    run_dir: Path,
) -> run_commit._CommitGeneration:
    """Select how a group another run left owed writes its commit message.

    Recovery inherits the *group*, not the process that created it, so the
    run's own generation — the message harness it was launched with — is the
    wrong default for exactly one scope. A ``session-paused`` group commits a
    marking that says its work is unverified, and
    :func:`_settle_paused_retained_work` rendered that text rather than
    generating it for two reasons that both outlive the first attempt: the
    pause may be the harness itself failing, and a claim about unverified work
    must not be rewordable by a model. Recovering it through the run's
    generation would spend a bounded harness turn to reword the one message in
    the ledger that must never change, and an unavailable harness would fail
    the recovery outright — leaving the retained work in the tree for the next
    process's startup gate to refuse, which is the dead end the settlement
    exists to remove.

    So the text the group carries is rewritten under this run's directory and
    handed back as a ``--message-file`` generation. Every other scope keeps the
    run's generation untouched: an accepted session's commit and the
    end-of-run sweep are recovered exactly as they were attempted, with a
    generated message.

    A ``session-paused`` group with no message is malformed durable evidence,
    not a group to guess a message for, so it raises rather than falling back
    to the harness this scope exists to avoid.
    """
    if group.get("scope") != commit_groups.GROUP_SCOPE_SESSION_PAUSED:
        return generation
    message = group.get("verbatim_message")
    if not isinstance(message, str) or not message.strip():
        raise RunError(
            "unresolved session-paused commit group carries no commit message"
        )
    return dataclasses.replace(
        generation,
        message_file=run_commit_paused._write_recovered_paused_work_message(
            run_dir,
            group_id=str(group.get("group_id")),
            message=message,
        ),
    )
