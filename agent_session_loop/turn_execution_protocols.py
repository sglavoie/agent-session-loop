"""The collaborator protocols of turn execution and its progress notifications.

The two collaborators ``run_loop`` owns -- serialized persistence and
human-facing progress -- enter through :class:`TurnPersistence` and
:class:`TurnProgress`, which is what lets the boundary be typed without
importing the runner."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from . import (
    agent_mux_delivery,
)


class TurnPersistence(Protocol):
    """The run's serialized payload writer, as the turn boundary needs it.

    Declared structurally rather than imported so this module never depends on
    the runner that owns the concrete implementation. ``lock`` is the run's
    reentrant mutex: every state write and event append below is taken under
    it, and it is always acquired *after* ``LiveTurn._lock``.
    """

    @property
    def lock(self) -> AbstractContextManager[Any]: ...

    def append_event(
        self, event_path: Path, name: str, payload: dict[str, Any]
    ) -> None: ...

    def write_state(self, payload: dict[str, Any]) -> None: ...


class TurnProgress(Protocol):
    """The run's human-facing progress reporter, as the turn boundary calls it.

    Progress output is deliberately outside every lock the turn holds, so a
    slow terminal can never stall a state write.
    """

    def turn_started(self, active_turn: dict[str, Any]) -> None: ...

    def turn_finished(self, turn: dict[str, Any]) -> None: ...

    def heartbeat(self, heartbeat: dict[str, Any]) -> None: ...

    def action_required(self, signal: dict[str, Any]) -> None: ...


def notify_turn_started(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    persistence: TurnPersistence | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_START_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_START_REASON,
        active_turn=active_turn,
        persistence=persistence,
    )


def notify_turn_heartbeat(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    persistence: TurnPersistence | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_HEARTBEAT_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_HEARTBEAT_REASON,
        active_turn=active_turn,
        persistence=persistence,
        cancelled=cancelled,
    )


def notify_turn_finished(
    payload: dict[str, Any],
    finished_turn: dict[str, Any],
    persistence: TurnPersistence | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_FINISH_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_FINISH_REASON,
        active_turn=finished_turn,
        persistence=persistence,
    )


def notify_turn_hang_suspected(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    *,
    hang_reason: str,
    hang_idle_seconds: float | None,
    harness_pid: int | None,
    persistence: TurnPersistence | None = None,
) -> None:
    # Bounded scalars only; the sampled process tree stays in
    # state.json / events.jsonl rather than riding the notification.
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_HANG_SUSPECTED_REASON,
        active_turn=active_turn,
        extra={
            "hang_reason": hang_reason,
            "hang_idle_seconds": hang_idle_seconds,
            "harness_pid": harness_pid,
        },
        persistence=persistence,
    )


def notify_turn_hang_cleared(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    *,
    hang_reason: str,
    hang_idle_seconds: float | None,
    harness_pid: int | None,
    persistence: TurnPersistence | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_HANG_CLEARED_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_HANG_CLEARED_REASON,
        active_turn=active_turn,
        extra={
            "hang_reason": hang_reason,
            "hang_idle_seconds": hang_idle_seconds,
            "harness_pid": harness_pid,
        },
        persistence=persistence,
    )
