"""Answering a remote detail read against this Mac's ASL corpus.

:mod:`asl_workbench.remote_reads` is the side that asks; this is the side that
looks. It is the only ASL-aware code the connector's correlated lane ever
reaches, and it exists so that
:class:`asl_workbench.connector_protocols.DetailResponder` can stay a one-method
protocol with no idea what a plan directory is.

Three translations happen here and nowhere else:

* **A key becomes a directory.** The request names a ``plan_key``, which is
  :func:`asl_workbench.catalog.plan_key` -- a digest of a path, not a path. The
  far end has never been told where anything lives and cannot be: a request
  that could name a directory would be a request that could name
  ``/etc/shadow``, and no containment rule applied afterwards would make that
  safe. :class:`~.workbench_remote_reads_index.PlanIndex` resolves the key against a
  real discovery pass, so
  the only directories reachable from the wire are the ones this Mac's own
  discovery already publishes.

* **A local payload becomes a transport payload.** The readers in
  :mod:`agent_session_loop.workbench_details` answer a browser on this machine,
  so every one of them carries absolute paths -- ``plan_dir``, ``path``,
  ``state_path``, ``events_path``. :data:`asl_workbench.transport.DETAIL_SHAPE`
  has nowhere to put them, and
  :func:`~.workbench_remote_reads_payload._scrub` takes them out before
  anything is handed back. The Pi renders what a plan *says*; where it sits on
  somebody's laptop is not part of that, and a filesystem layout that has
  crossed a network is a filesystem layout that has been disclosed.

* **A failure becomes a reason.** ``DetailError`` carries an HTTP status here
  because the local surface is HTTP. A remote read has no status line, so the
  ``reason`` travels instead -- ``not-found`` and ``malformed-plan`` and the
  rest -- and the answer is still an answer. A read that found nothing is
  reported as a refusal with a reason; a read that found an empty artifact is
  reported as complete with nothing in it. Those are different facts and the
  cockpit renders them differently.

Nothing here writes. Every reader it calls was already non-reclaiming, which is
what makes "a remote operator reading a plan cannot change it" a property of
the readers rather than a promise made at this layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from asl_workbench import catalog as portable_catalog
from asl_workbench import details
from asl_workbench.details import REASON_NOT_FOUND, DetailError, Window
from asl_workbench.transport import (
    DETAIL_REFUSAL_REASONS,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_ENTRIES,
)

from . import (
    workbench_archive,
    workbench_details,
    workbench_registry,
    workbench_registry_models,
)
from .workbench_remote_reads_index import PlanIndex
from .workbench_remote_reads_payload import (
    ROW_DETAILS,
    TEXT_DETAILS,
    _fit_rows,
    _fit_text,
    _Material,
    _scrub,
    detail_payload,
)

__all__ = ["AslDetailResponder"]


class AslDetailResponder:
    """Answers one ``detail.read`` against this Mac, and changes nothing.

    Satisfies :class:`asl_workbench.connector_protocols.DetailResponder`. The connector
    calls :meth:`answer` on a worker and takes whatever comes back as the frame
    it sends, so this class raises nothing it can help raising: a read that
    failed is a payload carrying a reason, which is an answer the far end can
    render, rather than an exception the far end experiences as silence.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        plan_roots: Iterable[str | Path] = (),
        include_default_dirs: bool = True,
        registry_path: str | Path | None = None,
        index: PlanIndex | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.registry_path = registry_path
        self.index = index or PlanIndex(
            state_dir=state_dir,
            plan_roots=plan_roots,
            include_default_dirs=include_default_dirs,
        )

    # -- the protocol ----------------------------------------------------
    def answer(self, request: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        """The detail payload answering one request, refusal included."""
        raw_target = request.get("target")
        target: dict[str, Any] = (
            dict(raw_target) if isinstance(raw_target, Mapping) else {}
        )
        kind = request.get("detail")
        if not isinstance(kind, str) or kind not in details.DETAIL_KINDS:
            return self._refusal(str(kind), target, details.REASON_INVALID_REQUEST, now)
        if kind == "archive":
            if target != {"archive": "list"}:
                return self._refusal(kind, target, details.REASON_INVALID_REQUEST, now)
            try:
                material = self._archive(request)
            except workbench_archive.ArchiveRefused:
                return self._refusal(kind, target, details.REASON_OS_ERROR, now)
            return detail_payload(
                detail=kind, target=target, material=material, observed_at=now
            )
        try:
            plan_dir = self.index.resolve(target.get("plan_key"), now=now)
            material = self._read(kind, plan_dir, request)
        except DetailError as error:
            return self._refusal(kind, target, error.reason, now)
        except (OSError, ValueError) as error:
            # A reader that leaked rather than classified. Still an answer, and
            # still the reason closest to true: something on this disk did not
            # read.
            del error
            return self._refusal(kind, target, details.REASON_OS_ERROR, now)
        if material.run_id is not None:
            target["run_id"] = material.run_id
            plan_key = target.get("plan_key")
            if isinstance(plan_key, str):
                target["run_key"] = portable_catalog.run_key(plan_key, material.run_id)
        return detail_payload(
            detail=kind, target=target, material=material, observed_at=now
        )

    def _archive(self, request: Mapping[str, Any]) -> _Material:
        """One bounded page of archive metadata, with no Mac path in it."""
        view = workbench_archive.review_archive(
            state_dir=self.state_dir, registry_path=self.registry_path
        )
        rows: list[dict[str, Any]] = []
        for entry in view.entry_payloads():
            summary = entry.get("summary")
            rows.append(
                {
                    "archive_id": entry["archive_id"],
                    "summary": summary
                    if isinstance(summary, str)
                    and "/" not in summary
                    and "\\" not in summary
                    else None,
                    "archived_at": entry.get("archived_at"),
                    "expires_at": entry.get("expires_at"),
                    "cleanup_state": entry.get("cleanup_state"),
                    "plan_archived": entry.get("plan_archived"),
                    "state_archived": entry.get("state_archived"),
                }
            )
        offset = max(0, int(request.get("offset") or 0))
        selected, cursor = _fit_rows(
            rows[offset : offset + MAX_DETAIL_ENTRIES - 1], offset=offset
        )
        if cursor is None and offset + len(selected) < len(rows):
            cursor = offset + len(selected)
        metadata = {
            "archive_retention_days": view.retention_days,
            "candidate_count": view.review.candidate_count,
            "undated_count": view.review.undated_count,
            "entry_count": len(rows),
        }
        return _Material(
            entries=[{"archive_metadata": metadata}, *selected],
            offset=offset,
            next_offset=cursor,
            total=len(rows),
        )

    def _refusal(
        self, kind: str, target: Mapping[str, Any], reason: str, now: float
    ) -> dict[str, Any]:
        """One read that produced no material, saying why in one closed word.

        ``complete`` is ``unknown`` rather than ``partial``: a refused read did
        not see part of the artifact, it did not see the artifact, and claiming
        partial knowledge of something never opened is the claim that makes a
        reconciling consumer delete rows.

        The reason is translated rather than passed through. Some readers raise
        in a vocabulary of their own -- ``read_questions`` answers in
        :data:`agent_session_loop.qna.QNA_ERROR_REASONS`, whose words are about
        a runner's state rather than about an artifact -- and
        :data:`asl_workbench.transport.DETAIL_REFUSAL_REASONS` is closed. An
        undeclared word would make the frame unencodable, which the far end
        experiences as silence: the worst possible reading of "there is nothing
        here". So it becomes ``not-found`` -- every one of them means the
        artifact this read names is not there to read -- and the original word
        travels as a warning, where a cockpit can still show it.
        """
        declared = reason if reason in DETAIL_REFUSAL_REASONS else REASON_NOT_FOUND
        payload: dict[str, Any] = {
            "detail": kind if kind in details.DETAIL_KINDS else "plan",
            "target": dict(target) or {"plan_key": ""},
            "observed_at": now,
            "complete": "unknown",
            "reason": declared,
        }
        if declared != reason:
            payload["warnings"] = [f"{kind}: {reason}"[:200]]
        return payload

    # -- dispatch --------------------------------------------------------
    def _read(self, kind: str, plan_dir: Path, request: Mapping[str, Any]) -> _Material:
        """Perform one read and bring back what it found, paths already gone."""
        if kind == "plan":
            registry = self._registry()
            payload = workbench_details.read_plan(
                plan_dir, registry=registry, state_dir=self.state_dir
            )
            return _Material(entries=[_scrub(payload)])
        if kind == "questions":
            raw_target = request.get("target")
            target = raw_target if isinstance(raw_target, Mapping) else {}
            question_id = target.get("question_id")
            return _Material(
                entries=[
                    _scrub(
                        workbench_details.read_questions(
                            plan_dir,
                            question_id=(
                                question_id if isinstance(question_id, str) else None
                            ),
                            state_dir=self.state_dir,
                        )
                    )
                ]
            )
        if kind == "recovery":
            return _Material(
                entries=[
                    _scrub(
                        workbench_details.read_recovery(
                            plan_dir, state_dir=self.state_dir
                        )
                    )
                ]
            )
        reader = workbench_details.open_plan(plan_dir, state_dir=self.state_dir)
        if kind in ROW_DETAILS:
            return self._rows(kind, reader, request)
        if kind in TEXT_DETAILS:
            return self._text(kind, reader, request)
        return _Material(entries=[_scrub(self._object(kind, reader))])

    def _registry(self) -> workbench_registry_models.WorkbenchRegistry:
        """The registry, or an empty one. A pin is context, never a dependency."""
        try:
            return workbench_registry.load_registry(self.registry_path)
        except workbench_registry_models.WorkbenchError:
            return workbench_registry_models.WorkbenchRegistry(
                path=workbench_registry.default_registry_path()
            )

    def _object(
        self, kind: str, reader: workbench_details.PlanReader
    ) -> Mapping[str, Any]:
        if kind == "artifacts":
            return workbench_details.read_artifacts(reader)
        if kind == "state":
            return workbench_details.read_state(reader)
        if kind == "verification":
            return workbench_details.read_verification(reader)
        if kind == "result":
            return workbench_details.read_result(reader)
        raise DetailError(details.REASON_INVALID_REQUEST, f"unreadable detail: {kind}")

    def _rows(
        self,
        kind: str,
        reader: workbench_details.PlanReader,
        request: Mapping[str, Any],
    ) -> _Material:
        """One paged read. The cursor the reader produced is the one that ships."""
        offset = details.bounded_int(
            request.get("offset"), default=0, maximum=MAX_DETAIL_ENTRIES, name="offset"
        )
        limit = details.bounded_int(
            request.get("limit"),
            default=details.DEFAULT_EVENT_LIMIT,
            maximum=MAX_DETAIL_ENTRIES,
            name="limit",
        )
        run_id = request.get("target", {}).get("run_id")
        if kind == "events":
            payload = workbench_details.read_events(
                reader,
                offset=offset,
                limit=limit,
                run_id=run_id if isinstance(run_id, str) else None,
            )
        elif kind == "commits":
            payload = workbench_details.read_commits(
                reader, run_id=run_id if isinstance(run_id, str) else None
            )
        else:
            payload = workbench_details.read_progress(reader)
        rows = payload.get(ROW_DETAILS[kind]) or []
        scrubbed = _scrub(list(rows))
        # ``events`` pages inside the reader; the other two hand back everything
        # they have, so the window is applied here. Either way the cursor is
        # computed from what is actually being sent.
        if kind == "events":
            start = int(payload.get("offset") or offset)
            total = payload.get("total")
            cursor = payload.get("next_offset")
        else:
            start = offset
            total = len(scrubbed)
            scrubbed = scrubbed[start : start + limit]
            cursor = start + len(scrubbed) if start + len(scrubbed) < total else None
        kept, trimmed_cursor = _fit_rows(scrubbed, offset=start)
        selected = payload.get("run_id")
        return _Material(
            entries=kept,
            offset=start,
            next_offset=trimmed_cursor if trimmed_cursor is not None else cursor,
            total=total if isinstance(total, int) else None,
            run_id=selected if isinstance(selected, str) else None,
        )

    def _text(
        self,
        kind: str,
        reader: workbench_details.PlanReader,
        request: Mapping[str, Any],
    ) -> _Material:
        """One bounded text read, cut to what a detail frame may carry."""
        max_bytes = details.bounded_int(
            request.get("max_bytes"),
            default=min(details.DEFAULT_LOG_BYTES, MAX_DETAIL_BYTES),
            maximum=MAX_DETAIL_BYTES,
            name="max_bytes",
        )
        artifact = request.get("artifact")
        stream = request.get("stream")
        if kind == "logs":
            # ``read_logs`` also takes ``path``, the browser's compatibility
            # form, and it is not offered here. The scope would contain it, but
            # a remote caller has no business composing a path at all: the
            # request shape gives it a name and a stream, and refusing the third
            # form outright is one fewer containment argument to be right about.
            payload = workbench_details.read_logs(
                reader,
                stream=stream if isinstance(stream, str) else None,
                artifact=artifact if isinstance(artifact, str) else None,
                window=Window(
                    offset=(
                        int(request["offset"])
                        if isinstance(request.get("offset"), int)
                        else None
                    ),
                    max_bytes=max_bytes,
                ),
            )
        elif kind == "last-message":
            payload = workbench_details.read_last_message(reader, max_bytes=max_bytes)
        else:
            payload = workbench_details.read_final_review(reader, max_bytes=max_bytes)
        text, over_budget = _fit_text(str(payload.get("text") or ""))
        cut = bool(
            payload.get("truncated")
            or payload.get("truncated_head")
            or payload.get("truncated_tail")
            or over_budget
        )
        warnings: list[str] = []
        failure = payload.get("error")
        if failure:
            warnings.append(f"{kind}: {failure}"[:200])
        size = payload.get("size")
        return _Material(
            content=text,
            offset=int(payload.get("offset") or 0),
            next_offset=(
                int(payload["next_offset"])
                if isinstance(payload.get("next_offset"), int) and not over_budget
                else None
            ),
            total=size if isinstance(size, int) else None,
            truncated=cut,
            artifact=(
                artifact
                if isinstance(artifact, str)
                and artifact in details.READABLE_ARTIFACT_NAMES
                else None
            ),
            stream=(
                stream
                if isinstance(stream, str) and stream in details.STREAM_ARTIFACTS
                else None
            ),
            warnings=warnings,
        )
