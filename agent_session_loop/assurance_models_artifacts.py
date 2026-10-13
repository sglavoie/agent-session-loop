"""Exclusive-create writers for canonical supervisor-audit artifacts."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path, PurePosixPath
from typing import Any

from . import durability
from .assurance_models_primitives import (
    _ARTIFACT_NAME_RE,
    AssuranceError,
    _require_int,
    canonical_json_bytes,
)
from .assurance_models_vocab import (
    ATTEMPT_DIR_PREFIX,
    MAX_ARTIFACT_BYTES,
    MAX_AUDIT_ATTEMPT_RECORDS,
)

# ---------------------------------------------------------------------------
# Exclusive-create artifact writers
# ---------------------------------------------------------------------------


def audit_artifact_name(name: str) -> str:
    """Validate ``name`` as a single, traversal-free artifact filename.

    Unlike :func:`agent_session_loop.artifacts.sanitize_artifact_name`, which
    rewrites an unsafe name into a safe one, this rejects it. A caller that
    produced ``../../etc/passwd`` has a bug, and quietly storing its bytes under
    a different name would hide that bug rather than stop it.
    """
    if not isinstance(name, str) or not _ARTIFACT_NAME_RE.match(name):
        raise AssuranceError(
            "audit artifact name must be a single filename of alphanumerics, "
            f"'.', '_', or '-' starting with an alphanumeric; got {name!r}"
        )
    if ".." in PurePosixPath(name).parts:
        raise AssuranceError(f"audit artifact name cannot traverse upward: {name!r}")
    return name


def attempt_dir_name(number: int) -> str:
    """Return the canonical directory name for audit attempt ``number``."""
    _require_int(
        number, label="attempt number", minimum=1, maximum=MAX_AUDIT_ATTEMPT_RECORDS
    )
    return f"{ATTEMPT_DIR_PREFIX}{number}"


def write_new_bytes(path: Path | str, data: bytes, *, durable: bool = True) -> Path:
    """Create ``path`` exclusively and write ``data`` to it.

    An existing destination is an error, never an overwrite: a second write to a
    canonical audit artifact means two audits disagree about one run, and the
    disagreement is the finding. On any failure the partial file is removed so a
    retry sees a clean slate. ``durable`` fullsyncs the file and its directory.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise AssuranceError("audit artifact data must be bytes")
    payload = bytes(data)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise AssuranceError(
            f"audit artifact {path} is {len(payload)} bytes, over the "
            f"{MAX_ARTIFACT_BYTES}-byte cap"
        )
    target = Path(path)
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as error:
        raise AssuranceError(
            f"audit artifact {target} already exists; canonical audit artifacts "
            "are written exactly once"
        ) from error
    except OSError as error:
        raise AssuranceError(
            f"could not create audit artifact {target}: {error}"
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if durable:
                durability.fullsync(handle.fileno())
    except OSError as error:
        with contextlib.suppress(OSError):
            target.unlink()
        raise AssuranceError(
            f"could not write audit artifact {target}: {error}"
        ) from error
    if durable:
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                durability.fullsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise AssuranceError(
                f"could not sync audit artifact directory {target.parent}: {error}"
            ) from error
    return target


def write_audit_artifact(
    run_dir: Path | str, name: str, data: bytes, *, durable: bool = True
) -> Path:
    """Write one canonical audit artifact into ``run_dir``, exactly once."""
    return write_new_bytes(
        Path(run_dir) / audit_artifact_name(name), data, durable=durable
    )


def write_audit_json(
    run_dir: Path | str, name: str, payload: Any, *, durable: bool = True
) -> Path:
    """Serialize ``payload`` canonically and write it as a new audit artifact."""
    return write_audit_artifact(
        run_dir, name, canonical_json_bytes(payload), durable=durable
    )


def create_attempt_dir(run_dir: Path | str, number: int) -> Path:
    """Create the attempt directory for ``number``, refusing to reuse one.

    A retry that lands in the previous attempt's directory would let the second
    invocation's artifacts sit beside — or on top of — the first's, which is
    exactly the evidence needed to explain why the retry happened.
    """
    target = Path(run_dir) / attempt_dir_name(number)
    try:
        target.mkdir(parents=True)
    except FileExistsError as error:
        raise AssuranceError(
            f"audit attempt directory {target} already exists; each attempt "
            "writes its own directory"
        ) from error
    except OSError as error:
        raise AssuranceError(
            f"could not create audit attempt directory {target}: {error}"
        ) from error
    return target
