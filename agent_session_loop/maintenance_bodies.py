"""Reproducible log bodies: discovery, ledgers, selection, and compression."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import durability
from .maintenance_policy import (
    _CHUNK_BYTES,
    BODY_LEDGER_SUFFIX,
    LOG_BODY_NAMES,
    _file_size,
)


def _iter_turn_dirs(run_dir: Path) -> list[Path]:
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return []
    return sorted(
        (child for child in children if child.is_dir() and not child.is_symlink()),
        key=lambda child: child.name,
    )


@dataclass(frozen=True)
class _Body:
    """One reproducible body on disk, in whichever form it currently has."""

    source: Path
    stored: Path
    compressed: bool
    stored_bytes: int


@dataclass(frozen=True)
class _BodyOutcome:
    """What one compression left behind: a compacted body, or why it stopped.

    ``body`` is the compacted replacement when the whole operation landed.
    ``blocked`` is non-empty when protection arrived mid-flight, which is not a
    failure — nothing is broken and nothing is lost — but does end this run's
    compaction. Both empty means the compression failed and said so in
    ``failures``.
    """

    body: _Body | None
    blocked: str


def _iter_bodies(run_dir: Path) -> list[_Body]:
    """Return every reproducible body of ``run_dir`` in a deterministic order.

    A body already removed by an earlier pass is skipped: its ledger records it
    and there is nothing left to act on.
    """
    bodies: list[_Body] = []
    for turn_dir in _iter_turn_dirs(run_dir):
        for name in LOG_BODY_NAMES:
            raw = turn_dir / name
            packed = turn_dir / f"{name}.gz"
            if raw.is_file() and not raw.is_symlink():
                bodies.append(
                    _Body(
                        source=raw,
                        stored=raw,
                        compressed=False,
                        stored_bytes=_file_size(raw),
                    )
                )
            elif packed.is_file() and not packed.is_symlink():
                bodies.append(
                    _Body(
                        source=raw,
                        stored=packed,
                        compressed=True,
                        stored_bytes=_file_size(packed),
                    )
                )
    return sorted(bodies, key=lambda body: str(body.source))


def _ledger_path(source: Path) -> Path:
    return source.with_name(f"{source.name}{BODY_LEDGER_SUFFIX}")


def _read_ledger(source: Path) -> dict[str, Any]:
    path = _ledger_path(source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_ledger(source: Path, payload: dict[str, Any]) -> None:
    """Persist a body's metadata durably, before the body itself can go.

    Durable and not merely atomic: the ordering that makes an interrupted pass
    safe is "metadata is on the platter, then bytes are removed", and only a
    completed fsync of both the file and its directory establishes that.
    """
    durability.write_json_file(_ledger_path(source), payload, durable=True)


def _over_budget_selection(
    bodies: list[_Body], *, budget_bytes: int
) -> tuple[list[_Body], int]:
    """Pick the bodies to remove so the rest fit ``budget_bytes``.

    Largest first, ties broken by path, so the selection is a pure function of
    the sizes on disk: the same corpus plans the same removals on every host and
    on every repeat, and the fewest bodies are touched to reach the budget.
    """
    ordered = sorted(bodies, key=lambda body: (-body.stored_bytes, str(body.source)))
    retained = sum(body.stored_bytes for body in bodies)
    selected: list[_Body] = []
    removable = 0
    for body in ordered:
        if retained <= budget_bytes:
            break
        selected.append(body)
        retained -= body.stored_bytes
        removable += body.stored_bytes
    return selected, removable


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _stage_compressed(source: Path, target: Path) -> tuple[Path, str, int]:
    """Stream ``source`` into a deterministic gzip staged beside ``target``.

    Streaming and not ``gzip.compress``: a multi-gigabyte ``stderr.log`` is
    exactly the case this exists for, and reading one into memory to save it
    would be the wrong kind of maintenance. The digest is taken on the same
    pass, so the original is read once. ``mtime=0`` keeps identical input
    byte-identical output, which is what makes a repeated pass a no-op.

    Staging stops one file short of publication on purpose. Compressing a
    multi-gigabyte body is the longest thing this module does, and it is exactly
    the window in which a plan lock, a run, or an assurance pin can appear, so
    the caller gets the chance to re-read protection while the only thing on
    disk is a dot-prefixed temporary it can discard.
    """
    temporary = durability.staging_path(target)
    digest = hashlib.sha256()
    total = 0
    try:
        with (
            source.open("rb") as raw,
            durability.open_staging_file(temporary) as staged,
        ):
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=staged, mtime=0
            ) as compressed:
                while True:
                    chunk = raw.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    compressed.write(chunk)
            staged.flush()
            durability.fullsync(staged.fileno())
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return temporary, digest.hexdigest(), total


class _ByteCounter:
    """A write-only sink that keeps the length of what it was given and nothing else."""

    def __init__(self) -> None:
        self.count = 0

    def write(self, data: bytes) -> int:
        self.count += len(data)
        return len(data)

    def flush(self) -> None:
        return None


def _compressed_size(source: Path) -> int:
    """Return the exact size ``_stage_compressed`` would publish for ``source``.

    The per-run budget is measured after compression, so a projection that
    judges a raw body by its raw size plans removals ``--apply`` never makes.
    The same deterministic gzip is streamed into a counter rather than a file,
    which costs the compression but writes nothing and holds one chunk at most.
    """
    counter = _ByteCounter()
    sink = gzip.GzipFile(filename="", mode="wb", fileobj=counter, mtime=0)
    with source.open("rb") as raw, sink as compressed:
        while True:
            chunk = raw.read(_CHUNK_BYTES)
            if not chunk:
                break
            compressed.write(chunk)
    return counter.count


def _discard_staged(temporary: Path) -> None:
    """Drop a staged gzip that was never published, leaving the corpus as it was."""
    with contextlib.suppress(OSError):
        temporary.unlink()


def _compress_file(source: Path, target: Path) -> tuple[str, int, int]:
    """Stage ``source``'s gzip and publish it at ``target`` in one step."""
    temporary, digest, total = _stage_compressed(source, target)
    try:
        os.replace(temporary, target)
    except OSError:
        _discard_staged(temporary)
        raise
    return digest, total, _file_size(target)
