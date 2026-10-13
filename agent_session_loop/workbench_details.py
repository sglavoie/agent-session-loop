"""The Mac provider behind the portable detail reads.

``asl_workbench.details`` says what a detail read is allowed to name and how
wide a window it may take; this module is the half that opens the files. It is
to :mod:`asl_workbench.details` what :mod:`.workbench_catalog` is to
``asl_workbench.catalog``, and the same two properties hold:

* **Nothing here reclaims.** Every plan is classified through
  :func:`.workbench_catalog.build_summary`, every lock is read with
  ``reclaim=False``, and the recovery card and question context are built the
  same way. Before this module, ``GET /api/state``, ``GET /api/verification``,
  ``GET /api/recovery`` and ``GET /api/questions`` still reclaimed a stale plan
  lock as a side effect of describing it -- so opening a plan's detail panel
  repaired the corpus, at whatever rate a dashboard happened to refresh.
  ``observational-reads`` in ``asl_workbench.changes`` records the target.

* **Nothing here reads outside the selected plan.** A read names an artifact
  and :class:`asl_workbench.details.DetailScope` resolves it against what that
  plan and run published. The compatibility form that still accepts a path
  goes through the scope's containment check, so a path is honoured only when
  the plan already owns it.

The payload each ``read_*`` returns is the body of one existing Workbench
detail response, field for field. Keeping them identical is the point: the
routes become request parsing plus a call, the browser sees what it saw
before, and a remote connector serving the same payloads cannot answer with
less than the local one does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asl_workbench import catalog as portable_catalog
from asl_workbench import details
from asl_workbench.catalog import PlanDetail
from asl_workbench.details import DetailError, DetailScope, Window

from . import (
    artifacts,
    intervention,
    plans_errors,
    plans_files,
    plans_models,
    plans_progress,
    plans_snapshot,
    qna,
    state,
    state_locks,
    state_payloads,
    tail,
    workbench,
    workbench_catalog,
    workbench_recovery,
    workbench_workspace,
)
from . import events as events_module

_JSONDict = dict[str, Any]

#: The artifact table, in the order the artifact listing publishes it. Two of
#: the ten are not summary fields: ``final_review_artifact_path`` is derived
#: from the plan directory, and every other entry is whatever the plan's own
#: classification resolved.
ARTIFACT_NAMES: tuple[str, ...] = details.ARTIFACT_NAMES


# ---------------------------------------------------------------------------
# Opening a plan


@dataclass(frozen=True)
class PlanReader:
    """One plan opened for reading, classified without repairing it.

    :attr:`scope` is the portable half -- the artifact table and containment
    rule a read consults -- and :attr:`summary` is the Mac-side classification
    it was built from, kept so a read that needs a field the scope does not
    carry (``harness``, ``review_resume``, ``cwd``) does not classify the plan
    a second time.
    """

    plan_dir: Path
    plan_state_dir: Path
    summary: workbench.WorkbenchSummary
    scope: DetailScope
    state_dir: Path | None = None

    @property
    def state_path(self) -> Path:
        return self.plan_state_dir / events_module.STATE_FILE_NAME

    @property
    def events_path(self) -> Path:
        return self.plan_state_dir / events_module.EVENTS_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self.plan_state_dir / state.LOCK_FILE_NAME

    def detail(self) -> PlanDetail:
        """The private half of this plan, for an online search or a fetch."""
        return workbench_catalog.record_for_summary(
            self.summary,
            pinned="registry" in self.summary.sources,
        ).detail

    def read(self, kind: str, payload: Mapping[str, Any]) -> details.DetailRead:
        """Wrap one payload in the envelope both providers answer with."""
        return details.build_read(kind, self.scope, payload)


def canonical_plan_dir(plan_dir: str | Path) -> Path:
    """Canonicalize a caller-supplied plan directory, or refuse it."""
    try:
        return plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise DetailError(
            details.REASON_INVALID_PLAN,
            f"invalid plan_dir: {error}",
            details={"plan_dir": str(plan_dir)},
        ) from error


def artifact_paths(
    summary: workbench.WorkbenchSummary, plan_dir: Path
) -> dict[str, Path | None]:
    """Every artifact this plan and run published, by name.

    ``final_review_artifact_path`` is the one entry the summary does not carry:
    ``final-review.json`` lives beside the plan rather than in its run state,
    so it is derived here. Its descriptor still reports whether it exists, so
    naming it is not a claim that it does.
    """
    return {
        "state_path": summary.state_path,
        "events_path": summary.events_path,
        "progress_path": summary.progress_path,
        "final_review_path": summary.final_review_path,
        "final_review_artifact_path": (
            plan_dir / workbench.FINAL_REVIEW_ARTIFACT_FILE_NAME
        ),
        "artifact_dir": summary.artifact_dir,
        "stdout_path": summary.stdout_path,
        "stderr_path": summary.stderr_path,
        "last_message_path": summary.last_message_path,
        "result_path": summary.result_path,
    }


def build_scope(
    summary: workbench.WorkbenchSummary,
    plan_dir: Path,
    *,
    plan_state_dir: Path,
) -> DetailScope:
    """The portable scope for one classified plan.

    The identities are the catalog's own :func:`asl_workbench.catalog.plan_key`
    and :func:`asl_workbench.catalog.run_key`, so a detail read and a cached
    catalog row name the same plan and the same run by the same string -- which
    is what lets :meth:`asl_workbench.details.DetailScope.check_run` tell a
    stale request from a current one.
    """
    key = portable_catalog.plan_key(str(plan_dir))
    return DetailScope(
        plan_key=key,
        plan_dir=str(plan_dir),
        plan_state_dir=str(plan_state_dir),
        artifacts={
            name: (str(path) if path is not None else None)
            for name, path in artifact_paths(summary, plan_dir).items()
        },
        run_key=portable_catalog.run_key(key, summary.run_id),
        run_id=summary.run_id,
    )


def open_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> PlanReader:
    """Classify one plan for reading, reclaiming nothing and repairing nothing.

    The single entry point for every detail read below, which is what makes
    "detail inspection is observational" checkable in one place rather than
    remembered at ten call sites.
    """
    canonical = canonical_plan_dir(plan_dir)
    resolved_state_dir = Path(state_dir) if state_dir is not None else None
    plan_state = state.plan_state_dir(canonical, resolved_state_dir)
    discovered = workbench.DiscoveredPlan(plan_dir=canonical, exists=canonical.is_dir())
    summary = workbench_catalog.build_summary(discovered, state_dir=resolved_state_dir)
    return PlanReader(
        plan_dir=canonical,
        plan_state_dir=plan_state,
        summary=summary,
        scope=build_scope(summary, canonical, plan_state_dir=plan_state),
        state_dir=resolved_state_dir,
    )


def read_lock(reader: PlanReader) -> state.LockInfo | None:
    """Classify this plan's lock without removing it.

    ``reclaim=False`` is the whole point: the shared lock reader's default is
    to delete a lock it decides is stale, and a detail panel asking what the
    lock says must not be the thing that releases it. ``POST /api/unlock`` is
    where a release is asked for.
    """
    return state_locks._read_lock_path(reader.lock_path, reader.plan_dir, reclaim=False)


# ---------------------------------------------------------------------------
# Plan and artifact reads


def read_plan(
    plan_dir: str | Path,
    *,
    registry: workbench.WorkbenchRegistry,
    state_dir: str | Path | None = None,
) -> _JSONDict:
    """One plan's triage summary, carrying its registry pin when it has one.

    The only read that consults the registry: a pin's label and launch profile
    belong to the plan the operator pinned, and a summary built without them
    would answer a deep link with a plan that had lost its own name.
    """
    canonical = canonical_plan_dir(plan_dir)
    pin = next((p for p in registry.pins if p.plan_dir == str(canonical)), None)
    discovered = workbench.DiscoveredPlan(
        plan_dir=canonical,
        sources=("registry",) if pin is not None else (),
        label=pin.label if pin is not None else None,
        profile=pin.profile if pin is not None else None,
        exists=canonical.is_dir(),
    )
    summary = workbench_catalog.build_summary(discovered, state_dir=state_dir)
    payload = summary.to_payload()
    # The queue row names a plan by its manifest title; the detail header has
    # to as well, or opening a plan retitles it with its directory name.
    payload["title"] = workbench_catalog.read_plan_title(canonical)
    return {"plan": payload}


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


def is_gzip_artifact(path: Path) -> bool:
    """Return True when ``path`` names a gzip artifact written by this project.

    Gzip verification artifacts always use the ``.gz`` suffix
    (``{check}-stdout.gz`` etc.), so suffix detection is deterministic and lets
    the bounded-window log APIs serve plain and gzip logs uniformly.
    """
    return path.suffix == ".gz"


def _window_payload(
    path: Path,
    data: bytes,
    *,
    size: int,
    read_offset: int,
    tail_lines: int | None,
) -> _JSONDict:
    text = data.decode("utf-8", errors="replace")
    truncated_head = read_offset > 0
    next_offset = read_offset + len(data)
    truncated_tail = next_offset < size
    if tail_lines is not None:
        lines = text.splitlines()
        if tail_lines == 0:
            text = ""
        elif len(lines) > tail_lines:
            text = "\n".join(lines[-tail_lines:])
            truncated_head = True
    return {
        "path": str(path),
        "exists": True,
        "size": size,
        "offset": read_offset,
        "next_offset": next_offset if truncated_tail else None,
        "truncated_head": truncated_head,
        "truncated_tail": truncated_tail,
        "text": text,
    }


def _window_error(
    path: Path,
    error: OSError | artifacts.ArtifactError,
    *,
    exists: bool,
    window: Window,
) -> _JSONDict:
    return {
        "path": str(path),
        "exists": exists,
        "error": str(error),
        "size": None,
        "offset": window.offset or 0,
        "next_offset": None,
        "truncated_head": False,
        "truncated_tail": False,
        "text": "",
    }


def _read_gzip_window(path: Path, window: Window) -> _JSONDict | None:
    """Bounded-window read over a gzip artifact.

    Gzip cannot be seeked, so the artifact is decompressed fully and the same
    offset/tail/max_bytes windowing is applied to the decompressed bytes. The
    payload shape matches :func:`read_log_window` so callers are agnostic to
    the on-disk encoding.
    """
    try:
        data = artifacts.read_gzip(path)
    except artifacts.ArtifactError as error:
        if not path.exists():
            return None
        return _window_error(path, error, exists=True, window=window)
    size = len(data)
    read_offset = (
        window.offset if window.offset is not None else max(0, size - window.max_bytes)
    )
    read_offset = min(read_offset, size)
    return _window_payload(
        path,
        data[read_offset : read_offset + window.max_bytes],
        size=size,
        read_offset=read_offset,
        tail_lines=window.tail_lines,
    )


def read_log_window(path: Path, window: Window) -> _JSONDict | None:
    """Read a bounded slice of a log file (plain text or gzip artifact).

    - ``offset`` mode: byte offset into the file; reads up to ``max_bytes``.
    - ``tail_lines`` mode: read the trailing ``max_bytes`` and keep the last
      ``tail_lines`` lines (the most-recent slice operators care about).
    - When neither is set: read from the end like ``tail -c max_bytes``.

    Returns ``None`` when the file is absent, which the caller answers as a
    404 rather than as an empty but successful window.
    """
    if is_gzip_artifact(path):
        return _read_gzip_window(path, window)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as error:
        return _window_error(path, error, exists=False, window=window)
    read_offset = (
        window.offset if window.offset is not None else max(0, size - window.max_bytes)
    )
    read_offset = min(read_offset, size)
    try:
        with path.open("rb") as handle:
            handle.seek(read_offset)
            raw = handle.read(window.max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — treat as a
        # graceful 404 rather than surfacing the race as a half-filled
        # payload.
        return None
    except OSError as error:
        payload = _window_error(path, error, exists=True, window=window)
        payload["size"] = size
        payload["offset"] = read_offset
        return payload
    return _window_payload(
        path,
        raw,
        size=size,
        read_offset=read_offset,
        tail_lines=window.tail_lines,
    )


def read_bounded_text(path: Path, *, max_bytes: int) -> _JSONDict | None:
    """Read up to ``max_bytes`` from the start of ``path`` as text.

    Gzip artifacts are decompressed transparently before the byte bound is
    applied so plain and gzip logs read through the same API.
    """
    if is_gzip_artifact(path):
        try:
            data = artifacts.read_gzip(path)
        except artifacts.ArtifactError as error:
            if not path.exists():
                return None
            return {
                "path": str(path),
                "exists": True,
                "size": None,
                "error": str(error),
                "truncated": False,
                "text": "",
            }
        raw = data[:max_bytes]
        return {
            "path": str(path),
            "exists": True,
            "size": len(data),
            "truncated": len(raw) < len(data),
            "text": raw.decode("utf-8", errors="replace"),
        }
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": False,
            "size": None,
            "error": str(error),
            "truncated": False,
            "text": "",
        }
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — surface as a
        # clean 404 so the operator UI does not see a fake successful
        # zero-byte response.
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": True,
            "size": size,
            "error": str(error),
            "truncated": False,
            "text": "",
        }
    return {
        "path": str(path),
        "exists": True,
        "size": size,
        "truncated": len(raw) < size,
        "text": raw.decode("utf-8", errors="replace"),
    }


def _missing(path: Path, description: str) -> DetailError:
    return DetailError(
        details.REASON_NOT_FOUND,
        f"{description} does not exist: {path}",
        details={"path": str(path)},
    )


def read_logs(
    reader: PlanReader,
    *,
    stream: str | None = None,
    artifact: str | None = None,
    path: str | None = None,
    window: Window | None = None,
) -> _JSONDict:
    """A bounded window over one of this plan's logs.

    The target is chosen by :meth:`asl_workbench.details.DetailScope.log_target`,
    which is the only place that decides whether a request may reach a file:
    a named artifact is resolved against what this plan published, and a
    supplied path is honoured only when this plan's state directory contains
    it.
    """
    resolved = Path(
        reader.scope.log_target(stream=stream, artifact=artifact, path=path)
    )
    payload = read_log_window(resolved, window or Window())
    if payload is None:
        raise _missing(resolved, "log file")
    return {"plan_dir": str(reader.plan_dir), **payload}


def read_last_message(
    reader: PlanReader,
    *,
    max_bytes: int = details.DEFAULT_LAST_MESSAGE_BYTES,
) -> _JSONDict:
    """The harness's last message for this plan's active turn, bounded."""
    try:
        resolved = Path(reader.scope.artifact("last_message_path", readable=True))
    except DetailError as error:
        if error.reason != details.REASON_NOT_FOUND:
            raise
        raise DetailError(
            details.REASON_NOT_FOUND,
            "no active last-message file for plan",
            details={"plan_dir": str(reader.plan_dir)},
        ) from error
    payload = read_bounded_text(resolved, max_bytes=max_bytes)
    if payload is None:
        raise _missing(resolved, "last-message file")
    return {"plan_dir": str(reader.plan_dir), **payload}


def read_result(reader: PlanReader) -> _JSONDict:
    """The active turn's ``result.json``, parsed."""
    try:
        resolved = Path(reader.scope.artifact("result_path", readable=True))
    except DetailError as error:
        if error.reason != details.REASON_NOT_FOUND:
            raise
        raise DetailError(
            details.REASON_NOT_FOUND,
            "no active result.json for plan",
            details={"plan_dir": str(reader.plan_dir)},
        ) from error
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise _missing(resolved, "result file") from error
    except OSError as error:
        raise DetailError(
            details.REASON_OS_ERROR, str(error), details={"path": str(resolved)}
        ) from error
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError as error:
        raise DetailError(
            details.REASON_OS_ERROR,
            f"result file is not valid JSON: {error}",
            details={"path": str(resolved)},
        ) from error
    return {
        "plan_dir": str(reader.plan_dir),
        "path": str(resolved),
        "result": parsed,
    }


# ---------------------------------------------------------------------------
# Progress and final review


def progress_entry_payload(entry: plans_models.ProgressEntry) -> _JSONDict:
    return {
        "session_id": entry.session_id.value,
        "status": entry.status,
        "completed_at": entry.completed_at,
        "files_modified": list(entry.files_modified),
        "key_decisions": entry.key_decisions,
        "discovered_facts": list(entry.discovered_facts),
        "open_questions": list(entry.open_questions),
        "blockers": entry.blockers,
    }


def read_progress(reader: PlanReader) -> _JSONDict:
    """The plan's progress journal, parsed into entries."""
    path = plans_progress.progress_path(reader.plan_dir)
    try:
        entries = plans_progress.read_progress_entries(reader.plan_dir)
    except plans_errors.PlanError as error:
        raise DetailError(
            details.REASON_MALFORMED_PLAN,
            str(error),
            details={"plan_dir": str(reader.plan_dir), "path": str(path)},
        ) from error
    return {
        "plan_dir": str(reader.plan_dir),
        "path": str(path),
        "exists": path.is_file(),
        "entries": [progress_entry_payload(entry) for entry in entries],
    }


def read_final_review(
    reader: PlanReader,
    *,
    max_bytes: int = details.DEFAULT_FINAL_REVIEW_BYTES,
) -> _JSONDict:
    """``final-review.md`` bounded, plus ``final-review.json`` when it parses.

    The markdown falls back to the plan's canonical ``final-review.md`` when
    the summary resolved none, so asking a plan that has not been reviewed
    gets a 404 naming the file it would be rather than a refusal naming an
    artifact vocabulary the operator did not use.
    """
    resolved = reader.scope.artifacts.get("final_review_path")
    path = (
        Path(resolved)
        if resolved is not None
        else reader.plan_dir / workbench.FINAL_REVIEW_FILE_NAME
    )
    payload = read_bounded_text(path, max_bytes=max_bytes)
    if payload is None:
        raise _missing(path, "final-review file")
    artifact_path = Path(reader.scope.artifact("final_review_artifact_path"))
    structured_review = None
    structured_error = None
    try:
        if artifact_path.is_file() and artifact_path.stat().st_size <= max_bytes:
            decoded = json.loads(artifact_path.read_text(encoding="utf-8"))
            if isinstance(decoded, dict):
                structured_review = decoded
            else:
                structured_error = "final-review.json did not contain an object"
    except (OSError, json.JSONDecodeError) as error:
        structured_error = str(error)
    try:
        workspace = workbench_workspace._workspace_projection(reader.plan_dir)
    except plans_errors.PlanError, OSError, ValueError:
        workspace = {}
    return {
        "plan_dir": str(reader.plan_dir),
        "artifact_path": str(artifact_path),
        "structured_review": structured_review,
        "structured_error": structured_error,
        "review_resume": reader.summary.review_resume,
        "workspace": workspace,
        **workbench_workspace._workspace_payload(workspace, cwd=reader.summary.cwd),
        **payload,
    }


# ---------------------------------------------------------------------------
# Questions and recovery


#: Envelope keys ``qna.questions_payload`` adds for its own command. The
#: Workbench re-emits the rest under its own envelope, so the resolver prompt
#: sees one ``command`` value whichever route it arrived through.
_QNA_ENVELOPE_KEYS = frozenset({"ok", "command", "reason", "paused"})


def read_questions(
    plan_dir: str | Path,
    *,
    question_id: str | None = None,
    state_dir: str | Path | None = None,
) -> _JSONDict:
    """One selected question context, classified without reclaiming.

    ``reclaim=False``: loading the resolver prompt is how an operator finds out
    a run is waiting, and finding out must not be what releases the lock the
    waiting run is holding.
    """
    try:
        facts = intervention.inspect_run(plan_dir, state_dir=state_dir)
        if facts.run_active and facts.context_kind == "waiting":
            selected_question = question_id or facts.question_id
            if selected_question is None:
                raise intervention.InterventionError(
                    intervention.REASON_NO_OPEN_QUESTION,
                    "no open question is waiting for a reply",
                )
            resolved_question_id, context_kind, body = intervention.question_body(
                plan_dir,
                question_id=selected_question,
                state_dir=state_dir,
            )
            intervention.enforce_body_bound(body)
            return {
                "context_kind": context_kind,
                "run_id": facts.run_id,
                "question_id": resolved_question_id,
                "actions": list(intervention.permitted_actions(facts)),
                "action_token": intervention.action_token(facts),
                "last_message": body,
                "rendered_prompt": body,
            }
    except intervention.InterventionError as error:
        if question_id is not None:
            raise DetailError(
                details.REASON_NOT_FOUND,
                error.message,
                status=404,
                details={"plan_dir": str(plan_dir), **error.details},
            ) from error
        # The Q&A reader below owns the established paused-run error contract.
        # A live-question inspection failure must not replace it with a second
        # vocabulary when the plan is actually paused.
        facts = None

    try:
        context = qna.load_qna_context(plan_dir, state_dir=state_dir, reclaim=False)
    except qna.QnAError as error:
        raise DetailError(
            error.reason,
            error.error,
            status=404 if error.reason == qna.REASON_NO_PAUSED_RUN else 400,
            details={"plan_dir": str(plan_dir), **error.details},
        ) from error
    except plans_errors.PlanError as error:
        raise DetailError(
            details.REASON_MALFORMED_PLAN,
            str(error),
            status=400,
            details={"plan_dir": str(plan_dir)},
        ) from error
    payload = qna.questions_payload(context)
    answer = {
        key: value for key, value in payload.items() if key not in _QNA_ENVELOPE_KEYS
    }
    if facts is None:
        try:
            facts = intervention.inspect_run(plan_dir, state_dir=state_dir)
        except intervention.InterventionError:
            facts = None
    if facts is not None:
        answer.update(
            {
                "context_kind": context.context_kind,
                "actions": list(intervention.permitted_actions(facts)),
                "action_token": intervention.action_token(facts),
            }
        )
    return answer


def read_recovery(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> _JSONDict:
    """The recovery card for one plan: evidence, artifacts, and actions.

    ``reclaim=False`` on the recovery builder. The recovery view is the one an
    operator opens precisely because something looks wrong, so a reclaim here
    would repair the corpus at the exact moment they were trying to read what
    state it was in -- and would change the diagnosis a second reader gets.
    Releasing a lock stays ``POST /api/unlock``, which the card itself offers
    as an action.
    """
    canonical = canonical_plan_dir(plan_dir)
    if not canonical.is_dir():
        raise DetailError(
            details.REASON_INVALID_PLAN,
            f"plan directory does not exist: {canonical}",
            details={"plan_dir": str(canonical)},
        )
    try:
        snapshot = plans_snapshot.inspect_plan(canonical)
    except plans_errors.PlanError as error:
        raise DetailError(
            details.REASON_MALFORMED_PLAN,
            str(error),
            details={"plan_dir": str(canonical)},
        ) from error
    discovered = workbench.DiscoveredPlan(plan_dir=snapshot.plan_dir, exists=True)
    recovery = workbench_recovery.build_workbench_recovery(
        discovered,
        state_dir=state_dir,
        reclaim=False,
    )
    return {
        "plan_dir": str(snapshot.plan_dir),
        "recovery": recovery.to_payload(),
        "artifacts": [artifact.to_payload() for artifact in recovery.artifacts],
        "run_id": recovery.run_id,
        "session_id": recovery.session_id,
        "turn_index": recovery.turn_index,
        "commit_index": recovery.commit_index,
    }


# ---------------------------------------------------------------------------
# Online search over private detail


def search_plans(
    query: object,
    *,
    state_dir: str | Path | None = None,
    registry: workbench.WorkbenchRegistry | None = None,
    plan_roots: Sequence[str | Path] = (),
    include_default_dirs: bool = True,
) -> list[_JSONDict]:
    """Search every visible plan's private detail, online and unretained.

    This is the search the Workbench's browser does today over
    ``searchable_text``, moved behind a service so a remote cockpit can ask for
    it instead of being handed the corpus. Every field it reads -- the plan
    path, the operator's pin label, the pause message, the last-message
    preview -- is on ``asl_workbench.catalog.PRIVATE_FIELDS``, so a hit is an
    answer to one question rather than a row to retain: nothing here is
    reachable from :class:`asl_workbench.catalog.CatalogEntry`, which carries
    its own cache-safe ``search_text``.

    One non-reclaiming catalog scan backs it, so searching leaves the corpus
    exactly as searching the queue does -- unchanged.
    """
    scan = workbench_catalog.scan(
        state_dir=state_dir,
        registry=registry,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    hits = details.search_details(
        (record.detail for record in scan.catalog.records), query
    )
    return [details.search_payload(detail) for detail in hits]
