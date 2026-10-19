# ruff: noqa: F403, F405
from .workbench_details_models import *
from .workbench_details_models import _JSONDict


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


def _read_gzip_window(path: Path, window: Window) -> _JSONDict | None:
    """Bounded-window read over a gzip artifact.

    Gzip cannot be seeked, so the artifact is decompressed fully and the same
    offset/tail/max_bytes windowing is applied to the decompressed bytes. The
    payload shape matches :func:`read_log_window` so callers are agnostic to
    the on-disk encoding.
    """
    try:
        data = artifacts.read_gzip(path)
    except artifacts.ArtifactError as error:
        if not path.exists():
            return None
        return _window_error(path, error, exists=True, window=window)
    size = len(data)
    read_offset = (
        window.offset if window.offset is not None else max(0, size - window.max_bytes)
    )
    read_offset = min(read_offset, size)
    return _window_payload(
        path,
        data[read_offset : read_offset + window.max_bytes],
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
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as error:
        return _window_error(path, error, exists=False, window=window)
    read_offset = (
        window.offset if window.offset is not None else max(0, size - window.max_bytes)
    )
    read_offset = min(read_offset, size)
    try:
        with path.open("rb") as handle:
            handle.seek(read_offset)
            raw = handle.read(window.max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — treat as a
        # graceful 404 rather than surfacing the race as a half-filled
        # payload.
        return None
    except OSError as error:
        payload = _window_error(path, error, exists=True, window=window)
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

    Gzip artifacts are decompressed transparently before the byte bound is
    applied so plain and gzip logs read through the same API.
    """
    if is_gzip_artifact(path):
        try:
            data = artifacts.read_gzip(path)
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
        raw = data[:max_bytes]
        return {
            "path": str(path),
            "exists": True,
            "size": len(data),
            "truncated": len(raw) < len(data),
            "text": raw.decode("utf-8", errors="replace"),
        }
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": False,
            "size": None,
            "error": str(error),
            "truncated": False,
            "text": "",
        }
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — surface as a
        # clean 404 so the operator UI does not see a fake successful
        # zero-byte response.
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": True,
            "size": size,
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


def _missing(path: Path, description: str) -> DetailError:
    return DetailError(
        details.REASON_NOT_FOUND,
        f"{description} does not exist: {path}",
        details={"path": str(path)},
    )
