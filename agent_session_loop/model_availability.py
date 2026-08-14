"""Durable shared model-availability ledger for optional model routing."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import durability, retry_hints

LEDGER_VERSION = 1
LEDGER_FILENAME = "model-availability.json"
LOCK_FILENAME = "model-availability.lock"


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str
    retry_at: str | None = None
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "availability": self.available,
            "reason": self.reason,
            "retry_at": self.retry_at,
            "warning": self.warning,
        }


def _now() -> datetime:
    return datetime.now(UTC)


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {"version": LEDGER_VERSION, "models": {}}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return None, f"model availability ledger is unreadable: {error}"
    if (
        not isinstance(payload, dict)
        or payload.get("version") != LEDGER_VERSION
        or not isinstance(payload.get("models"), dict)
    ):
        return (
            None,
            "model availability ledger is malformed or has an unsupported version",
        )
    return payload, None


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            durability.fullsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            durability.fullsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def availability(
    root: str | Path,
    model: str,
    *,
    now: datetime | None = None,
) -> Availability:
    """Read availability, removing an expired cooldown under the stable lock."""
    state_root = Path(root)
    current = now or _now()
    path = state_root / LEDGER_FILENAME
    with _locked(state_root):
        payload, warning = _read(path)
        if payload is None:
            return Availability(False, "ledger-unreadable", warning=warning)
        models = payload["models"]
        entry = models.get(model)
        if not isinstance(entry, dict):
            return Availability(True, "available")
        retry_at = entry.get("retry_at")
        reset = retry_hints.resolve_retry_at(retry_at, now=current)
        if reset is None:
            return Availability(False, str(entry.get("reason") or "cooldown"))
        if reset <= current:
            models.pop(model, None)
            _write(path, payload)
            return Availability(True, "cooldown-expired")
        return Availability(
            False,
            str(entry.get("reason") or "cooldown"),
            retry_at=str(retry_at),
        )


def suppress_until(
    root: str | Path,
    model: str,
    *,
    reason: str,
    retry_hint: object,
    run_id: str,
    now: datetime | None = None,
) -> Availability | None:
    """Persist a cooldown when ``retry_hint`` resolves; otherwise return None."""
    current = now or _now()
    retry_at = retry_hints.normalized_retry_at(retry_hint, now=current)
    if retry_at is None:
        return None
    state_root = Path(root)
    path = state_root / LEDGER_FILENAME
    with _locked(state_root):
        payload, _warning = _read(path)
        if payload is None:
            payload = {"version": LEDGER_VERSION, "models": {}}
        payload["models"][model] = {
            "reason": reason,
            "retry_at": retry_at,
            "run_id": run_id,
            "updated_at": current.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        _write(path, payload)
    return Availability(False, reason, retry_at=retry_at)
