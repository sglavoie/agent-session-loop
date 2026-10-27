"""Receipt-tail waiting and timeout reporting for control commands."""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cli_common, control_signal, events, state
from .cli_parser_support import flag_refusal
from .control_cli_vocab import (
    _EVENTS_TOKEN_UNAVAILABLE,
    _EVENTS_TOKEN_UNSEEN,
    CONTROL_WAIT_POLL_INTERVAL_SECONDS,
    DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS,
    FINALIZING_NOTE,
    MAX_CONTROL_WAIT_TIMEOUT_SECONDS,
)


@dataclass(frozen=True)
class _ReceiptWait:
    """Outcome of one bounded wait for a queued command's receipt."""

    observed: bool
    terminal_action: str | None
    satisfied: bool
    timeout_seconds: float
    elapsed_seconds: float
    run_finished: bool = False
    finalizing: dict[str, Any] | None = None
    #: The terminal event's own payload, for a wait whose outcome carries a
    #: refusal reason the caller has to report rather than merely announce.
    terminal_payload: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "observed": self.observed,
            "terminal_action": self.terminal_action,
            "timeout_seconds": self.timeout_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "run_finished": self.run_finished,
        }
        # Only an unsatisfied wait can be sitting behind a finalizing run. A
        # satisfied wait whose outcome is final on its own (a guard mutation)
        # has nothing left to explain.
        if (
            not self.run_finished
            and not self.satisfied
            and self.terminal_action is not None
        ):
            payload["finalizing"] = self.finalizing
            payload["note"] = FINALIZING_NOTE
        return payload


def _events_change_token(events_path: Path) -> object:
    """Return cheap append-log metadata used to skip unchanged full reads."""
    try:
        stat_result = events_path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        # The probe is only an optimization; an unreadable stat must still lead
        # to a full read attempt rather than a silent "nothing changed".
        return _EVENTS_TOKEN_UNAVAILABLE
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


_WAIT_RECORD_NAMES = frozenset({events.RUN_CONTROL_EVENT, events.RUN_FINISHED_EVENT})


def _read_wait_records(events_path: Path) -> list[dict[str, Any]]:
    """Read the records a receipt wait reads, tolerating a torn tail.

    Both names matter: the run-control records carry the receipt and the
    turn-level outcome, and ``run_finished`` is the only proof that the run
    those records belong to has actually released the lock.
    """
    try:
        # Bytes, decoded line by line below: a stray non-UTF-8 byte then
        # spoils only its own line, the way a torn line does, not the file.
        data = events_path.read_bytes()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:
            # A line being appended right now is read whole on the next poll.
            continue
        if isinstance(record, dict) and record.get("name") in _WAIT_RECORD_NAMES:
            records.append(record)
    return records


@dataclass(frozen=True)
class _ReceiptScan:
    """What one pass over the event log found after the command's receipt."""

    observed: bool = False
    terminal_action: str | None = None
    run_finished: bool = False
    terminal_payload: dict[str, Any] | None = None


def _scan_for_receipt(
    events_path: Path,
    *,
    command_id: str,
    terminal_actions: tuple[str, ...],
    terminal_requires_command_id: bool = False,
) -> _ReceiptScan:
    observed = False
    terminal_action: str | None = None
    terminal_payload: dict[str, Any] | None = None
    for record in _read_wait_records(events_path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("name") == events.RUN_FINISHED_EVENT:
            if observed:
                return _ReceiptScan(
                    observed=True,
                    terminal_action=terminal_action,
                    run_finished=True,
                    terminal_payload=terminal_payload,
                )
            continue
        action = payload.get("action")
        if not observed:
            if (
                action == events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
                and payload.get("command_id") == command_id
            ):
                observed = True
            continue
        # A terminal action carries no command ID, so only one recorded after
        # this command's receipt can be its outcome. The scan continues past it
        # because the turn ending is not the run ending.
        #
        # ``terminal_requires_command_id`` is the stricter rule available to
        # outcomes that do carry one: a guard mutation's result names the
        # command it answers, so a concurrent operator's guard cannot be
        # mistaken for this one's.
        if terminal_action is None and action in terminal_actions:
            if terminal_requires_command_id and payload.get("command_id") != command_id:
                continue
            terminal_action = action
            terminal_payload = dict(payload)
    return _ReceiptScan(
        observed=observed,
        terminal_action=terminal_action,
        terminal_payload=terminal_payload,
    )


def _active_finalization(
    plan_dir: Path, state_dir: str | None
) -> dict[str, Any] | None:
    """Read the finalization phase a run is publishing, if it is in one."""
    state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        # ValueError covers both a torn document and bytes that are not UTF-8.
        return None
    if not isinstance(raw, dict):
        return None
    active = raw.get("active_finalization")
    return active if isinstance(active, dict) else None


def _wait_for_receipt(
    plan_dir: Path,
    *,
    state_dir: str | None,
    command_id: str,
    timeout: float,
    terminal_actions: tuple[str, ...],
    terminal_requires_command_id: bool = False,
    terminal_is_final: bool = False,
) -> _ReceiptWait:
    """Tail ``events.jsonl`` until the command is observed, or the deadline.

    ``terminal_actions`` extends the wait past delivery: for an abort the
    supervisor wants the real outcome, so the wait continues until the runner
    records one of them after the receipt — and then past that, until the run
    itself finishes. The turn-level action only proves the turn ended; the run
    goes on to audit itself and keeps the plan lock while it does, so reporting
    the abort as done there is how a finalizing run gets mistaken for a hang.
    """
    started = time.monotonic()
    deadline = started + timeout
    events_path = state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
    last_read_token: object = _EVENTS_TOKEN_UNSEEN
    scan = _ReceiptScan()
    while True:
        current_token = _events_change_token(events_path)
        if (
            last_read_token is _EVENTS_TOKEN_UNSEEN
            or current_token is _EVENTS_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
            scan = _scan_for_receipt(
                events_path,
                command_id=command_id,
                terminal_actions=terminal_actions,
                terminal_requires_command_id=terminal_requires_command_id,
            )
            last_read_token = current_token
            if scan.observed and (
                not terminal_actions
                or (
                    scan.terminal_action is not None
                    and (terminal_is_final or scan.run_finished)
                )
            ):
                return _ReceiptWait(
                    observed=True,
                    terminal_action=scan.terminal_action,
                    satisfied=True,
                    timeout_seconds=timeout,
                    elapsed_seconds=time.monotonic() - started,
                    run_finished=scan.run_finished,
                    terminal_payload=scan.terminal_payload,
                )

        now = time.monotonic()
        if now >= deadline:
            return _ReceiptWait(
                observed=scan.observed,
                terminal_action=scan.terminal_action,
                satisfied=False,
                timeout_seconds=timeout,
                elapsed_seconds=now - started,
                run_finished=scan.run_finished,
                terminal_payload=scan.terminal_payload,
                finalizing=(
                    _active_finalization(plan_dir, state_dir)
                    if scan.terminal_action is not None
                    else None
                ),
            )
        time.sleep(min(CONTROL_WAIT_POLL_INTERVAL_SECONDS, deadline - now))


def _validated_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> float | None:
    """Resolve ``--timeout``, reporting a rejection in place.

    Returns ``None`` when the value is unusable, which the caller turns into
    exit 1 before anything is enqueued.
    """
    requested = getattr(args, "wait_timeout", None)
    if not getattr(args, "wait", False):
        if requested is not None:
            cli_common.print_error(
                args,
                command_name=command_name,
                text_label=text_label,
                reason="invalid-timeout",
                error="--timeout requires --wait",
            )
            return None
        return DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS
    try:
        timeout = (
            DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS
            if requested is None
            else float(requested)
        )
    except TypeError, ValueError:
        timeout = math.nan
    if (
        not math.isfinite(timeout)
        or timeout < 0
        or timeout > MAX_CONTROL_WAIT_TIMEOUT_SECONDS
    ):
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error=flag_refusal(
                "--timeout",
                f"must be between 0 and {int(MAX_CONTROL_WAIT_TIMEOUT_SECONDS)} "
                "seconds",
                requested,
            ),
        )
        return None
    return timeout


def _report_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    command_id: str,
    queue_path: Path,
    signal: control_signal.NotifyResult,
    run_active: bool,
    wait: _ReceiptWait,
) -> None:
    """Explain a receipt wait that ran out, distinguishing why.

    The command is durable either way, so the failure is about observation: no
    runner was listening, a listening runner did not drain in time, it drained
    but the abort it started has not resolved yet, or the abort ended the turn
    and the run is still finalizing. Only the last of those describes a run
    that is doing bounded work rather than one that may be stuck.
    """
    details = {
        "queue_path": str(queue_path),
        "command_id": command_id,
        "enqueued": True,
        "signal": signal.to_payload(),
        "wait": wait.to_payload(),
    }
    window = f"{wait.timeout_seconds:g}s"
    if not wait.observed and (
        not run_active or signal.reason == control_signal.REASON_NO_RUNNER
    ):
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="no-runner",
            error=(
                f"no runner is listening, so command {command_id} was not "
                f"observed within {window}"
            ),
            details=details,
            hint="the command stays queued for the next runner that drains it",
        )
        return
    if not wait.observed:
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="receipt-timeout",
            error=(
                f"the runner holding the plan lock did not drain command "
                f"{command_id} within {window}"
            ),
            details=details,
            hint="the command stays queued; retry with a longer --timeout",
        )
        return
    if wait.terminal_action is None:
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="abort-timeout",
            error=(
                f"the runner observed command {command_id} but recorded no abort "
                f"outcome within {window}"
            ),
            details=details,
            hint="the grace window may outlast --timeout; the abort is still armed",
        )
        return
    cli_common.print_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason="finalization-timeout",
        error=(
            f"the runner recorded {wait.terminal_action} for command "
            f"{command_id} but the run did not finish within {window}"
        ),
        details=details,
        hint=FINALIZING_NOTE,
    )
