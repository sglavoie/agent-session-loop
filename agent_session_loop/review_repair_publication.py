"""Persist rollback images and recover interrupted repair publications."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    durability,
    plans_files,
    plans_transaction,
)
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_session_ids import SessionId

# ---------------------------------------------------------------------------
# Durable publication window (crash-safe rollback)
# ---------------------------------------------------------------------------

#: Marker directory holding the pre-publication image of every file a repair
#: materialization is about to change. Its presence means a publication was
#: interrupted; recovery restores the image and removes it.
REPAIR_PUBLICATION_DIR_NAME = ".asl-review-repair-publication"


REPAIR_PUBLICATION_JOURNAL_NAME = "journal.json"


REPAIR_PUBLICATION_SCHEMA_VERSION = 1


#: Ordered publication phases. They are evidence, not control flow: recovery
#: rolls back from every one of them, because the only state a crashed
#: publication can be trusted to reproduce is the one it started from.
PUBLICATION_PHASE_AUTHORING = "authoring"


PUBLICATION_PHASE_VALIDATING = "validating"


PUBLICATION_PHASE_PUBLISHING = "publishing"


@dataclass(frozen=True)
class _PublicationWindow:
    """The open rollback image for one in-flight repair publication."""

    directory: Path
    plan_dir: Path
    journal: dict[str, Any]

    @property
    def journal_path(self) -> Path:
        return self.directory / REPAIR_PUBLICATION_JOURNAL_NAME


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_publication_journal(directory: Path, journal: Mapping[str, Any]) -> None:
    """Publish the journal itself atomically, payloads already durable."""
    durability.replace_text(
        directory / REPAIR_PUBLICATION_JOURNAL_NAME,
        json.dumps(dict(journal), sort_keys=True, separators=(",", ":")) + "\n",
        sync_file=True,
        sync_parent=True,
    )


def _open_publication_window(
    canonical_dir: Path,
    *,
    review_id: SessionId,
    child_filenames: Sequence[str],
    restore_paths: Sequence[Path],
    now: datetime | None = None,
) -> _PublicationWindow:
    """Record the pre-publication image of every file about to change.

    The image is durable before the first destination byte moves, so a crash at
    any later boundary is recoverable to the state the plan was in when the
    publication began.
    """
    directory = canonical_dir / REPAIR_PUBLICATION_DIR_NAME
    shutil.rmtree(directory, ignore_errors=True)
    try:
        directory.mkdir(parents=True)
        entries: list[dict[str, Any]] = []
        ordered = sorted({path for path in restore_paths}, key=str)
        for index, path in enumerate(ordered):
            relative = path.relative_to(canonical_dir).as_posix()
            original = _read_bytes_or_none(path)
            if original is None:
                entries.append({"path": relative, "payload": None})
                continue
            payload_name = f"{index:06d}.bin"
            with (directory / payload_name).open("wb") as handle:
                handle.write(original)
                handle.flush()
                durability.fullsync(handle.fileno())
            entries.append(
                {
                    "path": relative,
                    "payload": payload_name,
                    "sha256": hashlib.sha256(original).hexdigest(),
                }
            )
        journal = {
            "version": REPAIR_PUBLICATION_SCHEMA_VERSION,
            "review_session_id": review_id.value,
            "children": list(child_filenames),
            "phase": PUBLICATION_PHASE_AUTHORING,
            "opened_at": (now or datetime.now(UTC)).isoformat(),
            "pid": os.getpid(),
            "entries": entries,
        }
        _write_publication_journal(directory, journal)
    except OSError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise PlanError(
            f"failed to record the repair publication rollback image: {error}"
        ) from error
    plans_files._fsync_directory(canonical_dir)
    return _PublicationWindow(
        directory=directory, plan_dir=canonical_dir, journal=journal
    )


def _advance_publication_phase(window: _PublicationWindow, phase: str) -> None:
    """Record how far a publication got, for post-crash forensics."""
    window.journal["phase"] = phase
    # The phase is forensic evidence; losing it never changes what recovery does.
    with contextlib.suppress(OSError):
        _write_publication_journal(window.directory, window.journal)


def _close_publication_window(window: _PublicationWindow) -> None:
    """Retire a completed publication: the new state is now the valid one."""
    shutil.rmtree(window.directory, ignore_errors=True)
    with contextlib.suppress(OSError):
        plans_files._fsync_directory(window.plan_dir)


def _restore_publication_image(
    canonical_dir: Path, journal: Mapping[str, Any]
) -> dict[str, Any]:
    """Put every recorded file back exactly as the publication found it."""
    directory = canonical_dir / REPAIR_PUBLICATION_DIR_NAME
    entries = journal.get("entries")
    entries = entries if isinstance(entries, Sequence) else ()
    restored: list[str] = []
    removed: list[str] = []
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        relative = entry.get("path")
        if not isinstance(relative, str):
            continue
        destination = canonical_dir / relative
        payload = entry.get("payload")
        try:
            if payload is None:
                if destination.exists():
                    destination.unlink()
                    removed.append(relative)
                continue
            content = (directory / str(payload)).read_bytes()
            destination.parent.mkdir(parents=True, exist_ok=True)
            durability.replace_bytes(destination, content, sync_file=True)
            restored.append(relative)
        except OSError as error:
            errors.append(f"{relative}: {error}")
    with contextlib.suppress(OSError):
        plans_files._fsync_directory(canonical_dir)
    return {
        "restored": restored,
        "removed": removed,
        "errors": errors,
        "phase": journal.get("phase"),
        "review_session_id": journal.get("review_session_id"),
        "children": list(journal.get("children") or ()),
    }


def _rollback_publication_window(window: _PublicationWindow) -> dict[str, Any]:
    """Undo an in-flight publication and retire its window."""
    record = _restore_publication_image(window.plan_dir, window.journal)
    if not record["errors"]:
        shutil.rmtree(window.directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            plans_files._fsync_directory(window.plan_dir)
    return record


def recover_review_repair_publication(plan_dir: str | Path) -> dict[str, Any] | None:
    """Roll an interrupted repair publication back to its starting state.

    A publication is only valid once its children are loop-ready, and nothing
    on disk can prove that a crashed one would have got there. So recovery is
    always a rollback: the plan returns to the state it held before the
    materialization, which is a state a run can execute. The findings that
    prompted the repair are still in the review artifact, so the next cycle
    re-plans and re-materializes rather than inheriting a half-published child.

    Returns ``None`` when there is nothing to recover. Callers must hold the
    plan lock: this writes plan files.
    """
    canonical_dir = canonicalize_path(plan_dir)
    directory = canonical_dir / REPAIR_PUBLICATION_DIR_NAME
    journal_path = directory / REPAIR_PUBLICATION_JOURNAL_NAME
    if not directory.is_dir():
        return None
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        journal = None
    if not isinstance(journal, Mapping):
        # The journal is published atomically *before* the first destination
        # byte moves, so a window without one is a window that never opened:
        # there is nothing to restore. It is still removed, because leaving it
        # would make every later publication believe a recovery is pending.
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            plans_files._fsync_directory(canonical_dir)
        return {
            "recovered": True,
            "reason": "unopened-publication-window",
            "restored": [],
            "removed": [],
            "errors": [],
        }
    if journal.get("version") != REPAIR_PUBLICATION_SCHEMA_VERSION:
        # A window written by a different schema may name files this code would
        # restore wrongly. Leave it in place and report it: an operator (or a
        # matching ASL version) resolves it, and every caller keeps failing
        # closed until then.
        return {
            "recovered": False,
            "reason": "unsupported-publication-schema",
            "version": journal.get("version"),
            "restored": [],
            "removed": [],
            "errors": [],
        }
    # A repair publication straddles the plan transaction, so the transaction's
    # own roll-forward runs first: restoring an image over a half-installed
    # generation would leave the transaction to re-install it afterwards.
    plans_transaction.recover_pending_plan_transaction(canonical_dir)
    record = _restore_publication_image(canonical_dir, journal)
    if not record["errors"]:
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            plans_files._fsync_directory(canonical_dir)
    return {"recovered": not record["errors"], "reason": "rolled-back", **record}
