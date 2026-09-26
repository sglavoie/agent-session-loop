"""Answering a remote detail read against this Mac's ASL corpus.

:mod:`asl_workbench.remote_reads` is the side that asks; this is the side that
looks. It is the only ASL-aware code the connector's correlated lane ever
reaches, and it exists so that :class:`asl_workbench.connector.DetailResponder`
can stay a one-method protocol with no idea what a plan directory is.

Three translations happen here and nowhere else:

* **A key becomes a directory.** The request names a ``plan_key``, which is
  :func:`asl_workbench.catalog.plan_key` -- a digest of a path, not a path. The
  far end has never been told where anything lives and cannot be: a request
  that could name a directory would be a request that could name
  ``/etc/shadow``, and no containment rule applied afterwards would make that
  safe. :class:`PlanIndex` resolves the key against a real discovery pass, so
  the only directories reachable from the wire are the ones this Mac's own
  discovery already publishes.

* **A local payload becomes a transport payload.** The readers in
  :mod:`agent_session_loop.workbench_details` answer a browser on this machine,
  so every one of them carries absolute paths -- ``plan_dir``, ``path``,
  ``state_path``, ``events_path``. :data:`asl_workbench.transport.DETAIL_SHAPE`
  has nowhere to put them, and :func:`_scrub` takes them out before anything is
  handed back. The Pi renders what a plan *says*; where it sits on somebody's
  laptop is not part of that, and a filesystem layout that has crossed a
  network is a filesystem layout that has been disclosed.

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

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asl_workbench import catalog as portable_catalog
from asl_workbench import details
from asl_workbench.details import REASON_NOT_FOUND, DetailError, Window
from asl_workbench.observation import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
)
from asl_workbench.transport import (
    DETAIL_REFUSAL_REASONS,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_ENTRIES,
)

from . import workbench, workbench_archive, workbench_catalog, workbench_details

__all__ = [
    "MAX_ENTRY_BUDGET_BYTES",
    "OBJECT_DETAILS",
    "PLAN_INDEX_TTL_SECONDS",
    "ROW_DETAILS",
    "TEXT_DETAILS",
    "AslDetailResponder",
    "PlanIndex",
    "detail_payload",
]


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

#: How long a resolved ``plan_key`` table is trusted before the corpus is
#: walked again. A miss refreshes immediately whatever this says, so the window
#: only decides how long a *stale hit* can last -- a plan archived a minute ago
#: answers from its old directory and reports ``not-found`` when the read lands,
#: which is the same answer a cockpit would have got a moment later anyway.
PLAN_INDEX_TTL_SECONDS = 30.0

#: The byte budget for the rows of one answer, measured on the encoded form.
#: Below :data:`asl_workbench.transport.MAX_DETAIL_BYTES` on purpose: that bound
#: governs ``content`` alone, and a frame also has to carry the envelope, the
#: target and the pagination around it.
MAX_ENTRY_BUDGET_BYTES = MAX_DETAIL_BYTES - 8 * 1024

#: Detail kinds whose payload is one object. Their answer carries a single
#: entry, because "the state file, scrubbed" is one thing and splitting it into
#: rows would invent a structure the reader does not have.
OBJECT_DETAILS: tuple[str, ...] = (
    "plan",
    "artifacts",
    "state",
    "verification",
    "result",
    "questions",
    "recovery",
)

#: Detail kinds whose payload is a sequence, and the key each keeps it under.
#: These are the reads that page: their rows become ``entries`` and their
#: cursor becomes ``next_offset``.
ROW_DETAILS: Mapping[str, str] = {
    "events": "events",
    "commits": "commits",
    "progress": "entries",
}

#: Detail kinds whose payload is bounded text. These carry ``content``, and
#: their window arithmetic becomes ``offset``/``next_offset``/``total``.
TEXT_DETAILS: tuple[str, ...] = ("logs", "last-message", "final-review")

#: Keys never sent. Every one of them holds a path on this machine, and
#: :data:`asl_workbench.transport.DETAIL_SHAPE` has no field for a path -- the
#: far end addresses a plan by key and an artifact by name, both of the way
#: through.
PRIVATE_KEYS: frozenset[str] = frozenset(
    {
        "plan_dir",
        "path",
        "cwd",
        "root",
        "state_dir",
        "state_root",
        "workspace_root",
        "worktree",
        "repo_path",
        # Not a path field, but a derived blob that concatenates one. The
        # catalog builds it so a local browser can filter on anything it can
        # see, and the plan directory is one of the things it can see. It is
        # also of no use to a remote reader, which filters against the catalog
        # this connector already publishes.
        "searchable_text",
    }
)

#: Key suffixes dropped for the same reason, so an artifact table gaining a new
#: ``*_path`` column does not quietly start publishing one.
PRIVATE_KEY_SUFFIXES: tuple[str, ...] = ("_path", "_dir", "_root")


# --------------------------------------------------------------------------
# Resolving a key to a directory
# --------------------------------------------------------------------------


class PlanIndex:
    """``plan_key`` to plan directory, from this Mac's own discovery.

    The gate between a remote request and a local filesystem. A key that no
    discovery pass produced resolves to nothing, so the set of directories the
    wire can reach is exactly the set this Mac already publishes in its catalog
    -- not a superset of it, and never a path the far end composed.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        plan_roots: Iterable[str | Path] = (),
        include_default_dirs: bool = True,
        ttl_seconds: float = PLAN_INDEX_TTL_SECONDS,
    ) -> None:
        self.state_dir = state_dir
        self.plan_roots = tuple(plan_roots)
        self.include_default_dirs = include_default_dirs
        self._ttl = float(ttl_seconds)
        self._plans: dict[str, Path] = {}
        self._scanned_at: float | None = None

    @property
    def size(self) -> int:
        """How many plans the last pass indexed."""
        return len(self._plans)

    def refresh(self, now: float) -> int:
        """Walk the corpus and rebuild the table. Returns how many plans."""
        scan = workbench_catalog.scan(
            state_dir=self.state_dir,
            plan_roots=self.plan_roots,
            include_default_dirs=self.include_default_dirs,
        )
        rebuilt: dict[str, Path] = {}
        for summary in scan.summaries:
            plan_dir = str(summary.plan_dir)
            try:
                rebuilt[portable_catalog.plan_key(plan_dir)] = Path(plan_dir)
            except portable_catalog.CatalogError:
                continue
        self._plans = rebuilt
        self._scanned_at = now
        return len(rebuilt)

    def resolve(self, plan_key: object, *, now: float) -> Path:
        """The directory one key names, or a refusal saying it names none.

        A miss always costs a fresh pass before it is believed: a plan created
        since the last walk is a plan an operator is most likely to be asking
        about, and answering ``not-found`` from a stale table would be answering
        about the corpus as it was rather than as it is.
        """
        if not isinstance(plan_key, str) or not plan_key:
            raise DetailError(
                details.REASON_INVALID_REQUEST, "a detail read needs a plan_key"
            )
        if self._stale(now):
            self.refresh(now)
        found = self._plans.get(plan_key)
        if found is None:
            self.refresh(now)
            found = self._plans.get(plan_key)
        if found is None:
            raise DetailError(
                details.REASON_NOT_FOUND, "no plan on this connector has that key"
            )
        return found

    def items(self, *, now: float) -> list[tuple[str, Path]]:
        """Every indexed plan as ``(plan_key, directory)``, refreshed if stale.

        For the caller that has to visit the whole corpus rather than resolve
        one key -- settling what a restart left open is the case -- and it walks
        the same table a read resolves against, so it can never reach a
        directory the wire could not have named.
        """
        if self._stale(now):
            self.refresh(now)
        return sorted(self._plans.items())

    def _stale(self, now: float) -> bool:
        return self._scanned_at is None or now - self._scanned_at >= self._ttl


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def _scrub(value: Any) -> Any:
    """One payload with every local path taken out, recursively.

    Applied to whatever a reader returns rather than to a list of known fields,
    so a reader that grows a path column does not need this module edited to
    stop publishing it. The default is to drop, which is the direction a
    privacy rule has to fail in.
    """
    if isinstance(value, Mapping):
        return {
            key: _scrub(item)
            for key, item in value.items()
            if not _private_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


def _private_key(key: str) -> bool:
    return key in PRIVATE_KEYS or key.endswith(PRIVATE_KEY_SUFFIXES)


def _encoded_size(value: Any) -> int:
    """How many bytes one row costs on the wire, near enough to bound by."""
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except TypeError, ValueError:
        return MAX_ENTRY_BUDGET_BYTES


def _fit_rows(rows: Sequence[Any], *, offset: int) -> tuple[list[Any], int | None]:
    """As many rows as the budget allows, and the cursor for the rest.

    A bound on the *encoded* size rather than on the count, because a hundred
    ordinary events and a hundred events carrying stack traces are not the same
    frame. Trimming moves the cursor back, so the caller's next page starts
    exactly where this one stopped and nothing is skipped.
    """
    kept: list[Any] = []
    spent = 0
    for row in rows[:MAX_DETAIL_ENTRIES]:
        cost = _encoded_size(row)
        if kept and spent + cost > MAX_ENTRY_BUDGET_BYTES:
            return kept, offset + len(kept)
        kept.append(row)
        spent += cost
    if len(rows) > len(kept):
        return kept, offset + len(kept)
    return kept, None


def _fit_text(text: str) -> tuple[str, bool]:
    """Text cut to what one detail frame may carry, and whether it was cut.

    Cut on a character boundary, from the front: the readers hand back a window
    that already starts where the caller asked, so what has to go is the tail.
    """
    if not text:
        return "", False
    encoded = text.encode("utf-8", "surrogatepass")
    if len(encoded) <= MAX_DETAIL_BYTES:
        return text, False
    trimmed = encoded[:MAX_DETAIL_BYTES].decode("utf-8", "ignore")
    return trimmed, True


@dataclass
class _Material:
    """What one read found, before it is dressed as a transport payload."""

    entries: list[Any] | None = None
    content: str | None = None
    offset: int | None = None
    next_offset: int | None = None
    total: int | None = None
    truncated: bool = False
    artifact: str | None = None
    stream: str | None = None
    run_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> str:
        """Whether this answer is the whole thing.

        ``partial`` for anything cut -- by a page, by a window, or by the frame
        budget -- because a caller that cannot tell a complete artifact from a
        trimmed one will read the trimmed one as the end of the file.
        """
        cut = self.next_offset is not None or self.truncated or bool(self.warnings)
        return COMPLETENESS_PARTIAL if cut else COMPLETENESS_COMPLETE


def detail_payload(
    *,
    detail: str,
    target: Mapping[str, Any],
    material: _Material,
    observed_at: float,
) -> dict[str, Any]:
    """One :data:`DETAIL_SHAPE` payload, carrying only what it actually has.

    Absent is not the same as empty here: a field omitted was not applicable,
    and a field present and empty is a fact about the artifact. The transport
    refuses ``content_bytes`` without ``content`` and ``entry_count`` without
    ``entries`` for the same reason.
    """
    payload: dict[str, Any] = {
        "detail": detail,
        "target": dict(target),
        "observed_at": observed_at,
        "complete": material.complete,
    }
    if material.content is not None:
        payload["content"] = material.content
        payload["content_bytes"] = len(
            material.content.encode("utf-8", "surrogatepass")
        )
        payload["encoding"] = "utf-8"
    if material.entries is not None:
        payload["entries"] = material.entries
        payload["entry_count"] = len(material.entries)
    for name in ("offset", "next_offset", "total", "artifact", "stream"):
        value = getattr(material, name)
        if value is not None:
            payload[name] = value
    if material.truncated:
        payload["truncated"] = True
    if material.warnings:
        payload["warnings"] = material.warnings[:4]
    return payload


# --------------------------------------------------------------------------
# The responder
# --------------------------------------------------------------------------


class AslDetailResponder:
    """Answers one ``detail.read`` against this Mac, and changes nothing.

    Satisfies :class:`asl_workbench.connector.DetailResponder`. The connector
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

    def _registry(self) -> workbench.WorkbenchRegistry:
        """The registry, or an empty one. A pin is context, never a dependency."""
        try:
            return workbench.load_registry(self.registry_path)
        except workbench.WorkbenchError:
            return workbench.WorkbenchRegistry(path=workbench.default_registry_path())

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
