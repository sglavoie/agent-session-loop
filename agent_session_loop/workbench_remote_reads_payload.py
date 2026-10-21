"""Dressing one remote read as a transport payload, bounded and path-free.

The projection half of :mod:`agent_session_loop.workbench_remote_reads`: the
byte budgets, the scrub that removes every local path, the row and text
fitting, and the :data:`asl_workbench.transport.DETAIL_SHAPE` payload builder.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from asl_workbench.observation import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
)
from asl_workbench.transport import MAX_DETAIL_BYTES, MAX_DETAIL_ENTRIES

__all__ = [
    "MAX_ENTRY_BUDGET_BYTES",
    "OBJECT_DETAILS",
    "ROW_DETAILS",
    "TEXT_DETAILS",
    "detail_payload",
]

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
