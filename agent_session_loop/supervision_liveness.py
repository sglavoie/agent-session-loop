"""Durable proof that a supervising helper process exists, or no longer does.

A supervising agent polls the mailbox, and the mailbox is equally silent for a
healthy quiet helper and for a helper that was never started (or died at
launch). This record closes that gap: the helper stamps its own
``(pid, ps birth time)`` identity next to the plan's state before it probes or
launches anything, and :func:`probe` re-checks that identity later. A start
time is fixed for the life of a process, so the pair is an identity a reused
pid cannot forge.

This lives outside :mod:`agent_session_loop.mailbox` on purpose: it is process
identity rather than mailbox content, and it has to keep working for a run
launched with ``--no-mailbox``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import artifacts, process_sampling, process_tree, state

SUPERVISION_LIVENESS_FILE_TEMPLATE = "supervision-liveness-{supervision_id}.json"
SUPERVISION_LIVENESS_GLOB = "supervision-liveness-*.json"
SUPERVISION_LIVENESS_VERSION = 1
# Well beyond the 8-day quota sleep a helper can legitimately spend without
# touching its record: pruning must never delete evidence for a process that is
# still alive and merely waiting.
SUPERVISION_LIVENESS_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

PROBE_TIMEOUT_SECONDS = process_tree.SAMPLE_TIMEOUT_SECONDS

ProbeVerdict = Literal["alive", "dead", "unverified"]

ALIVE: ProbeVerdict = "alive"
DEAD: ProbeVerdict = "dead"
UNVERIFIED: ProbeVerdict = "unverified"


@dataclass(frozen=True)
class LivenessRecord:
    """One helper launch's process identity and the launch that produced it."""

    supervision_id: str
    pid: int
    birth: str | None
    host: str
    argv: tuple[str, ...]
    command: dict[str, Any] | None
    plan_dir: str
    started_at: str
    # Older records predate the UTC-pinned ps environment. Their birth may be
    # expressed in a local time zone, so only they need offset tolerance.
    birth_utc: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "birth": self.birth,
            "birth_utc": self.birth_utc,
            "command": self.command,
            "host": self.host,
            "pid": self.pid,
            "plan_dir": self.plan_dir,
            "started_at": self.started_at,
            "supervision_id": self.supervision_id,
            "version": SUPERVISION_LIVENESS_VERSION,
        }


def record_path_for_plan(
    plan_dir: str | Path,
    supervision_id: str,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the liveness record path one supervising launch owns.

    One file per launch identity makes cross-helper clobbering structurally
    impossible, so the record needs no lock of its own. The UUID requirement
    also keeps the launch identity from reaching outside the state directory.
    """
    if not _is_uuid(supervision_id):
        raise ValueError("supervision_id must be a UUID")
    return state.plan_state_dir(plan_dir, state_dir) / (
        SUPERVISION_LIVENESS_FILE_TEMPLATE.format(supervision_id=supervision_id)
    )


def write_record(
    plan_dir: str | Path,
    supervision_id: str,
    *,
    command: Mapping[str, Any] | None = None,
    argv: Iterable[str] | None = None,
    pid: int | None = None,
    host: str | None = None,
    state_dir: str | Path | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Path:
    """Persist this process's identity for ``supervision_id`` and return its path.

    A ``birth`` that ``ps`` would not report is stored as ``None`` rather than
    failing the write: an unauthenticatable record still proves the helper got
    far enough to write one. ``OSError`` and
    :class:`~agent_session_loop.artifacts.ArtifactError` propagate so the caller
    owns the best-effort policy.
    """
    resolved_pid = os.getpid() if pid is None else pid
    record = LivenessRecord(
        supervision_id=supervision_id,
        pid=resolved_pid,
        birth=process_tree.process_birth(
            resolved_pid,
            run_command=run_command,
            timeout=timeout,
        ),
        host=socket.gethostname() if host is None else host,
        argv=tuple(sys.argv if argv is None else argv),
        command=dict(command) if command is not None else None,
        plan_dir=str(plan_dir),
        started_at=_utc_now_iso(),
        birth_utc=True,
    )
    record_path = record_path_for_plan(plan_dir, supervision_id, state_dir)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts.atomic_write_bytes(
        record_path,
        json.dumps(record.to_payload(), separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        + b"\n",
    )
    return record_path


def read_record(
    plan_dir: str | Path,
    supervision_id: str,
    *,
    state_dir: str | Path | None = None,
) -> LivenessRecord | None:
    """Return one launch's stored identity, or ``None`` when it is unusable.

    Every failure mode — missing, unreadable, unparseable, wrong version,
    mismatched launch, or a malformed field — yields ``None``, because a record
    that cannot be trusted must not be probed as if it could.
    """
    try:
        record_path = record_path_for_plan(plan_dir, supervision_id, state_dir)
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except OSError, ValueError, RecursionError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != SUPERVISION_LIVENESS_VERSION:
        return None
    if payload.get("supervision_id") != supervision_id:
        return None
    pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    birth = payload.get("birth")
    if birth is not None and not isinstance(birth, str):
        return None
    birth_utc = payload.get("birth_utc", False)
    if not isinstance(birth_utc, bool):
        return None
    command = payload.get("command")
    if command is not None and not isinstance(command, dict):
        return None
    argv = payload.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return None
    # Checked one at a time rather than through an `all(...)` over a tuple: the
    # generator form tests exactly the same thing but leaves each name typed
    # `Any | None`, so a null host or plan_dir would still reach the record.
    host = payload.get("host")
    stored_plan_dir = payload.get("plan_dir")
    started_at = payload.get("started_at")
    if not isinstance(host, str):
        return None
    if not isinstance(stored_plan_dir, str):
        return None
    if not isinstance(started_at, str):
        return None
    return LivenessRecord(
        supervision_id=supervision_id,
        pid=pid,
        birth=birth,
        host=host,
        argv=tuple(argv),
        command=command,
        plan_dir=stored_plan_dir,
        started_at=started_at,
        birth_utc=birth_utc,
    )


def prune_records(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    max_age_seconds: float = SUPERVISION_LIVENESS_MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Best-effort removal of records left behind by long-finished launches."""
    cutoff = (_utc_now().timestamp() if now is None else now) - max_age_seconds
    removed = 0
    try:
        candidates = sorted(
            state.plan_state_dir(plan_dir, state_dir).glob(SUPERVISION_LIVENESS_GLOB)
        )
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def probe(
    record: LivenessRecord,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeVerdict:
    """Return whether ``record``'s process is still the one that wrote it.

    ``dead`` is reported only on positive evidence — ``ps`` says no process
    holds the pid, or it holds it with a different start time (pid reuse).
    Everything that merely leaves the question open (another host's record, a
    ``ps`` that failed, timed out, or reported nothing parsable, a record with
    no birth to authenticate against) is ``unverified``, so a probe failure
    never manufactures a death.

    :func:`~agent_session_loop.process_tree.process_birth` is deliberately not
    reused here: it collapses "no such process" and "``ps`` failed" into the
    same ``None``, and those two are the whole distinction this probe exists to
    make.
    """
    if state.recorded_on_foreign_host(
        record.host, pid=record.pid, process_start=record.birth
    ):
        return UNVERIFIED
    if record.pid <= 0:
        return UNVERIFIED
    try:
        completed = run_command(
            [*process_tree.PS_BIRTH_COMMAND, str(record.pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=process_sampling.ps_environment(),
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a failed probe is unverified, never dead
        return UNVERIFIED
    if completed.returncode != 0:
        # An empty selection exits 1 without output. Diagnostics, partial
        # output, other exit codes and signals mean ps itself failed; none
        # establishes that the helper's pid is vacant.
        if (
            completed.returncode == 1
            and not (completed.stdout or "").strip()
            and not (completed.stderr or "").strip()
        ):
            return DEAD
        return UNVERIFIED
    birth = _parse_birth(completed.stdout)
    if birth is None or not record.birth:
        return UNVERIFIED
    # New records use UTC for both samples. Offset tolerance for them would
    # mistake a reused pid born 15 minutes later for the original helper.
    if record.birth_utc:
        return ALIVE if record.birth == birth else DEAD
    # An older record may disagree only because its writer used a local zone.
    return ALIVE if process_sampling.birth_may_match(record.birth, birth) else DEAD


def _parse_birth(stdout: str | None) -> str | None:
    for line in (stdout or "").splitlines():
        birth = " ".join(line.split())
        if process_tree._LSTART_RE.match(birth):
            return birth
    return None


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or value == "":
        return False
    try:
        uuid.UUID(value)
    except ValueError, AttributeError:
        return False
    return True


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="microseconds")
