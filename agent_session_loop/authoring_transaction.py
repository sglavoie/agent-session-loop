"""Archive directories, readiness refresh, and the atomic plan transaction."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import (
    durability,
    plans_files,
    plans_models,
    plans_transaction,
)
from .plans_content_hash import stable_readiness_sha256
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_models import ReadinessMetadata
from .plans_readiness import write_readiness_metadata
from .plans_session_ids import SessionId, parse_session_id

ARCHIVE_ROOT_NAME = "resplit-archive"


# ---------------------------------------------------------------------------
# Archive paths and readiness refresh
# ---------------------------------------------------------------------------


def _timestamp_slug(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def archive_dir_name(session_id: int | str | SessionId, now: datetime) -> str:
    """Deterministic archive subdirectory name for a resplit parent session."""
    canonical_id = parse_session_id(session_id)
    return f"session-{canonical_id.value}-{_timestamp_slug(now)}"


def create_archive_dir(
    plan_dir: str | os.PathLike[str],
    session_id: int | str | SessionId,
    now: datetime,
) -> Path:
    """Create a collision-safe archive directory under ``resplit-archive/``."""
    canonical_dir = canonicalize_path(plan_dir)
    archive_root = canonical_dir / ARCHIVE_ROOT_NAME
    archive_root.mkdir(exist_ok=True)

    base_name = archive_dir_name(session_id, now)
    candidate = archive_root / base_name
    collision_index = 2
    while True:
        try:
            candidate.mkdir()
        except FileExistsError:
            candidate = archive_root / f"{base_name}-{collision_index}"
            collision_index += 1
            continue
        return candidate


def refresh_readiness_metadata(
    plan_dir: str | os.PathLike[str],
    *,
    state: plans_models.ReadinessState = "draft",
    checked_at: str | None = None,
    now: datetime | None = None,
) -> ReadinessMetadata:
    """Recompute the stable readiness SHA and write manifest readiness lines.

    ``checked_at`` may be supplied for deterministic callers; otherwise a UTC
    ISO-8601 timestamp is derived from ``now`` (defaulting to the current time).
    """
    canonical_dir = canonicalize_path(plan_dir)
    sha = stable_readiness_sha256(canonical_dir)
    if checked_at is None:
        stamp = now if now is not None else datetime.now(UTC)
        checked_at = stamp.astimezone(UTC).isoformat()
    metadata = ReadinessMetadata(
        state=state, readiness_sha256=sha, checked_at=checked_at
    )
    write_readiness_metadata(canonical_dir, metadata)
    return metadata


# ---------------------------------------------------------------------------
# Atomic multi-file transaction
# ---------------------------------------------------------------------------


@dataclass
class _StagedFile:
    path: Path
    text: str


class PlanTransaction:
    """Stage many plan files and publish them as one recoverable transaction.

    Immutable payloads and a durable journal are written before any destination
    changes. Ordinary I/O failures roll back immediately; process interruption
    leaves the journal in place so the next authoritative plan read rolls the
    complete publication forward instead of observing a mixed generation.

    Callers still hold the plan lock to serialize writers. The transaction also
    holds a marker-scoped mutex shared with ordinary read-side recovery.
    """

    def __init__(self, plan_dir: str | os.PathLike[str]) -> None:
        self.plan_dir = canonicalize_path(plan_dir)
        self._staged: list[_StagedFile] = []

    def stage(self, relative_path: str | os.PathLike[str], text: str) -> None:
        """Stage ``text`` to be written at ``relative_path`` under the plan dir."""
        path = (self.plan_dir / os.fspath(relative_path)).resolve()
        self._staged.append(_StagedFile(path=path, text=text))

    @property
    def staged_paths(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self._staged)

    def commit(self) -> tuple[Path, ...]:
        """Write all staged files, with durable roll-forward crash recovery."""
        if not self._staged:
            return ()
        try:
            self.plan_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PlanError(f"failed to stage plan write: {error}") from error
        plans_transaction.recover_pending_plan_transaction(self.plan_dir)
        transaction_dir = self.plan_dir / plans_transaction.PLAN_TRANSACTION_DIR_NAME
        staging_dir = (
            self.plan_dir / plans_transaction.PLAN_TRANSACTION_STAGING_DIR_NAME
        )
        cleanup_dir = (
            self.plan_dir / plans_transaction.PLAN_TRANSACTION_CLEANUP_DIR_NAME
        )
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(cleanup_dir, ignore_errors=True)
            staging_dir.mkdir()
            entries: list[dict[str, str]] = []
            for index, item in enumerate(self._staged):
                try:
                    relative_path = item.path.relative_to(self.plan_dir).as_posix()
                except ValueError as error:
                    raise PlanError(
                        f"staged plan path escapes the plan directory: {item.path}"
                    ) from error
                content = item.text.encode("utf-8")
                payload_name = f"{index:06d}.bin"
                payload_path = staging_dir / payload_name
                with payload_path.open("wb") as handle:
                    handle.write(content)
                    handle.flush()
                    durability.fullsync(handle.fileno())
                entries.append(
                    {
                        "path": relative_path,
                        "payload": payload_name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            journal_temp = (
                staging_dir / f".{plans_transaction.PLAN_TRANSACTION_JOURNAL_NAME}.tmp"
            )
            with journal_temp.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": plans_transaction.PLAN_TRANSACTION_SCHEMA_VERSION,
                        "entries": entries,
                    },
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                durability.fullsync(handle.fileno())
            os.replace(
                journal_temp,
                staging_dir / plans_transaction.PLAN_TRANSACTION_JOURNAL_NAME,
            )
            plans_files._fsync_directory(staging_dir)
        except (OSError, PlanError) as error:
            shutil.rmtree(staging_dir, ignore_errors=True)
            if isinstance(error, PlanError):
                raise
            raise PlanError(f"failed to stage plan write: {error}") from error

        try:
            with plans_transaction._plan_transaction_mutex(staging_dir):
                return self._publish_staged_transaction(
                    transaction_dir=transaction_dir,
                    staging_dir=staging_dir,
                )
        except (OSError, PlanError) as error:
            shutil.rmtree(staging_dir, ignore_errors=True)
            if isinstance(error, PlanError):
                raise
            raise PlanError(f"failed to stage plan write: {error}") from error

    def _publish_staged_transaction(
        self, *, transaction_dir: Path, staging_dir: Path
    ) -> tuple[Path, ...]:
        """Publish prepared payloads while holding their transaction mutex."""
        try:
            os.replace(staging_dir, transaction_dir)
            plans_files._fsync_directory(self.plan_dir)
        except OSError as error:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise PlanError(f"failed to stage plan write: {error}") from error

        backups: list[tuple[Path, bytes | None]] = []
        replaced: list[Path] = []
        install_temps: list[Path] = []
        try:
            for index, item in enumerate(self._staged):
                original = _read_bytes_if_exists(item.path)
                backups.append((item.path, original))
                item.path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = item.path.with_name(
                    f".{item.path.name}.asl-transaction-install.tmp"
                )
                install_temps.append(temp_path)
                content = (transaction_dir / f"{index:06d}.bin").read_bytes()
                with temp_path.open("wb") as handle:
                    handle.write(content)
                    handle.flush()
                    durability.fullsync(handle.fileno())
                os.replace(temp_path, item.path)
                plans_files._fsync_directory(item.path.parent)
                replaced.append(item.path)
        except OSError as error:
            rolled_back = self._rollback(backups, replaced, install_temps)
            if rolled_back:
                with contextlib.suppress(OSError):
                    plans_transaction._retire_plan_transaction(
                        self.plan_dir, transaction_dir
                    )
            raise PlanError(f"failed to commit plan write: {error}") from error

        try:
            plans_transaction._retire_plan_transaction(self.plan_dir, transaction_dir)
        except OSError as error:
            raise PlanError(
                f"plan write committed but transaction cleanup failed: {error}"
            ) from error
        return tuple(item.path for item in self._staged)

    @staticmethod
    def _rollback(
        backups: list[tuple[Path, bytes | None]],
        replaced: list[Path],
        temp_paths: list[Path],
    ) -> bool:
        succeeded = True
        replaced_set = set(replaced)
        for path, original in backups:
            if path not in replaced_set:
                continue
            if original is None:
                _silent_unlink(path)
            else:
                try:
                    durability.replace_bytes(path, original)
                except OSError:
                    succeeded = False
        for tmp_path in temp_paths:
            _silent_unlink(tmp_path)
        return succeeded


def _read_bytes_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _silent_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
