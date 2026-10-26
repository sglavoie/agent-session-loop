# ruff: noqa: F403, F405
from .scope_recovery_common import *
from .scope_recovery_common import _digest, _event_records
from .scope_recovery_external_changes import *
from .scope_recovery_external_changes import (
    _capture_recovery,
    _check_recovery_authority,
    _checkpoint_content,
    _dirty_paths,
    _external_proposal,
    _finish_checkpoint,
    _reconcile_checkpoint_staging,
)


def acknowledge_external_changes(
    plan_dir: str | Path,
    session_id: str,
    repositories: tuple[str, ...],
    *,
    state_dir: str | Path | None = None,
    accept: str | None = None,
    reason: str | None = None,
    checkpoint_retained: bool = False,
) -> dict[str, Any]:
    """Preview independent changes; acceptance never grants ongoing write authority."""
    from . import durability, run_commit, run_commit_paused

    if accept is not None and (reason is None or not reason.strip()):
        raise RecoveryError("acceptance requires an explicit operator reason")
    plan_dir = plans_files.canonicalize_path(plan_dir)
    state_locks.acquire_lock(
        plan_dir, state_dir, owner="asl-acknowledge-external-changes"
    )
    try:
        state_root = state.plan_state_dir(plan_dir, state_dir)
        event_path = state_root / events.EVENTS_FILE_NAME
        journal = state_root / RECOVERY_FILE_NAME
        try:
            progress = (
                json.loads(journal.read_text(encoding="utf-8"))
                if journal.exists()
                else None
            )
        except ValueError, RecursionError:
            raise RecoveryError("recovery journal is unreadable") from None
        if progress is not None and not isinstance(progress, dict):
            raise RecoveryError("recovery journal is unreadable")
        if progress is not None and progress.get("complete"):
            progress = None
        if progress is None:
            proposal = _external_proposal(
                plan_dir, session_id, repositories, event_path
            )
        else:
            proposal = progress["proposal"]
            completed_event = any(
                record.get("name")
                == events.WORKSPACE_EXTERNAL_CHANGES_ACKNOWLEDGED_EVENT
                and record.get("payload", {}).get("details", {}).get("acceptance")
                == proposal
                for record in _event_records(event_path)
            )
            _check_recovery_authority(proposal, event_path, completed=completed_event)
        resume = [
            "asl",
            "acknowledge-external-changes",
            str(plan_dir),
            "--session",
            session_id,
        ]
        for alias in repositories:
            resume.extend(["--repository", alias])
        if state_dir is not None:
            resume.extend(["--state-dir", str(state_dir)])
        resume.extend(
            [
                "--accept",
                proposal["evidence_digest"],
                "--reason",
                reason or (progress or {}).get("reason", "OPERATOR_REASON"),
                "--checkpoint-retained",
                "--json",
            ]
        )
        if accept is None:
            return dict(
                ok=True,
                accepted=False,
                **proposal,
                recovery_progress=progress,
                resume_argv=resume,
            )
        if accept != proposal["evidence_digest"]:
            raise RecoveryError("reviewed evidence changed; preview and review again")
        if progress is not None and completed_event:
            current = _capture_recovery(proposal)
            if current != progress["expected_snapshot"]:
                raise RecoveryError("evidence changed after completed acknowledgement")
            progress["complete"] = True
            durability.write_json_file(journal, progress, durable=True)
            return dict(
                ok=True, accepted=True, **proposal, checkpoints=progress["checkpoints"]
            )
        if proposal["remaining_blockers"]:
            raise RecoveryError("; ".join(proposal["remaining_blockers"]))
        if proposal["checkpoint_targets"] and not checkpoint_retained:
            raise RecoveryError("retained checkpoints require --checkpoint-retained")
        if progress is None:
            if (
                _external_proposal(plan_dir, session_id, repositories, event_path)
                != proposal
            ):
                raise RecoveryError("evidence changed during acceptance")
            message = run_commit_paused._paused_work_commit_message(
                session_label=f"session {session_id}",
                pause_reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                commit_mode="per-session",
                repair_attempt=None,
                repair_max_attempts=None,
                run_id=proposal["original_run_id"],
            )
            progress = dict(
                proposal=proposal,
                reason=reason,
                expected_snapshot=proposal["current_snapshot"],
                checkpoints={},
                in_flight=None,
                message=message,
                complete=False,
                resume_argv=resume,
            )
            durability.write_json_file(journal, progress, durable=True)

        def persist() -> None:
            durability.write_json_file(journal, progress, durable=True)

        try:
            current = _capture_recovery(proposal)
            if (
                progress["in_flight"] is not None
                and current != progress["expected_snapshot"]
            ):
                intent = progress["in_flight"]
                if (
                    current[intent["target"]["repository"]]["head"]
                    == intent["head_before"]
                ):
                    _reconcile_checkpoint_staging(progress, current)
                else:
                    _finish_checkpoint(progress, current)
                persist()
            if current != progress["expected_snapshot"]:
                raise RecoveryError(
                    "evidence changed during recovery; preserve and inspect it"
                )
            for index, target in enumerate(proposal["checkpoint_targets"], 1):
                if target["repository"] in progress["checkpoints"]:
                    continue
                _check_recovery_authority(proposal, event_path)
                if _capture_recovery(proposal) != progress["expected_snapshot"]:
                    raise RecoveryError("evidence changed before checkpoint")
                root = Path(target["root"])
                progress["in_flight"] = dict(
                    target=target,
                    head_before=current[target["repository"]]["head"],
                    content=_checkpoint_content(root, target["paths"]),
                )
                persist()
                artifacts = (
                    state_root
                    / "external-change-recovery"
                    / proposal["evidence_digest"]
                )
                message_file = run_commit_paused._write_paused_work_message(
                    artifacts, progress["message"]
                )
                result = run_commit._run_asl_commit(
                    cwd=root,
                    run_dir=artifacts,
                    commit_index=index,
                    session_id=session_id,
                    repository_key=target["repository"],
                    repository_alias=target["alias"],
                    staging_paths=tuple(
                        f"{target['alias']}:{p}" for p in target["paths"]
                    ),
                    generation=run_commit._CommitGeneration(message_file=message_file),
                )
                current = _capture_recovery(proposal)
                if (
                    current[target["repository"]]["head"]
                    != progress["in_flight"]["head_before"]
                ):
                    _finish_checkpoint(progress, current)
                    persist()
                if not result["success"]:
                    raise RecoveryError(
                        f"checkpoint failed in {target['repository']}; "
                        f"inspect {result['result_path']}"
                    )
            current = _capture_recovery(proposal)
            if current != progress["expected_snapshot"] or any(
                _dirty_paths(current[t["repository"]])
                for t in proposal["checkpoint_targets"]
            ):
                raise RecoveryError(
                    "checkpoint targets are not clean or unrelated evidence changed"
                )
            if _check_recovery_authority(proposal, event_path) != current:
                raise RecoveryError("evidence changed during final acknowledgement")
            events.append_event(
                event_path,
                events.Event(
                    name=events.WORKSPACE_EXTERNAL_CHANGES_ACKNOWLEDGED_EVENT,
                    payload=dict(
                        reason=progress["reason"],
                        details=dict(
                            session_id=session_id,
                            acceptance=proposal,
                            checkpoints=progress["checkpoints"],
                            final_snapshot=current,
                        ),
                    ),
                ),
            )
            progress["complete"] = True
            persist()
            return dict(
                ok=True, accepted=True, **proposal, checkpoints=progress["checkpoints"]
            )
        except (RecoveryError, OSError) as error:
            return recovery_failure(
                "acknowledge-external-changes",
                error,
                reason=RECOVERY_REASON_INTERRUPTED,
                checkpoints=progress["checkpoints"],
                resume_argv=resume,
            )
    finally:
        state_locks.release_lock(plan_dir, state_dir)


def acknowledge_external_changes_from_args(args: argparse.Namespace) -> int:
    try:
        result = acknowledge_external_changes(
            args.plan_dir,
            args.session,
            tuple(args.repository),
            state_dir=args.state_dir,
            accept=args.accept,
            reason=args.reason,
            checkpoint_retained=args.checkpoint_retained,
        )
    except (
        RecoveryError,
        plans_errors.PlanError,
        state.StateError,
        workspaces.WorkspaceError,
        repository_evidence.RepositoryEvidenceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        result = recovery_failure("acknowledge-external-changes", error)
    return print_recovery_result("acknowledge-external-changes", result)


def recovery_diagnostics(
    plan_dir: Path,
    session_id: str,
    guard: dict[str, Any],
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Offer explicit operator recovery without claiming independent authorship."""
    import shlex

    aliases = sorted(
        {
            v["alias"]
            for v in guard.get("violations", ())
            if isinstance(v.get("alias"), str)
            and v["alias"] not in guard.get("write_aliases", ())
        }
    )
    argv = [
        "asl",
        "acknowledge-external-changes",
        str(plan_dir),
        "--session",
        session_id,
    ]
    for alias in aliases:
        argv.extend(["--repository", alias])
    if state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    argv.append("--json")
    return dict(
        preview_command=shlex.join(argv) if aliases else None,
        acceptance=(
            "Review evidence; use --accept DIGEST --reason TEXT "
            "and --checkpoint-retained"
        ),
        **{
            "progress": None,
            **recovery_journal_projection(
                plan_dir, state_dir=state_dir, session_id=session_id, guard=guard
            ),
        },
    )


def _incident_run_id(event_path: Path, guard: dict[str, Any]) -> str | None:
    """The run whose recorded pause retained ``guard``, when it is on record.

    A guard that has not been published yet (the pause is being built right
    now) has no event, and its digest alone already tells it apart from any
    earlier incident.
    """
    try:
        lines = event_path.read_bytes().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError, RecursionError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        details = payload.get("details")
        if isinstance(details, dict) and details.get("workspace_write_guard") == guard:
            run_id = payload.get("run_id")
            return run_id if isinstance(run_id, str) else None
    return None


def recovery_journal_projection(
    plan_dir: Path,
    *,
    state_dir: str | Path | None = None,
    session_id: str | None = None,
    guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the plan-level recovery journal onto one incident.

    The journal outlives the incident it recovered, so reading it bare would
    report an earlier session's finished recovery as progress on whatever
    pause is current (ASI-157). ``progress`` is therefore only projected when
    the journal's proposal names this incident -- the same session, the same
    retained guard digest, and the same original run. With no current incident
    only an unfinished recovery is projected, since acknowledging resumes it
    whatever pause is current. Any other journal is reported as
    ``historical_recovery``, without its completion state.
    """
    state_root = state.plan_state_dir(plan_dir, state_dir)
    path = state_root / RECOVERY_FILE_NAME
    if not path.exists():
        return {}
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except ValueError, OSError, RecursionError:
        return {"progress": {"error": "recovery journal is unreadable"}}
    proposal = progress.get("proposal") if isinstance(progress, dict) else None
    if not isinstance(proposal, dict):
        return {"progress": {"error": "recovery journal has no proposal"}}
    if guard is None:
        current = not progress.get("complete")
    else:
        incident_run_id = _incident_run_id(state_root / events.EVENTS_FILE_NAME, guard)
        current = (
            proposal.get("session_id") == session_id
            and proposal.get("retained_guard_sha256") == _digest(guard)
            and (
                incident_run_id is None
                or proposal.get("original_run_id") == incident_run_id
            )
        )
    if current:
        return {"progress": progress}
    return {
        "historical_recovery": {
            "session_id": proposal.get("session_id"),
            "original_run_id": proposal.get("original_run_id"),
            "retained_guard_sha256": proposal.get("retained_guard_sha256"),
        }
    }
