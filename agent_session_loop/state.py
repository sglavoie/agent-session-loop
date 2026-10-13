"""Operational state and locking boundaries.

Runner state exists to coordinate local execution, capture logs, and recover
from pauses. It must never replace the plan files as the durable completion
record.

This module keeps the shared records, errors, path helpers and the small
primitives (pid liveness, string coercion, state serialization) every sibling
needs. Plan references,
locking, status payloads, abandonment, status output, schema upgrade and
migration live in the sibling ``state_*`` modules; callers import each symbol
from its owner."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    config,
    plans,
)

LOCK_FILE_NAME = "lock.json"


RUNS_DIR_NAME = "runs"


RESULT_FILE_NAME = "result.json"


TURN_DIR_PREFIX = "turn-"


STATE_MIGRATED_EVENT = "state_migrated"


class StateError(Exception):
    """Raised when operational state cannot be read or updated safely."""


class AmbiguousPlanError(StateError):
    """Raised when a bare plan label matches multiple plan directories.

    A subclass of :class:`StateError` so existing ``except StateError`` guards
    keep working, while callers that need the ``ambiguous-plan`` reason can
    catch this type first. Carries the sorted canonical candidates and the
    resolved state root that produced them.
    """

    def __init__(
        self,
        label: str,
        candidates: list[Path],
        state_root: Path,
    ) -> None:
        self.label = label
        self.candidates = sorted(candidates, key=str)
        self.state_root = state_root
        joined = ", ".join(str(candidate) for candidate in self.candidates)
        super().__init__(
            f"plan label {label!r} is ambiguous under {state_root}: matches {joined}"
        )


@dataclass(frozen=True)
class LockInfo:
    """Parsed operational lock state for one plan directory."""

    path: Path
    pid: int | None
    plan_dir: str
    created_at: str
    stale: bool
    reason: str
    raw: dict[str, Any]

    @property
    def active(self) -> bool:
        return not self.stale

    @property
    def owner_hostname(self) -> str | None:
        """Host that recorded ``pid``, or ``None`` for legacy locks."""
        return _nonempty_str(self.raw.get("hostname"))

    @property
    def owner_process_start(self) -> str | None:
        """Birth time recorded for ``pid``, or ``None`` if the lock names none.

        Distinguishes the process that published the lock from a later one that
        reused its pid, for readers that report on the owner. It is deliberately
        not an authentication token for acting *on* that process: ``ps`` reports
        a start time to one-second resolution, so two processes can present the
        same value, and any check of it is separated from whatever it authorizes
        by a window in which the pid can be recycled.
        """
        return _nonempty_str(self.raw.get("process_start"))

    @property
    def wakeup_endpoint(self) -> str | None:
        """FIFO this run reads control wakeups from, or ``None`` if it has none.

        Written by the owner before the lock became visible, so the path names
        an endpoint whose read end that owner already holds. ``None`` covers a
        version 2 lock, which predates the endpoint, and a runner whose state
        directory could not host a FIFO; both mean the durable queue poll is the
        only transport for that run.
        """
        return _nonempty_str(self.raw.get("wakeup_endpoint"))

    @property
    def redirect_endpoint(self) -> str | None:
        """FIFO this run reads urgent redirect wakeups from, or ``None``.

        Published on the same terms as :attr:`wakeup_endpoint` and kept
        separate from it so that "this run serves urgent redirects" is a fact a
        producer can read, rather than an inference from the presence of the
        control endpoint. ``None`` covers a lock below version 4 and a runner
        whose state directory could not host a second FIFO — and unlike a
        missing control endpoint it is not merely a latency cost, because
        redirect admission refuses a submission it cannot deliver urgently.
        """
        return _nonempty_str(self.raw.get("redirect_endpoint"))

    @property
    def foreign_host(self) -> bool:
        """Whether ``pid`` belongs to another host's process table.

        Nothing this host can observe says anything about such a lock's owner,
        so it is never proof of death and never safe to steal.
        """
        hostname = self.owner_hostname
        return hostname is not None and hostname != socket.gethostname()

    @property
    def repository_roots(self) -> tuple[str, ...]:
        """Repository roots this lock claims, or ``()`` for a version 1 lock.

        Empty means "declares nothing", not "claims nothing": a version 1 lock
        predates the claim, so a caller comparing registries cannot read it as
        proof that the owning run touches no repository of its own.
        """
        raw_roots = self.raw.get("repositories")
        if not isinstance(raw_roots, list):
            return ()
        return tuple(root for root in raw_roots if isinstance(root, str) and root)


@dataclass(frozen=True)
class UnlockResult:
    """Result from removing or checking a plan lock."""

    path: Path
    had_lock: bool
    removed: bool
    was_active: bool
    was_stale: bool
    message: str


def state_root(state_dir: str | Path | None = None) -> Path:
    if state_dir is None:
        return config.default_state_root()
    return plans.canonicalize_path(state_dir)


def plan_state_dir(plan_dir: str | Path, state_dir: str | Path | None = None) -> Path:
    return state_root(state_dir) / plans.path_sha256(plan_dir)


def lock_path_for_plan(
    plan_dir: str | Path, state_dir: str | Path | None = None
) -> Path:
    return plan_state_dir(plan_dir, state_dir) / LOCK_FILE_NAME


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _coerce_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except TypeError, ValueError:
        return None
    return pid if pid > 0 else None


def _dump_state_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a state payload the way the runner persists it on disk."""
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
