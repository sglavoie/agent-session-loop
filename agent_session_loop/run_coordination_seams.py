"""Collaborator seams run coordination takes from the run loop without importing it."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol


class CoordinationPersistence(Protocol):
    """The run's serialized payload writer, as coordination needs it.

    Declared structurally rather than imported so this module never depends on
    the runner that owns the concrete implementation. ``lock`` is the run's
    reentrant mutex and the outermost lock every durable write below takes;
    it is never held across a mailbox read, a lease read, a sleep, or the audit
    subprocess.
    """

    @property
    def lock(self) -> AbstractContextManager[Any]: ...

    def append_event(
        self, event_path: Path, name: str, payload: dict[str, Any]
    ) -> None: ...

    def write_state(self, payload: dict[str, Any]) -> None: ...


class WaitNotifier(Protocol):
    """The fail-open AgentMux notification seam a live wait drives.

    Both calls are made outside every coordination lock and neither may raise:
    a lifecycle notification is advisory, and losing one must never end a wait
    that a supervisor is still answering.
    """

    def wait_started(self, *, question_id: str, question_text: str | None) -> None: ...

    def wait_ended(
        self, *, question_id: str, wait_reason: str, closure_id: str | None
    ) -> None: ...
