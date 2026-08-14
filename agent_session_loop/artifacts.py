"""Bounded output artifact primitives.

Store command/stream output without embedding unbounded strings in JSON. The
module provides small, independently tested building blocks:

- atomic byte/gzip writers (temp-file + :func:`os.replace`, mirroring the
  established :func:`agent_session_loop.plans._atomic_write_text` pattern);
- SHA-256 digests, byte counts, and UTF-8 character counts;
- bounded UTF-8 previews built from the first and last 8 KiB of a stream;
- versioned :class:`OutputMetadata` describing a captured stream — preview text,
  truncation flag, original size, digest, and an optional gzip artifact path;
- :class:`StreamCapture`, the incremental counterpart of
  :func:`store_stream_output` that bounds a stream chunk by chunk so callers
  never hold the full output in memory.

The full stream is retained (gzip-compressed, byte-for-byte recoverable) only
when the caller marks the check as failed; passing output produces metadata but
never a full raw artifact. Verification streams command output through
:class:`StreamCapture` instead of buffering entire stdout/stderr strings in
memory.
"""

from __future__ import annotations

import codecs
import contextlib
import gzip
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import durability

# Previews standardize on the first 8 KiB and last 8 KiB of each stream.
PREVIEW_HEAD_BYTES = 8 * 1024
PREVIEW_TAIL_BYTES = 8 * 1024

# Marker inserted between the head and tail slices of a truncated preview.
PREVIEW_TRUNCATION_MARKER = "\n…[truncated]…\n"

# Versioned so v1 readers can detect and reject future metadata shapes.
OUTPUT_METADATA_VERSION = 1

# gzip's own header timestamp is pinned so identical input yields identical
# bytes; recovery only relies on decompression, but determinism keeps digests
# of the compressed artifact stable across runs.
_GZIP_MTIME = 0

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_ARTIFACT_NAME = "output"

# Hex digits of the SHA-256 digest appended to a lossy-sanitized stem. The
# full 64-character digest is used because short prefixes collide in practice
# (e.g. "a$%#:??/b-stdout" and "a%%#%$:/b-stdout" share the first 8 hex
# chars), and a collision silently overwrites the other check's artifact.
_ARTIFACT_DIGEST_CHARS = 64

# Per-component filename byte limit on the filesystems we target (NAME_MAX).
_MAX_FILENAME_COMPONENT_BYTES = 255

# The longest generated form of a stem is the atomic-write temp name of its
# gzip artifact, ``.{stem}.gz.tmp``; reserve those decoration bytes so the
# stem itself can never push a component past the limit.
_ARTIFACT_STEM_MAX_BYTES = _MAX_FILENAME_COMPONENT_BYTES - (
    len(".") + len(".gz") + len(".tmp")
)

# Budget for the human-readable prefix of a digest-suffixed stem: the stem
# budget minus the ``-{digest}`` suffix.
_ARTIFACT_PREFIX_MAX_BYTES = _ARTIFACT_STEM_MAX_BYTES - (
    len("-") + _ARTIFACT_DIGEST_CHARS
)


class ArtifactError(Exception):
    """Raised when an artifact cannot be written or read."""


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _cleanup(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def atomic_write_bytes(path: Path | str, data: bytes, *, durable: bool = False) -> None:
    """Write ``data`` to ``path`` via temp-file + :func:`os.replace`.

    The destination is never left half-written: on any failure the temp file is
    removed and the original destination (if any) is untouched. When
    ``durable`` is true, the temporary file receives a platform fullsync before
    replacement and the containing directory is synced afterward.
    """
    path = Path(path)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        if durable:
            with tmp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                durability.fullsync(handle.fileno())
        else:
            tmp_path.write_bytes(data)
    except OSError as error:
        _cleanup(tmp_path)
        raise ArtifactError(f"could not write artifact {path}: {error}") from error
    try:
        os.replace(tmp_path, path)
    except OSError as error:
        _cleanup(tmp_path)
        raise ArtifactError(f"could not finalize artifact {path}: {error}") from error
    if durable:
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                durability.fullsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise ArtifactError(
                f"could not sync artifact directory {path.parent}: {error}"
            ) from error


def gzip_compress(data: bytes) -> bytes:
    """Deterministically gzip ``data`` exactly as :func:`atomic_write_gzip` stores it.

    The pinned mtime keeps the output byte-identical for identical input, so
    callers can project an artifact's on-disk size without writing it.
    """
    return gzip.compress(data, mtime=_GZIP_MTIME)


def atomic_write_gzip(path: Path | str, data: bytes) -> None:
    """Gzip-compress ``data`` and write it atomically to ``path``."""
    atomic_write_bytes(path, gzip_compress(data))


def read_gzip(path: Path | str) -> bytes:
    """Return the byte-for-byte contents of a gzip artifact written by this module."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ArtifactError(f"could not read artifact {path}: {error}") from error
    try:
        return gzip.decompress(raw)
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ArtifactError(
            f"artifact {path} is not valid gzip data: {error}"
        ) from error


def sanitize_artifact_name(name: str) -> str:
    """Reduce ``name`` to a safe, single-path-segment artifact stem.

    Path separators, ``..`` sequences, and other unsafe characters collapse to
    ``-``; leading/trailing dots and dashes are stripped so the result cannot
    collide with the ``.<name>.tmp`` temp files or escape its directory. Falls
    back to ``output`` when nothing usable remains.
    """
    collapsed = _UNSAFE_NAME_RE.sub("-", name.strip())
    trimmed = collapsed.strip("-._")
    return trimmed or _DEFAULT_ARTIFACT_NAME


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` encoded UTF-8 bytes.

    The cut never splits a multi-byte sequence: any incomplete trailing
    sequence produced by the byte slice is dropped rather than decoded.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def derive_artifact_stem(name: str | None) -> str:
    """Return a collision-resistant, filesystem-safe artifact stem for ``name``.

    When :func:`sanitize_artifact_name` is lossless and the stem fits the
    per-component filename byte budget (including its ``.gz`` extension and
    atomic-write ``.{stem}.gz.tmp`` decoration), the stem is ``name`` itself,
    preserving the established ``{check}-stdout`` naming for safe names.
    Otherwise — sanitization rewrote or dropped characters, or the name is too
    long to store as-is — distinct names could collapse to one stem, so the
    readable prefix is byte-capped and the full SHA-256 hex digest of the
    original name is appended to keep distinct names on distinct paths. The
    digest covers the untruncated original, so distinct over-long names still
    derive distinct stems. The derivation is idempotent: applying it to its
    own output returns the output unchanged.

    Live verification and state compaction must both derive failure-artifact
    paths through this function so their names agree for the same check name.
    """
    raw = name or _DEFAULT_ARTIFACT_NAME
    sanitized = sanitize_artifact_name(raw)
    if sanitized == raw and len(sanitized.encode("utf-8")) <= _ARTIFACT_STEM_MAX_BYTES:
        return sanitized
    prefix = _truncate_utf8(sanitized, _ARTIFACT_PREFIX_MAX_BYTES)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:_ARTIFACT_DIGEST_CHARS]}"


def bounded_preview(
    data: bytes,
    *,
    head_bytes: int = PREVIEW_HEAD_BYTES,
    tail_bytes: int = PREVIEW_TAIL_BYTES,
) -> tuple[str, bool]:
    """Return ``(preview, truncated)`` for ``data``.

    When ``data`` fits within ``head_bytes + tail_bytes`` the whole stream is
    decoded and ``truncated`` is ``False``. Otherwise the first ``head_bytes``
    and last ``tail_bytes`` are decoded (``errors="replace"`` so a byte slice
    that splits a multi-byte sequence still yields text) and joined with
    :data:`PREVIEW_TRUNCATION_MARKER`.
    """
    if len(data) <= head_bytes + tail_bytes:
        return data.decode("utf-8", errors="replace"), False
    head = data[:head_bytes].decode("utf-8", errors="replace")
    tail = data[-tail_bytes:].decode("utf-8", errors="replace")
    return f"{head}{PREVIEW_TRUNCATION_MARKER}{tail}", True


@dataclass(frozen=True)
class OutputMetadata:
    """Versioned, bounded description of a captured output stream."""

    version: int
    byte_count: int
    char_count: int
    sha256: str
    truncated: bool
    preview: str
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "byte_count": self.byte_count,
            "char_count": self.char_count,
            "sha256": self.sha256,
            "truncated": self.truncated,
            "preview": self.preview,
            "artifact_path": self.artifact_path,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OutputMetadata:
        version = payload.get("version")
        if version != OUTPUT_METADATA_VERSION:
            raise ArtifactError(
                f"unsupported output-metadata version {version!r}; "
                f"expected {OUTPUT_METADATA_VERSION}"
            )
        return cls(
            version=version,
            byte_count=payload["byte_count"],
            char_count=payload["char_count"],
            sha256=payload["sha256"],
            truncated=payload["truncated"],
            preview=payload["preview"],
            artifact_path=payload.get("artifact_path"),
        )


def store_stream_output(
    data: bytes,
    *,
    failed: bool,
    artifact_dir: Path | str | None = None,
    artifact_name: str | None = None,
    head_bytes: int = PREVIEW_HEAD_BYTES,
    tail_bytes: int = PREVIEW_TAIL_BYTES,
) -> OutputMetadata:
    """Build :class:`OutputMetadata` for ``data``.

    The full stream is gzip-retained only when ``failed`` is true and
    ``artifact_dir`` is provided; passing output records bounded metadata but
    never writes a full raw artifact. ``artifact_name`` is reduced to a
    collision-resistant stem via :func:`derive_artifact_stem` and given a
    ``.gz`` suffix.
    """
    byte_count = len(data)
    char_count = len(data.decode("utf-8", errors="replace"))
    digest = sha256_hex(data)
    preview, truncated = bounded_preview(
        data, head_bytes=head_bytes, tail_bytes=tail_bytes
    )

    artifact_path: str | None = None
    if failed and artifact_dir is not None:
        directory = Path(artifact_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactError(
                f"could not create artifact directory {directory}: {error}"
            ) from error
        stem = derive_artifact_stem(artifact_name)
        target = directory / f"{stem}.gz"
        atomic_write_gzip(target, data)
        artifact_path = str(target)

    return OutputMetadata(
        version=OUTPUT_METADATA_VERSION,
        byte_count=byte_count,
        char_count=char_count,
        sha256=digest,
        truncated=truncated,
        preview=preview,
        artifact_path=artifact_path,
    )


def _promote_spool(
    spool_path: Path,
    *,
    artifact_dir: Path | str,
    artifact_name: str | None,
) -> str:
    """Move a finished gzip spool file into ``artifact_dir`` atomically.

    The spool lives in the system temp directory (possibly another
    filesystem), so it is first moved next to the target and then swapped in
    via :func:`os.replace`, preserving the never-half-written guarantee of
    :func:`atomic_write_bytes`.
    """
    directory = Path(artifact_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ArtifactError(
            f"could not create artifact directory {directory}: {error}"
        ) from error
    stem = derive_artifact_stem(artifact_name)
    target = directory / f"{stem}.gz"
    tmp_path = target.with_name(f".{target.name}.tmp")
    try:
        shutil.move(str(spool_path), str(tmp_path))
        os.replace(tmp_path, target)
    except OSError as error:
        _cleanup(tmp_path)
        raise ArtifactError(f"could not finalize artifact {target}: {error}") from error
    return str(target)


class StreamCapture:
    """Incrementally bound a byte stream into :class:`OutputMetadata`.

    Chunks fed via :meth:`feed` update a running byte count, SHA-256 digest,
    UTF-8 character count, and the first/last preview windows, so peak
    retained memory stays at the preview bounds regardless of stream size.
    With ``spool=True`` every chunk is also gzip-spooled to a temp file so
    :meth:`finalize` can retain the full stream as a failure artifact without
    ever holding it in memory; the artifact directory itself is only created
    (and the spool promoted into it) when the capture finalizes as failed.
    Spool IO errors fail open: metadata is still produced, just with no
    artifact path.

    :meth:`finalize` yields the same digest, counts, preview, truncation
    flag, and (for failures) byte-for-byte recoverable gzip artifact as
    :func:`store_stream_output` over the concatenated chunks.
    """

    def __init__(
        self,
        *,
        head_bytes: int = PREVIEW_HEAD_BYTES,
        tail_bytes: int = PREVIEW_TAIL_BYTES,
        spool: bool = False,
    ) -> None:
        self._head_bytes = head_bytes
        self._tail_bytes = tail_bytes
        self._byte_count = 0
        self._char_count = 0
        self._hasher = hashlib.sha256()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._head = bytearray()
        self._tail = bytearray()
        self._finalized = False
        self._spool_handle: Any | None = None
        self._spool_gzip: gzip.GzipFile | None = None
        self._spool_path: Path | None = None
        if spool:
            self._open_spool()

    def _open_spool(self) -> None:
        try:
            handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed by _drop_spool/finalize, outlives this scope
                prefix="asl-stream-spool-", suffix=".gz", delete=False
            )
            self._spool_handle = handle
            self._spool_path = Path(handle.name)
            # ``filename=""`` keeps the gzip header name-free and — with the
            # pinned mtime — byte-identical to :func:`atomic_write_gzip` for
            # the same input.
            self._spool_gzip = gzip.GzipFile(
                filename="", fileobj=handle, mode="wb", mtime=_GZIP_MTIME
            )
        except OSError:
            self._drop_spool()

    def _drop_spool(self) -> None:
        if self._spool_gzip is not None:
            with contextlib.suppress(OSError):
                self._spool_gzip.close()
        if self._spool_handle is not None:
            with contextlib.suppress(OSError):
                self._spool_handle.close()
        if self._spool_path is not None:
            _cleanup(self._spool_path)
        self._spool_gzip = None
        self._spool_handle = None
        self._spool_path = None

    def _close_spool(self) -> Path | None:
        """Flush and close the spool, returning its path (``None`` if broken)."""
        if self._spool_gzip is None or self._spool_path is None:
            self._drop_spool()
            return None
        path = self._spool_path
        try:
            self._spool_gzip.close()
            if self._spool_handle is not None:
                self._spool_handle.close()
        except OSError:
            self._drop_spool()
            return None
        self._spool_gzip = None
        self._spool_handle = None
        self._spool_path = None
        return path

    def feed(self, chunk: bytes) -> None:
        """Fold ``chunk`` into the running digest, counts, preview, and spool."""
        if self._finalized:
            raise ArtifactError("stream capture already finalized")
        if not chunk:
            return
        self._byte_count += len(chunk)
        self._hasher.update(chunk)
        self._char_count += len(self._decoder.decode(chunk))
        if len(self._head) < self._head_bytes:
            self._head.extend(chunk[: self._head_bytes - len(self._head)])
        self._tail.extend(chunk)
        overflow = len(self._tail) - self._tail_bytes
        if overflow > 0:
            del self._tail[:overflow]
        if self._spool_gzip is not None:
            try:
                self._spool_gzip.write(chunk)
            except OSError:
                self._drop_spool()

    def _render_preview(self) -> tuple[str, bool]:
        if self._byte_count <= self._head_bytes + self._tail_bytes:
            # The head plus the trailing bytes beyond it reconstruct the whole
            # stream, mirroring :func:`bounded_preview`'s non-truncated branch.
            suffix_len = self._byte_count - len(self._head)
            data = bytes(self._head)
            if suffix_len > 0:
                data += bytes(self._tail[-suffix_len:])
            return data.decode("utf-8", errors="replace"), False
        head = bytes(self._head).decode("utf-8", errors="replace")
        tail = bytes(self._tail).decode("utf-8", errors="replace")
        return f"{head}{PREVIEW_TRUNCATION_MARKER}{tail}", True

    def finalize(
        self,
        *,
        failed: bool,
        artifact_dir: Path | str | None = None,
        artifact_name: str | None = None,
    ) -> OutputMetadata:
        """Close the capture and return its bounded :class:`OutputMetadata`.

        The spooled full stream is promoted into ``artifact_dir`` only when
        ``failed`` is true and a directory is given; otherwise (and on any
        promotion error, failing open) the spool is deleted.
        """
        if self._finalized:
            raise ArtifactError("stream capture already finalized")
        self._finalized = True
        self._char_count += len(self._decoder.decode(b"", final=True))
        preview, truncated = self._render_preview()
        spool_path = self._close_spool()
        artifact_path: str | None = None
        if spool_path is not None:
            if failed and artifact_dir is not None:
                try:
                    artifact_path = _promote_spool(
                        spool_path,
                        artifact_dir=artifact_dir,
                        artifact_name=artifact_name,
                    )
                except ArtifactError:
                    artifact_path = None
            if artifact_path is None:
                _cleanup(spool_path)
        return OutputMetadata(
            version=OUTPUT_METADATA_VERSION,
            byte_count=self._byte_count,
            char_count=self._char_count,
            sha256=self._hasher.hexdigest(),
            truncated=truncated,
            preview=preview,
            artifact_path=artifact_path,
        )

    def discard(self) -> None:
        """Abandon the capture, releasing the spool without metadata."""
        self._finalized = True
        self._drop_spool()
