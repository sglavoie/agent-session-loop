# ruff: noqa: F403, F405
import gzip
import os
import stat
import zlib
from typing import BinaryIO

from .workbench_details_models import *
from .workbench_details_models import _JSONDict

#: How much decompressed output one read of a gzip stream asks for. A window
#: is assembled from these, so a read holds at most the window plus one chunk.
_GZIP_CHUNK_BYTES = 64 * 1024


def _open_regular_file(path: Path) -> BinaryIO:
    """Open an artifact without blocking on a FIFO substituted at its path."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


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


def _read_gzip_slice(
    path: Path, *, offset: int | None, max_bytes: int
) -> tuple[bytes, int, int]:
    """Stream a gzip artifact and keep only the window a read asked for.

    Returns ``(data, size, read_offset)`` exactly as slicing the fully
    decompressed bytes would: ``offset`` keeps ``[offset, offset + max_bytes)``
    and ``None`` keeps the trailing ``max_bytes``. The stream is still read to
    its end, because ``size`` (and so ``truncated_tail``) is the decompressed
    length and a corrupt trailer is still refused, but memory stays bounded by
    the window: the bytes outside it are counted and dropped.

    Raises :class:`artifacts.ArtifactError` worded as
    :func:`artifacts.read_gzip` words it, so callers cannot tell the two apart.
    """
    kept = bytearray()
    size = 0
    try:
        with (
            _open_regular_file(path) as source,
            gzip.GzipFile(fileobj=source, mode="rb") as stream,
        ):
            while chunk := stream.read(_GZIP_CHUNK_BYTES):
                chunk_start = size
                size += len(chunk)
                if offset is None:
                    kept += chunk
                    excess = len(kept) - max_bytes
                    if excess > 0:
                        del kept[:excess]
                    continue
                low = max(offset, chunk_start)
                high = min(offset + max_bytes, size)
                if low < high:
                    kept += chunk[low - chunk_start : high - chunk_start]
    except (EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise artifacts.ArtifactError(
            f"artifact {path} is not valid gzip data: {error}"
        ) from error
    except OSError as error:
        raise artifacts.ArtifactError(
            f"could not read artifact {path}: {error}"
        ) from error
    read_offset = offset if offset is not None else max(0, size - max_bytes)
    return bytes(kept), size, min(read_offset, size)


def _read_gzip_window(path: Path, window: Window) -> _JSONDict | None:
    """Bounded-window read over a gzip artifact.

    Gzip cannot be seeked, so the artifact is streamed and the same
    offset/tail/max_bytes windowing is applied to the decompressed bytes as
    they pass. The payload shape matches :func:`read_log_window` so callers
    are agnostic to the on-disk encoding.
    """
    try:
        data, size, read_offset = _read_gzip_slice(
            path, offset=window.offset, max_bytes=window.max_bytes
        )
    except artifacts.ArtifactError as error:
        if not path.exists():
            return None
        return _window_error(path, error, exists=True, window=window)
    return _window_payload(
        path,
        data,
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
    size = None
    read_offset = 0
    try:
        with _open_regular_file(path) as handle:
            size = os.fstat(handle.fileno()).st_size
            read_offset = (
                window.offset
                if window.offset is not None
                else max(0, size - window.max_bytes)
            )
            read_offset = min(read_offset, size)
            handle.seek(read_offset)
            raw = handle.read(window.max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — treat as a
        # graceful 404 rather than surfacing the race as a half-filled
        # payload.
        return None
    except OSError as error:
        payload = _window_error(path, error, exists=path.exists(), window=window)
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

    Gzip artifacts are decompressed transparently, with the byte bound applied
    as they stream, so plain and gzip logs read through the same API.
    """
    if is_gzip_artifact(path):
        try:
            raw, size, _ = _read_gzip_slice(path, offset=0, max_bytes=max_bytes)
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
        return {
            "path": str(path),
            "exists": True,
            "size": size,
            "truncated": len(raw) < size,
            "text": raw.decode("utf-8", errors="replace"),
        }
    file_size = None
    try:
        with _open_regular_file(path) as handle:
            file_size = os.fstat(handle.fileno()).st_size
            raw = handle.read(max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — surface as a
        # clean 404 so the operator UI does not see a fake successful
        # zero-byte response.
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": path.exists(),
            "size": file_size,
            "error": str(error),
            "truncated": False,
            "text": "",
        }
    return {
        "path": str(path),
        "exists": True,
        "size": file_size,
        "truncated": len(raw) < file_size,
        "text": raw.decode("utf-8", errors="replace"),
    }


def _missing(path: Path, description: str) -> DetailError:
    return DetailError(
        details.REASON_NOT_FOUND,
        f"{description} does not exist: {path}",
        details={"path": str(path)},
    )
