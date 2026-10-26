# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Commit-group execution and durable phase transitions."""

from . import commit_settlement_ledger as _ledger
from . import commit_settlement_targets as _targets
from . import commit_settlement_types as _types

for _module in (_types, _ledger, _targets):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


def _run_commit_group(
    *,
    event_path: Path,
    payload: dict[str, Any],
    progress: run_reporting._ProgressReporter,
    verification_context: verification_models.VerificationContext,
    run_dir: Path,
    run_id: str,
    session_id: str | None,
    targets: Sequence[Mapping[str, Any]],
    generation: run_commit._CommitGeneration,
    scope: str | None = None,
    verbatim_message: str | None = None,
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

    ``verbatim_message`` is the same text ``generation.message_file`` points
    at, recorded on the durable group so a *later* run can rebuild that argv.
    The two are passed separately rather than derived from one another because
    this module writes no files: the message file is written by the caller that
    rendered the text, and the text is what the group has to keep. A caller
    that leaves the generation on its default message harness leaves this unset
    too, and the group records that its message was generated.
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
            verbatim_message=verbatim_message,
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

    latest: dict[str, dict[str, Any]] = {"group": group}

    def persist_group(current: dict[str, Any], phase: str) -> None:
        latest["group"] = current
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
    except RunError as error:
        # The group is already durable as ``pending`` by now, so letting the
        # error escape would hand the caller nothing to retry: it would see no
        # group, treat the failure as one that never built anything durable,
        # and keep running while the next session's ``git add -A`` absorbed
        # this group's changes. A Git spawn failure or timeout while reading a
        # HEAD is a failed repository like any other, so it is recorded as one.
        return _fail_interrupted_group(
            latest["group"], error=error, persist=persist_group
        )


def _fail_interrupted_group(
    group: dict[str, Any],
    *,
    error: RunError,
    persist: Callable[[dict[str, Any], str], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mark a group ``failed`` at the repository an error interrupted.

    Every repository the coordinator finished is recorded before it moves on,
    so the first one still remaining is the one it was working on.
    """
    current = dict(group)
    commit_groups._refresh_remaining(current)
    remaining = current.get("remaining_aliases") or [None]
    current["status"] = commit_groups.GROUP_STATUS_FAILED
    current["failed_alias"] = remaining[0]
    persist(current, "repository-failed")
    return current, {
        "success": False,
        "status_result": "error",
        "error": str(error),
        "group_id": current.get("group_id"),
        "repository_key": remaining[0],
    }
