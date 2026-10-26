"""Plan snapshot inspection and the stamp-validated snapshot cache."""

from __future__ import annotations

import threading
from pathlib import Path

from .plans_content_hash import (
    plan_content_sha256,
    plan_fingerprint,
)
from .plans_discovery import (
    _format_noncanonical_session_file_error,
    discover_session_files,
    noncanonical_session_file_suggestions,
)
from .plans_errors import (
    PlanError,
    PlanNotFoundError,
)
from .plans_files import (
    _is_file,
    _raise_read_error,
    canonicalize_path,
    path_sha256,
    plan_title_from_markdown,
)
from .plans_handoff import (
    _REPOSITORY_REGISTRY_UNSET,
    read_session_status,
)
from .plans_models import (
    PlanSnapshot,
    SessionStatus,
)
from .plans_registry import (
    read_repository_registry,
)
from .plans_session_scope import (
    SessionScopeError,
)
from .plans_transaction import (
    recover_pending_plan_transaction,
)
from .workspaces import (
    RepositoryRegistry,
)


def require_plan_directory(plan_dir: str | Path) -> Path:
    """Return the canonical ``plan_dir``, refusing one that is not a directory.

    Every plan-scoped command reports a missing plan, or a path that names a
    file rather than a directory, with these same messages,
    including the ones (``unlock``, ``compact-state``) that never read the plan
    files and would otherwise quietly report on a state directory that cannot
    belong to anything.
    """
    canonical_dir = canonicalize_path(plan_dir)
    if not canonical_dir.is_dir():
        if canonical_dir.exists():
            raise PlanError(f"plan path is not a directory: {canonical_dir}")
        raise PlanNotFoundError(f"plan directory does not exist: {canonical_dir}")
    return canonical_dir


def require_split_plan_directory(plan_dir: str | Path) -> Path:
    """Return the canonical ``plan_dir``, refusing one that is not a split plan.

    :func:`require_plan_directory` plus the two files that make a directory a
    split plan, with the messages :func:`inspect_plan` reports for them, for
    commands (``gc``, ``unlock``) that never read the sessions themselves.
    """
    canonical_dir = require_plan_directory(plan_dir)
    for name in ("context.md", "manifest.md"):
        if not _is_file(canonical_dir / name):
            raise PlanError(f"missing {name} in plan directory: {canonical_dir}")
    return canonical_dir


def inspect_plan(
    plan_dir: str | Path,
    *,
    repository_registry: RepositoryRegistry
    | None
    | object = _REPOSITORY_REGISTRY_UNSET,
) -> PlanSnapshot:
    """Inspect a split plan directory and return durable session state."""
    recover_pending_plan_transaction(plan_dir)
    canonical_dir = require_split_plan_directory(plan_dir)
    context_path = canonical_dir / "context.md"
    manifest_path = canonical_dir / "manifest.md"

    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(manifest_path, error)
    plan_title = (
        plan_title_from_markdown(manifest_text)
        or canonical_dir.name
        or str(canonical_dir)
    )

    sessions = discover_session_files(canonical_dir)
    if not sessions:
        noncanonical_suggestions = noncanonical_session_file_suggestions(canonical_dir)
        if noncanonical_suggestions:
            raise PlanError(
                _format_noncanonical_session_file_error(
                    canonical_dir, noncanonical_suggestions
                )
            )
        raise PlanError(
            f"no session-<id>.md files found in plan directory: {canonical_dir}"
        )

    registry = (
        read_repository_registry(canonical_dir)
        if repository_registry is _REPOSITORY_REGISTRY_UNSET
        else repository_registry
    )
    if registry is not None and not isinstance(registry, RepositoryRegistry):
        raise TypeError("repository_registry must be a RepositoryRegistry or None")
    statuses: list[SessionStatus] = []
    for session in sessions:
        try:
            status = read_session_status(
                session,
                repository_registry=registry,
            )
        except SessionScopeError as error:
            raise SessionScopeError(
                error.reason,
                line_number=error.line_number,
                source_line=error.source_line,
                session_id=session.id,
                path=session.path,
            ) from error
        statuses.append(status)
    content_hash = plan_content_sha256(canonical_dir, sessions)

    return PlanSnapshot(
        plan_dir=canonical_dir,
        context_path=context_path,
        manifest_path=manifest_path,
        plan_title=plan_title,
        sessions=tuple(statuses),
        content_sha256=content_hash,
        path_hash=path_sha256(canonical_dir),
        fingerprint=plan_fingerprint(canonical_dir, content_hash),
    )


def _plan_file_stamps(canonical_dir: Path) -> dict[str, tuple[int, int, int]] | None:
    """Stamp every file `plan_content_sha256` hashes, or `None` if unreadable.

    Mirrors `plan_content_sha256`'s file-set enumeration so an added or removed
    session file *or* expansion note changes the stamp keys, not just the values.
    """
    try:
        recover_pending_plan_transaction(canonical_dir)
        sessions = discover_session_files(canonical_dir)
    except PlanError, OSError:
        return None

    relative_names = ["context.md", "manifest.md"]
    for session in sorted(sessions, key=lambda session: session.session_id.sort_key):
        relative_names.append(session.filename)
        expansion_name = session.expansion_filename
        try:
            if (canonical_dir / expansion_name).is_file():
                relative_names.append(expansion_name)
        except OSError:
            return None

    stamps: dict[str, tuple[int, int, int]] = {}
    for relative_name in relative_names:
        try:
            info = (canonical_dir / relative_name).stat()
        except OSError:
            return None
        stamps[relative_name] = (info.st_mtime_ns, info.st_size, info.st_ctime_ns)
    return stamps


class PlanSnapshotCache:
    """Reuse an `inspect_plan()` result while every hashed plan file is untouched.

    Instances are owned by their caller, so a one-shot CLI invocation keeps the
    uncached behaviour. `get()` is fail-closed: anything it cannot positively
    prove unchanged -- a different plan directory, a different file set, a
    changed `(mtime_ns, size, ctime_ns)`, or an `OSError` -- forces a full
    re-read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plan_dir: Path | None = None
        self._snapshot: PlanSnapshot | None = None
        self._stamps: dict[str, tuple[int, int, int]] | None = None

    def get(self, plan_dir: str | Path) -> PlanSnapshot:
        """Return a validated cached snapshot, re-reading the plan on any doubt."""
        canonical_dir = canonicalize_path(plan_dir)
        with self._lock:
            if (
                self._snapshot is not None
                and self._plan_dir == canonical_dir
                and self._stamps is not None
                and self._stamps == _plan_file_stamps(canonical_dir)
            ):
                return self._snapshot
            return self._reread(canonical_dir)

    def get_fresh(self, plan_dir: str | Path) -> PlanSnapshot:
        """Re-read the plan unconditionally and re-record it as the cached state."""
        canonical_dir = canonicalize_path(plan_dir)
        with self._lock:
            return self._reread(canonical_dir)

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next `get()` re-reads."""
        with self._lock:
            self._plan_dir = None
            self._snapshot = None
            self._stamps = None

    def _reread(self, canonical_dir: Path) -> PlanSnapshot:
        # Stamp before reading: a write racing the read then leaves a stamp that
        # no longer matches the file, which costs a re-read instead of serving a
        # snapshot that never existed on disk.
        stamps = _plan_file_stamps(canonical_dir)
        snapshot = inspect_plan(canonical_dir)
        self._plan_dir = canonical_dir
        self._snapshot = snapshot
        self._stamps = stamps
        return snapshot
