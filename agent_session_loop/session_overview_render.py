"""Focused portion of agent_session_loop/session_overview.py."""

from .session_overview_running import *


def _running_row_payload(row: RunningRow) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "plan_dir": str(row.plan_dir),
        "state_dir": str(row.state_dir),
        "run_id": row.run_id,
        "harness": row.harness,
        "session_id": row.session_id,
        "turn_index": row.turn_index,
        "pid": row.pid,
        "started_at": row.started_at,
        "elapsed_seconds": row.elapsed_seconds,
        "idle_seconds": row.idle_seconds,
        "last_activity_at": row.last_activity_at,
        "lock_active": row.lock_active,
        "lock_stale": row.lock_stale,
        "check_index": row.check_index,
        "check_count": row.check_count,
        "check_name": row.check_name,
        "check_kind": row.check_kind,
        "cwd": row.cwd,
        "workspace_root": row.workspace_root,
        "registry_digest": row.registry_digest,
        "repositories": [dict(repository) for repository in row.repositories],
    }


def _recent_row_payload(row: RecentRow) -> dict[str, Any]:
    return {
        "plan_dir": str(row.plan_dir),
        "state_dir": str(row.state_dir),
        "run_id": row.run_id,
        "harness": row.harness,
        "session_id": row.session_id,
        "turn_index": row.turn_index,
        "status": row.status,
        "finished_at": row.finished_at,
        "duration_seconds": row.duration_seconds,
        "artifact_dir": str(row.artifact_dir),
        "session_kind": row.session_kind,
        "cwd": row.cwd,
        "workspace_root": row.workspace_root,
        "registry_digest": row.registry_digest,
        "repositories": [dict(repository) for repository in row.repositories],
    }


def _stale_lock_payload(lock: StaleLock) -> dict[str, Any]:
    return {
        "plan_dir": lock.plan_dir,
        "lock_path": str(lock.lock_path),
        "pid": lock.pid,
        "reason": lock.reason,
        "created_at": lock.created_at,
    }


def _skipped_payload(skipped: SkippedArtifact) -> dict[str, Any]:
    return {
        "path": str(skipped.path),
        "reason": skipped.reason,
    }


def build_sessions_payload(
    *,
    state_root_path: Path,
    show_running: bool,
    show_recent: bool,
    limit: int,
    running_rows: tuple[RunningRow, ...],
    recent_rows: tuple[RecentRow, ...],
    stale_locks: tuple[StaleLock, ...],
    skipped: tuple[SkippedArtifact, ...],
) -> dict[str, Any]:
    """Build the canonical `asl sessions` JSON payload."""
    counts = OverviewCounts(
        running=len(running_rows),
        recent=len(recent_rows),
        stale_locks=len(stale_locks),
        skipped=len(skipped),
    )
    return responses.success_payload(
        "sessions",
        state_root=str(state_root_path),
        filters={"running": show_running, "recent": show_recent},
        limit=limit,
        running=[_running_row_payload(row) for row in running_rows],
        recent=[_recent_row_payload(row) for row in recent_rows],
        stale_locks=[_stale_lock_payload(lock) for lock in stale_locks],
        skipped=[_skipped_payload(item) for item in skipped],
        counts=counts.as_dict(),
    )


def _format_int(value: int | None) -> str:
    return "?" if value is None else str(value)


def _print_running_section(
    payload: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    rows = payload["running"]
    if not rows:
        print("Running sessions: none")
        return
    print(f"Running sessions ({len(rows)}):")
    for row in rows:
        plan = plan_label(row["plan_dir"], verbose=verbose)
        session_id = row["session_id"] or "?"
        turn_index = _format_int(row["turn_index"])
        harness = row["harness"] or "unknown"
        pid = _format_int(row["pid"])
        check_suffix = ""
        workspace_suffix = ""
        if row.get("workspace_root"):
            repositories = row.get("repositories") or []
            visible, omitted = workspaces.bounded_repository_rows(repositories)
            aliases = [
                str(repository.get("alias"))
                for repository in visible
                if isinstance(repository, dict) and repository.get("alias")
            ]
            alias_text = ",".join(aliases)
            if omitted:
                alias_text += f",+{omitted}"
            workspace_suffix = f"  workspace {row['workspace_root']}" + (
                f" [{alias_text}]" if alias_text else ""
            )
        if row.get("kind") == "verification":
            check_index = row.get("check_index")
            check_count = row.get("check_count")
            if check_index is None:
                position = f"-/{check_count}" if check_count is not None else "-"
            else:
                position = (
                    f"{int(check_index) + 1}/{check_count}"
                    if check_count is not None
                    else str(int(check_index) + 1)
                )
            check_name = row.get("check_name") or "pending"
            check_suffix = f"  check {position} {check_name}"
        print(
            f"  {row['kind']:<12}"
            f"  started {format_local_time(row['started_at'])}"
            f"  elapsed {format_duration_seconds(row['elapsed_seconds'])}"
            f"  idle {format_duration_seconds(row['idle_seconds'])}"
            f"  pid {pid}"
            f"  harness {harness}"
            f"  session {session_id} turn {turn_index}"
            f"{check_suffix}"
            f"{workspace_suffix}"
            f"  plan {plan}"
        )


def _print_recent_section(
    payload: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    rows = payload["recent"]
    if not rows:
        print("Recent sessions: none")
        return
    print(f"Recent sessions ({len(rows)}):")
    for row in rows:
        plan = plan_label(row["plan_dir"], verbose=verbose)
        session_id = row["session_id"] or "?"
        harness = row["harness"] or "unknown"
        run_id = row["run_id"] or "unknown"
        status = row["status"] or "unknown"
        kind = row.get("session_kind")
        kind_label = f" ({kind})" if kind else ""
        workspace_suffix = (
            f"  workspace {row['workspace_root']}" if row.get("workspace_root") else ""
        )
        print(
            f"  finished {format_local_time(row['finished_at'])}"
            f"  dur {format_duration_seconds(row['duration_seconds'])}"
            f"  {status}"
            f"  harness {harness}"
            f"  session {session_id}{kind_label}"
            f"  run {run_id}"
            f"{workspace_suffix}"
            f"  plan {plan}"
        )


def _print_footer_warnings(
    payload: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    stale_locks = payload["stale_locks"]
    if stale_locks:
        print(f"Stale locks ({len(stale_locks)}):")
        for lock in stale_locks:
            plan = (
                plan_label(lock["plan_dir"], verbose=verbose)
                if lock["plan_dir"]
                else "unknown"
            )
            pid = _format_int(lock["pid"])
            print(
                f"  plan {plan}  pid {pid}  reason {lock['reason']}"
                f"  lock {lock['lock_path']}"
            )
    skipped = payload["skipped"]
    if skipped:
        print(f"Skipped artifacts ({len(skipped)}):")
        for item in skipped:
            print(f"  {item['path']}  reason {item['reason']}")


def _print_text_sessions(payload: dict[str, Any], *, verbose: bool) -> None:
    filters = payload["filters"]
    print(f"State root: {payload['state_root']}")
    if filters["running"]:
        _print_running_section(payload, verbose=verbose)
    if filters["recent"]:
        _print_recent_section(payload, verbose=verbose)
    _print_footer_warnings(payload, verbose=verbose)


def sessions_from_args(args: argparse.Namespace) -> int:
    """Render `asl sessions` output from CLI arguments."""
    if args.running and args.recent:
        show_running = True
        show_recent = True
    elif args.running:
        show_running = True
        show_recent = False
    elif args.recent:
        show_running = False
        show_recent = True
    else:
        show_running = True
        show_recent = True

    limit = args.limit if args.limit is not None else DEFAULT_RECENT_LIMIT
    if limit < 0:
        message = "--limit must be a non-negative integer"
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "sessions",
                    reason="input-error",
                    error=message,
                    paused=False,
                )
            )
        else:
            print(f"agent-session-loop sessions: {message}", file=sys.stderr)
        return 1

    try:
        resolved_state_root = state.state_root(args.state_dir)
    except OSError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "sessions",
                    reason="state-error",
                    error=str(error),
                    paused=False,
                    state_dir=str(args.state_dir) if args.state_dir else None,
                )
            )
        else:
            print(f"agent-session-loop sessions: {error}", file=sys.stderr)
        return 1

    running_rows: tuple[RunningRow, ...] = ()
    stale_locks: tuple[StaleLock, ...] = ()
    recent_rows: tuple[RecentRow, ...] = ()
    skipped_combined: list[SkippedArtifact] = []

    try:
        if show_running:
            running_rows, stale_locks, running_skipped = scan_running_rows(
                args.state_dir
            )
            skipped_combined.extend(running_skipped)
        if show_recent:
            recent_rows, recent_skipped = scan_recent_rows(args.state_dir, limit=limit)
            skipped_combined.extend(recent_skipped)
    except OSError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "sessions",
                    reason="state-error",
                    error=str(error),
                    paused=False,
                    state_root=str(resolved_state_root),
                )
            )
        else:
            print(f"agent-session-loop sessions: {error}", file=sys.stderr)
        return 1

    payload = build_sessions_payload(
        state_root_path=resolved_state_root,
        show_running=show_running,
        show_recent=show_recent,
        limit=limit,
        running_rows=running_rows,
        recent_rows=recent_rows,
        stale_locks=stale_locks,
        skipped=tuple(skipped_combined),
    )

    if args.json:
        responses.print_json(payload)
    else:
        _print_text_sessions(payload, verbose=args.verbose)
    return 0


__all__ = ["argparse", "json", "sys", "Iterator", "dataclass", "UTC", "datetime", "Path", "Any", "plans_errors", "plans_files", "plans_metadata", "plans_registry", "responses", "state", "state_locks", "state_payloads", "workspaces", "format_duration_seconds", "DEFAULT_RECENT_LIMIT", "RUNNING_KIND", "RECENT_KIND", "STATE_JSON_NAME", "LOCK_JSON_NAME", "RUNS_DIR_NAME", "RESULT_JSON_NAME", "TURN_PREFIX", "RunningRow", "RecentRow", "_coerce_nonempty_str", "_workspace_fields", "StaleLock", "SkippedArtifact", "OverviewCounts", "parse_iso_timestamp", "format_local_time", "plan_basename_label", "plan_label", "running_sort_key", "recent_sort_key", "_read_json_object", "_coerce_int", "_coerce_float", "_read_state_dir_plan_dir_fallbacks", "_resolve_recent_plan_dir", "_resolve_session_id", "_build_recent_row", "_iter_state_plan_dirs", "_iter_run_dirs", "_iter_turn_result_paths", "scan_recent_rows", "_coerce_pid", "_running_lock_metadata", "_build_running_row", "scan_running_rows", "_running_row_payload", "_recent_row_payload", "_stale_lock_payload", "_skipped_payload", "build_sessions_payload", "_format_int", "_print_running_section", "_print_recent_section", "_print_footer_warnings", "_print_text_sessions", "sessions_from_args"]  # fmt: skip
