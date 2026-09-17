"""The portable half of the Workbench's detail reads.

:mod:`asl_workbench.catalog` says what may be published; this module says what
may be *fetched*. A catalog entry names a plan without naming its filesystem,
and everything the entry leaves out -- the state payload, the event journal,
log windows, the last message, the turn result, verification, commits, the
progress journal, the final review, open questions, and the recovery card --
is read on demand through the rules below. The provider half that touches the
filesystem is ``agent_session_loop.workbench_details``; this module imports no
ASL runtime and opens no file, so the same rules load on the Pi.

Three rules, and each is a refusal rather than a convention:

* **A read names an artifact, not a path.** :data:`ARTIFACT_NAMES` is closed,
  and :meth:`DetailScope.artifact` resolves a name against the artifacts *the
  selected plan and run actually published*. A caller that knows a filename
  cannot turn that into a read of it, and a name the plan never published is a
  404 rather than a probe of the filesystem.

* **A path, where one is still accepted, is contained.** The compatibility
  form that hands back a path from an earlier artifact listing goes through
  :meth:`DetailScope.contained_path`, which refuses anything outside the
  selected plan's own state directory. Containment is decided here so the
  local server and a remote connector cannot disagree about it.

* **A window is bounded before it is read.** :class:`Window` and
  :func:`paginate` carry the byte caps, tail windows, offsets, and page
  arithmetic the existing routes use, so "bounded" is a property of the
  request object rather than something each caller remembers.

Detail is also where search lives. :func:`detail_search_text` builds a haystack
from the private half of a plan -- its path, label, pause prose, and
last-message preview -- which is exactly the material
:data:`asl_workbench.catalog.CACHE_SAFE_FIELDS` keeps out of a published entry.
Searching it is an online read of detail the Pi asks for and does not retain;
:class:`asl_workbench.catalog.CatalogEntry` keeps its own cache-safe
``search_text`` for the rows a remote cockpit holds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from .catalog import CACHE_SAFE_FIELDS, PRIVATE_FIELDS, PlanDetail, safe_label

# -- Vocabularies -----------------------------------------------------------

#: The detail surfaces a caller may ask for. One name per existing Workbench
#: detail view, so a transport that carries a request carries a word from a
#: closed set rather than a route string a future version may rename.
DETAIL_KINDS: tuple[str, ...] = (
    "plan",
    "artifacts",
    "state",
    "events",
    "logs",
    "last-message",
    "result",
    "verification",
    "commits",
    "progress",
    "final-review",
    "questions",
    "recovery",
)

#: Every artifact a detail read may name, using the same keys the artifact
#: listing publishes. Sharing the names rather than translating them is
#: deliberate: a descriptor a caller was handed is the request it can make
#: next, and a translation table would be one more thing to drift.
ARTIFACT_NAMES: tuple[str, ...] = (
    "state_path",
    "events_path",
    "progress_path",
    "final_review_path",
    "final_review_artifact_path",
    "artifact_dir",
    "stdout_path",
    "stderr_path",
    "last_message_path",
    "result_path",
)

#: The artifacts a bounded text or log window may be taken over. ``artifact_dir``
#: is absent because it is a directory, and a windowed read of a directory is a
#: request that has already gone wrong.
READABLE_ARTIFACT_NAMES: tuple[str, ...] = tuple(
    name for name in ARTIFACT_NAMES if name != "artifact_dir"
)

#: The two live streams, and the artifact each resolves to. ``stream=stdout``
#: is the form the browser already sends; it is an alias for the artifact name
#: rather than a second lookup path.
STREAM_ARTIFACTS: Mapping[str, str] = {
    "stdout": "stdout_path",
    "stderr": "stderr_path",
}

# -- Bounds -----------------------------------------------------------------

#: Default window on a log read, and the hard cap on any single text read.
#: Both match the bounds the Workbench server has always applied; they live
#: here so the remote provider cannot answer with a larger window than the
#: local one would.
DEFAULT_LOG_BYTES = 64 * 1024
MAX_READ_BYTES = 1024 * 1024

#: Default and maximum page size over the event journal.
DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 2000

#: Defaults for the two whole-file text reads. The final review is larger
#: because it is a document an operator reads end to end, while the last
#: message is a preview of the harness's most recent output.
DEFAULT_LAST_MESSAGE_BYTES = 64 * 1024
DEFAULT_FINAL_REVIEW_BYTES = 256 * 1024

# -- Refusals ---------------------------------------------------------------

#: Refusal reasons. These are the strings the HTTP layer already answers with,
#: kept here so a provider raising one does not have to know it is being served
#: over HTTP, and so a remote transport reports the same reason for the same
#: refusal.
REASON_INVALID_REQUEST = "invalid-request"
REASON_INVALID_PLAN = "invalid-plan"
REASON_NOT_FOUND = "not-found"
REASON_MALFORMED_PLAN = "malformed-plan"
REASON_OS_ERROR = "os-error"

_STATUS_BY_REASON: Mapping[str, int] = {
    REASON_INVALID_REQUEST: 400,
    REASON_INVALID_PLAN: 400,
    REASON_NOT_FOUND: 404,
    REASON_MALFORMED_PLAN: 500,
    REASON_OS_ERROR: 500,
}


class DetailError(Exception):
    """A detail read that was refused, with the reason a caller is owed.

    Carries the status the HTTP surface answers with so the refusal decided in
    this module is the refusal the client sees. A provider raising one has not
    read anything: every refusal below is decided before a file is opened.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = (
            status if status is not None else _STATUS_BY_REASON.get(reason, 400)
        )
        self.details: dict[str, Any] = dict(details or {})


# -- Bounded windows --------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A bounded read over one artifact, validated at construction.

    Exactly the three modes the log routes have always supported:

    * ``offset`` -- start at that byte and read up to ``max_bytes``;
    * ``tail_lines`` -- read the trailing ``max_bytes`` and keep the last
      ``tail_lines`` lines;
    * neither -- read the trailing ``max_bytes``, like ``tail -c``.

    ``offset`` and ``tail_lines`` are mutually exclusive because the answer to
    "which bytes did you mean" has to be one answer.
    """

    offset: int | None = None
    tail_lines: int | None = None
    max_bytes: int = DEFAULT_LOG_BYTES

    def __post_init__(self) -> None:
        if self.offset is not None and self.tail_lines is not None:
            raise DetailError(
                REASON_INVALID_REQUEST,
                "specify either 'tail_lines' or 'offset', not both",
            )
        if self.offset is not None and self.offset < 0:
            raise DetailError(REASON_INVALID_REQUEST, "offset must be non-negative")
        if self.tail_lines is not None and self.tail_lines < 0:
            raise DetailError(REASON_INVALID_REQUEST, "tail_lines must be non-negative")
        if self.max_bytes < 0:
            raise DetailError(REASON_INVALID_REQUEST, "max_bytes must be non-negative")
        if self.max_bytes > MAX_READ_BYTES:
            raise DetailError(
                REASON_INVALID_REQUEST,
                f"max_bytes exceeds maximum {MAX_READ_BYTES}",
                details={"max_bytes": self.max_bytes},
            )


@dataclass(frozen=True)
class Page:
    """One window over an ordered sequence, with the arithmetic already done.

    ``next_offset`` is ``None`` at the end of the sequence rather than an
    offset past it, so a caller paging forward stops because the page said so
    and not because it read an empty answer and guessed.
    """

    offset: int
    limit: int
    total: int
    returned: int

    @property
    def next_offset(self) -> int | None:
        end = self.offset + self.returned
        return end if end < self.total else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "next_offset": self.next_offset,
        }


def paginate(rows: Sequence[Any], *, offset: int, limit: int) -> tuple[list[Any], Page]:
    """Slice ``rows`` and describe the slice."""
    if offset < 0:
        raise DetailError(REASON_INVALID_REQUEST, "offset must be non-negative")
    if limit < 0:
        raise DetailError(REASON_INVALID_REQUEST, "limit must be non-negative")
    if limit > MAX_EVENT_LIMIT:
        raise DetailError(
            REASON_INVALID_REQUEST,
            f"limit exceeds maximum {MAX_EVENT_LIMIT}",
            details={"limit": limit},
        )
    sliced = list(rows[offset : offset + limit])
    return sliced, Page(
        offset=offset, limit=limit, total=len(rows), returned=len(sliced)
    )


def bounded_int(
    value: object,
    *,
    name: str,
    default: int,
    maximum: int,
) -> int:
    """Coerce one request parameter into a non-negative, capped integer."""
    if value is None:
        resolved = default
    elif isinstance(value, bool) or not isinstance(value, int | str):
        raise DetailError(
            REASON_INVALID_REQUEST,
            f"query parameter {name!r} must be an integer",
            details={name: repr(value)},
        )
    else:
        try:
            resolved = int(value)
        except ValueError as error:
            raise DetailError(
                REASON_INVALID_REQUEST,
                f"query parameter {name!r} must be an integer",
                details={name: str(value)},
            ) from error
    if resolved < 0:
        raise DetailError(
            REASON_INVALID_REQUEST,
            f"query parameter {name!r} must be non-negative",
        )
    if resolved > maximum:
        raise DetailError(
            REASON_INVALID_REQUEST,
            f"query parameter {name!r} exceeds maximum {maximum}",
        )
    return resolved


# -- The plan a read is allowed to touch ------------------------------------


@dataclass(frozen=True)
class DetailScope:
    """The one plan, and the one run, a detail read may reach.

    Built by the provider from a non-reclaiming classification of the plan, and
    then the only thing a read consults. Every path a read can reach is either
    a value in :attr:`artifacts` -- which the plan itself published -- or a
    path the caller supplied that :meth:`contained_path` proved is inside
    :attr:`plan_state_dir`. Neither route can name a file belonging to another
    plan, and no route accepts a bare filesystem path with nothing behind it.
    """

    plan_key: str
    plan_dir: str
    plan_state_dir: str
    artifacts: Mapping[str, str | None]
    run_key: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        unknown = tuple(
            sorted(name for name in self.artifacts if name not in ARTIFACT_NAMES)
        )
        if unknown:
            raise DetailError(
                REASON_INVALID_REQUEST,
                f"artifact table names unclassified artifacts: {', '.join(unknown)}",
                details={"unknown": list(unknown)},
            )

    # -- Named artifacts ---------------------------------------------------

    def available(self) -> tuple[str, ...]:
        """The artifact names this plan and run actually published."""
        return tuple(
            name for name in ARTIFACT_NAMES if self.artifacts.get(name) is not None
        )

    def artifact(self, name: object, *, readable: bool = False) -> str:
        """Resolve one named artifact against this plan, or refuse.

        ``readable=True`` additionally refuses a name that is not a file a
        bounded text window can be taken over -- today only ``artifact_dir``,
        which is a directory.
        """
        if not isinstance(name, str) or name not in ARTIFACT_NAMES:
            raise DetailError(
                REASON_INVALID_REQUEST,
                f"unknown artifact name: {name!r}",
                details={"artifact": str(name), "known": list(ARTIFACT_NAMES)},
            )
        if readable and name not in READABLE_ARTIFACT_NAMES:
            raise DetailError(
                REASON_INVALID_REQUEST,
                f"artifact {name!r} is not readable as text",
                details={"artifact": name, "readable": list(READABLE_ARTIFACT_NAMES)},
            )
        resolved = self.artifacts.get(name)
        if resolved is None:
            raise DetailError(
                REASON_NOT_FOUND,
                f"plan published no {name!r} artifact",
                details={"plan_dir": self.plan_dir, "artifact": name},
            )
        return resolved

    def check_run(self, requested: object) -> None:
        """Refuse a request that names a run other than the selected one.

        A stale browser tab holds the run key it last saw. Answering its
        request from the current run would hand it the wrong run's bytes under
        the identity it asked for, which is worse than a refusal it can
        recover from by reloading.
        """
        if requested is None or requested == "":
            return
        if not isinstance(requested, str) or requested != self.run_key:
            raise DetailError(
                REASON_NOT_FOUND,
                "requested run is not the plan's selected run",
                details={
                    "plan_dir": self.plan_dir,
                    "requested_run_key": str(requested),
                    "run_key": self.run_key,
                },
            )

    # -- Containment -------------------------------------------------------

    def contains(self, candidate: str) -> bool:
        """Whether an already-resolved path lies inside this plan's state."""
        return PurePath(candidate).is_relative_to(PurePath(self.plan_state_dir))

    def contained_path(self, candidate: str) -> str:
        """Accept a caller-supplied path only when this plan contains it."""
        if not self.contains(candidate):
            raise DetailError(
                REASON_INVALID_REQUEST,
                "path must be inside the plan state directory",
                details={
                    "path": candidate,
                    "plan_state_dir": self.plan_state_dir,
                },
            )
        return candidate

    # -- Log target selection ----------------------------------------------

    def log_target(
        self,
        *,
        stream: str | None = None,
        artifact: str | None = None,
        path: str | None = None,
    ) -> str:
        """The single file a log read applies to, from exactly one selector.

        ``artifact`` is the form to prefer and ``stream`` is its two-value
        alias. ``path`` is the compatibility form kept for callers holding a
        path from an earlier artifact listing; it is contained rather than
        trusted.
        """
        supplied = [
            label
            for label, value in (
                ("stream", stream),
                ("artifact", artifact),
                ("path", path),
            )
            if value is not None
        ]
        if not supplied:
            raise DetailError(
                REASON_INVALID_REQUEST,
                "missing required query parameter 'stream' or 'path'",
            )
        if len(supplied) > 1:
            raise DetailError(
                REASON_INVALID_REQUEST,
                f"specify one of {supplied[0]!r} or {supplied[1]!r}, not both",
            )
        if stream is not None:
            name = STREAM_ARTIFACTS.get(stream)
            if name is None:
                raise DetailError(
                    REASON_INVALID_REQUEST,
                    f"stream must be one of {sorted(STREAM_ARTIFACTS)}",
                    details={"stream": stream},
                )
            try:
                return self.artifact(name, readable=True)
            except DetailError as error:
                if error.reason != REASON_NOT_FOUND:
                    raise
                raise DetailError(
                    REASON_NOT_FOUND,
                    f"no active {stream} log for plan",
                    details={"plan_dir": self.plan_dir},
                ) from error
        if artifact is not None:
            return self.artifact(artifact, readable=True)
        if path is not None:
            return self.contained_path(path)
        raise DetailError(
            REASON_INVALID_REQUEST,
            "missing required query parameter 'stream' or 'path'",
        )


# -- The envelope a detail read comes back in -------------------------------


@dataclass(frozen=True)
class DetailRead:
    """One answered detail read, tied to the plan and run it describes.

    The kind and the identities travel with the payload so a cached catalog row
    and a freshly fetched detail can be matched without the consumer
    reconstructing which request produced which answer -- and so a remote
    consumer can drop a detail whose run no longer matches the row it holds.
    """

    kind: str
    plan_key: str
    payload: Mapping[str, Any]
    run_key: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in DETAIL_KINDS:
            raise DetailError(
                REASON_INVALID_REQUEST,
                f"unknown detail kind: {self.kind!r}",
                details={"kind": self.kind, "known": list(DETAIL_KINDS)},
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "detail": dict(self.payload),
        }


def build_read(kind: str, scope: DetailScope, payload: Mapping[str, Any]) -> DetailRead:
    """Wrap one provider payload in the envelope both providers answer with."""
    return DetailRead(
        kind=kind,
        plan_key=scope.plan_key,
        run_key=scope.run_key,
        payload=dict(payload),
    )


# -- Online search over the private half ------------------------------------

#: The private fields search reads, in the order they are concatenated. Every
#: one is on :data:`asl_workbench.catalog.PRIVATE_FIELDS`, which is what makes
#: this an online read: none of it is in a published entry, so searching it is
#: something a cockpit asks the Mac to do rather than something it can do from
#: a cache it retained.
SEARCH_FIELDS: tuple[str, ...] = (
    "plan_dir",
    "label",
    "detail",
    "concise_reason",
    "next_action_hint",
    "pause_message",
    "last_message_preview",
    "run_id",
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def detail_search_text(detail: PlanDetail) -> str:
    """The haystack for one plan, built from its private half alone.

    Prefers the summary's own ``searchable_text`` -- the Workbench already
    builds it, and reusing it means local search and remote search match the
    same words -- and falls back to concatenating :data:`SEARCH_FIELDS` for a
    detail that predates it or was assembled without it.
    """
    existing = detail.fields.get("searchable_text")
    if isinstance(existing, str) and existing.strip():
        return _normalize(existing)
    parts: list[str] = []
    for name in SEARCH_FIELDS:
        value = detail.fields.get(name)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return _normalize(" ".join(parts))


def matches_search(detail: PlanDetail, query: object) -> bool:
    """Whether one plan's detail matches a free-text query.

    Substring matching over the normalized haystack, which is what the
    Workbench's browser search does today. An empty query matches everything,
    because "no filter" is not "no results".
    """
    if not isinstance(query, str):
        return False
    needle = _normalize(query)
    if not needle:
        return True
    return needle in detail_search_text(detail)


def search_details(
    details: Iterable[PlanDetail], query: object
) -> tuple[PlanDetail, ...]:
    """Every plan whose private detail matches ``query``, in input order."""
    return tuple(detail for detail in details if matches_search(detail, query))


def private_label(detail: PlanDetail) -> str | None:
    """The operator's own name for a plan, which never reaches a cached entry.

    ``label`` is what someone typed into a registry pin. A published entry gets
    :func:`asl_workbench.catalog.display_label`'s answer instead, which prefers
    a manifest title and falls back to a project name -- so the pin's wording
    stays on the Mac and is fetched with the rest of the detail.
    """
    raw = detail.fields.get("label")
    return safe_label(raw)


def search_payload(detail: PlanDetail) -> dict[str, Any]:
    """One search hit as a payload, carrying no field the Pi may retain.

    Deliberately not a :class:`asl_workbench.catalog.CatalogEntry`: a hit is an
    answer to a question that was asked once. It names the plan key so the
    consumer can join it to a row it already holds, and carries the private
    label and haystack so a cockpit can show why the plan matched.
    """
    return {
        "plan_key": detail.plan_key,
        "label": private_label(detail),
        "search_text": detail_search_text(detail),
    }


# -- Self-check --------------------------------------------------------------


def validate_vocabularies() -> tuple[str, ...]:
    """Every structural problem with this module's vocabularies.

    Checked rather than asserted in prose because the artifact names are the
    same words the catalog partition classifies: an artifact that is not a
    private summary field would be one this package had published by accident.
    """
    problems: list[str] = []
    if len(set(DETAIL_KINDS)) != len(DETAIL_KINDS):
        problems.append("DETAIL_KINDS contains a duplicate")
    if len(set(ARTIFACT_NAMES)) != len(ARTIFACT_NAMES):
        problems.append("ARTIFACT_NAMES contains a duplicate")
    for name in ARTIFACT_NAMES:
        if name in CACHE_SAFE_FIELDS:
            problems.append(f"{name}: artifact names a cache-safe summary field")
    # `final_review_artifact_path` is derived from the plan directory rather
    # than carried on the summary, so it is the one artifact with no field to
    # classify. Every other one must be a private summary field.
    for name in ARTIFACT_NAMES:
        if name == "final_review_artifact_path":
            continue
        if name not in PRIVATE_FIELDS:
            problems.append(f"{name}: artifact is not a private summary field")
    for name in SEARCH_FIELDS:
        if name not in PRIVATE_FIELDS:
            problems.append(f"{name}: search field is not a private summary field")
    for stream, name in STREAM_ARTIFACTS.items():
        if name not in READABLE_ARTIFACT_NAMES:
            problems.append(f"{stream}: stream resolves to unreadable {name}")
    return tuple(problems)
