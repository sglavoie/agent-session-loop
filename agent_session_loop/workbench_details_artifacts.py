# ruff: noqa: F403, F405
from .workbench_details_models import *
from .workbench_details_models import _JSONDict
from .workbench_details_open import *


def artifact_descriptor(path: Path | None) -> _JSONDict:
    """Describe one artifact without reading it."""
    if path is None:
        return {"path": None, "exists": False, "kind": None, "size": None}
    try:
        stat = path.stat()
    except FileNotFoundError, NotADirectoryError:
        return {"path": str(path), "exists": False, "kind": None, "size": None}
    except OSError as error:
        return {
            "path": str(path),
            "exists": False,
            "kind": None,
            "size": None,
            "error": str(error),
        }
    if path.is_dir():
        return {
            "path": str(path),
            "exists": True,
            "kind": "directory",
            "size": None,
        }
    return {
        "path": str(path),
        "exists": True,
        "kind": "file",
        "size": stat.st_size,
    }


def read_artifacts(reader: PlanReader) -> _JSONDict:
    """Descriptors for every artifact name this plan can be asked for.

    Every key is a name :meth:`asl_workbench.details.DetailScope.artifact`
    accepts, so the listing is also the request vocabulary: a caller reads one
    of these descriptors and asks for it back by name.
    """
    table = artifact_paths(reader.summary, reader.plan_dir)
    return {
        "plan_dir": str(reader.plan_dir),
        "artifacts": {
            name: artifact_descriptor(table[name]) for name in ARTIFACT_NAMES
        },
        "run_id": reader.summary.run_id,
        "harness": reader.summary.harness,
    }


# ---------------------------------------------------------------------------
# State, events, verification


def read_state(reader: PlanReader) -> _JSONDict:
    """The runner state payload, the session table, and the workspace."""
    try:
        snapshot = plans_snapshot.inspect_plan(reader.plan_dir)
    except plans_errors.PlanError as error:
        raise DetailError(
            details.REASON_MALFORMED_PLAN,
            str(error),
            details={"plan_dir": str(reader.plan_dir)},
        ) from error
    payload = state_payloads._runner_state_payload(
        reader.state_path,
        lock_info=read_lock(reader),
    )
    try:
        workspace = workbench_workspace._workspace_projection(
            reader.plan_dir, runtime=payload
        )
    except plans_errors.PlanError, OSError, ValueError:
        workspace = {}
    next_pending = snapshot.next_pending
    return {
        "plan_dir": str(reader.plan_dir),
        "state_path": str(reader.state_path),
        "state": payload,
        "sessions": [state_payloads._session_payload(s) for s in snapshot.sessions],
        "counts": state_payloads._status_counts(snapshot),
        "next_pending_id": next_pending.id if next_pending is not None else None,
        "workspace": workspace,
        **workbench_workspace._workspace_payload(workspace, cwd=payload.get("cwd")),
    }


def read_verification(reader: PlanReader) -> _JSONDict:
    """The active verification's position, read off the same state file."""
    runner_state = state_payloads._runner_state_payload(
        reader.state_path,
        lock_info=read_lock(reader),
    )
    return {
        "plan_dir": str(reader.plan_dir),
        "state_path": str(reader.state_path),
        "active_verification": runner_state.get("active_verification"),
        "run_id": runner_state.get("run_id"),
        "harness": runner_state.get("harness"),
    }


def _event_records(reader: PlanReader) -> list[dict[str, Any]]:
    try:
        return list(tail._read_event_records(reader.events_path))
    except tail.TailError as error:
        raise DetailError(
            details.REASON_OS_ERROR,
            str(error),
            details={"plan_dir": str(reader.plan_dir)},
        ) from error


def _selected_run_id(
    reader: PlanReader,
    records: list[dict[str, Any]],
    requested: str | None,
) -> str | None:
    if requested:
        return requested
    try:
        state_payload = tail._read_json_file(reader.state_path)
    except tail.TailError:
        state_payload = None
    return tail._latest_run_id(state_payload, records)


def read_events(
    reader: PlanReader,
    *,
    offset: int = 0,
    limit: int = details.DEFAULT_EVENT_LIMIT,
    run_id: str | None = None,
) -> _JSONDict:
    """One page of the selected run's events, with the page arithmetic shared.

    Pagination is :func:`asl_workbench.details.paginate` rather than a slice
    computed here, so a remote consumer paging over a connector sees the same
    ``next_offset`` rule -- ``None`` at the end, never an offset past it -- as
    the local browser does.
    """
    records = _event_records(reader)
    selected = _selected_run_id(reader, records, run_id)
    run_records = tail._event_records_for_run(records, selected)
    sliced, page = details.paginate(run_records, offset=offset, limit=limit)
    return {
        "plan_dir": str(reader.plan_dir),
        "events_path": str(reader.events_path),
        "run_id": selected,
        **page.to_payload(),
        "events": sliced,
    }


def read_commits(reader: PlanReader, *, run_id: str | None = None) -> _JSONDict:
    """Commit rows for the selected run, paired from its event journal."""
    records = _event_records(reader)
    selected = _selected_run_id(reader, records, run_id)
    run_records = tail._event_records_for_run(records, selected)
    return {
        "plan_dir": str(reader.plan_dir),
        "events_path": str(reader.events_path),
        "run_id": selected,
        "commits": build_commit_rows(run_records),
    }


def build_commit_rows(records: Sequence[Mapping[str, Any]]) -> list[_JSONDict]:
    """Pair commit_started/commit_finished events into commit rows."""
    started: dict[int, _JSONDict] = {}
    rows: list[_JSONDict] = []
    for record in records:
        name = record.get("name")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        index = payload.get("commit_index")
        if not isinstance(index, int):
            continue
        if name == events_module.COMMIT_STARTED_EVENT:
            started[index] = {
                "commit_index": index,
                "scope": payload.get("scope"),
                "session_id": payload.get("session_id") or payload.get("session"),
                "run_id": payload.get("run_id"),
                "started_at": record.get("created_at"),
                "started_payload": payload,
                "finished_at": None,
                "status_result": None,
                "commit_sha": None,
                "duration_seconds": None,
                "finished_payload": None,
                "stdout_path": payload.get("stdout_path"),
                "stderr_path": payload.get("stderr_path"),
                "result_path": payload.get("result_path"),
            }
        elif name == events_module.COMMIT_FINISHED_EVENT:
            row = started.pop(index, None)
            if row is None:
                row = {
                    "commit_index": index,
                    "scope": payload.get("scope"),
                    "session_id": payload.get("session_id") or payload.get("session"),
                    "run_id": payload.get("run_id"),
                    "started_at": None,
                    "started_payload": None,
                    "stdout_path": payload.get("stdout_path"),
                    "stderr_path": payload.get("stderr_path"),
                    "result_path": payload.get("result_path"),
                }
            row["finished_at"] = record.get("created_at")
            row["status_result"] = payload.get("status_result")
            row["commit_sha"] = payload.get("commit_sha")
            row["duration_seconds"] = payload.get("duration_seconds")
            row["finished_payload"] = payload
            row["stdout_path"] = payload.get("stdout_path") or row.get("stdout_path")
            row["stderr_path"] = payload.get("stderr_path") or row.get("stderr_path")
            row["result_path"] = payload.get("result_path") or row.get("result_path")
            rows.append(row)
    # Any started-without-finished commits still in flight surface as rows.
    rows.extend(started.values())
    rows.sort(key=lambda row: row.get("commit_index", 0))
    return rows


# ---------------------------------------------------------------------------
# Bounded text and log windows
