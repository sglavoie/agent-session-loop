"""Launching ``asl run`` chunks and ``asl questions`` for the autopilot."""

from __future__ import annotations

import contextlib
import json
import subprocess
import time

from asl_autopilot_command_parse import _child_env
from asl_autopilot_constants import (
    DEFAULT_HANG_ESCALATION_PROBES,
    DEFAULT_HANG_IDLE_CEILING,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_STALL_IDLE_THRESHOLD,
)
from asl_autopilot_events_tailer import _build_events_tailer
from asl_autopilot_hang import _hang_payload, _HangEscalation
from asl_autopilot_heartbeat import _format_heartbeat, _heartbeat_detail
from asl_autopilot_mailbox_watcher import _MailboxWatcher, _poll_mailbox_watcher
from asl_autopilot_status import _emit_heartbeat, _status_snapshot


def _invoke_run(
    parsed: dict,
    *,
    chunk_index: int,
    reply_text: str | None = None,
    clock=time.monotonic,
    heartbeat=_emit_heartbeat,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    popen=subprocess.Popen,
    status_probe=None,
    events_tailer=None,
    mailbox_watcher: _MailboxWatcher | None = None,
    stall_threshold: float = DEFAULT_STALL_IDLE_THRESHOLD,
    hang_escalation_probes: int = DEFAULT_HANG_ESCALATION_PROBES,
    hang_idle_ceiling: float = DEFAULT_HANG_IDLE_CEILING,
) -> dict:
    """Start one ``asl run`` chunk, wait with heartbeats, return its JSON.

    The child's ``argv`` already carries ``--json`` (guaranteed by
    :func:`parse_command`), so its stdout is the final run payload. We emit a
    concise heartbeat when the run starts and every ``heartbeat_interval``
    seconds it stays alive, keeping stdout clear for the parsed result. When a
    mailbox watcher is supplied, the same ``communicate`` loop also wakes at
    its five-second poll/lease deadlines, emits each newly observed question as
    one bounded ``QUESTION`` progress record, and retains full question bodies
    in the returned ``outbox`` object. A child that crashes or prints non-JSON
    yields a synthetic ``run-error`` payload.

    Heartbeat ticks derive their live snapshot from the plan's ``events.jsonl``
    rather than re-probing ``asl status`` every 30s: when ``status_probe`` is
    supplied it is called once at start to learn the ``events_path``, and an
    :class:`_EventsTailer` (overridable via ``events_tailer``) folds the appended
    ``turn_heartbeat``/``verification_heartbeat`` records into each tick's
    session label, ``(n/total)``, and idle seconds. A ``WARN possibly stalled``
    line is raised once the event stream itself goes silent past
    ``stall_threshold`` (the authoritative signal that the runner stopped
    emitting its 30s heartbeats), independent of the ``idle_seconds`` frozen at
    the last heartbeat, so the 30s ``state.json`` refresh lag can no longer raise
    a false positive and a below-threshold idle sample can no longer suppress a
    real stall. If no events
    path is available, the tailer raises, or the snapshot is paused/unknown, the
    tick falls back to the injectable ``status_probe`` (the historical idle-only
    behavior) and finally to the plain ``running, elapsed Ns`` line. The initial
    started/resumed heartbeat is never probed.

    Above the soft WARN sit the hard hang bars (see :class:`_HangEscalation`):
    ``hang_escalation_probes`` consecutive ticks carrying a result-received
    ``hang_suspected`` block, or a tick at/above ``hang_idle_ceiling`` idle
    seconds whose CPU sample does not report the tree progressing, stop the
    wait loop and return a ``{"_worker_hung": True, ...}``
    marker (mirroring the ``_invoke_error`` marker) **without killing or
    waiting on the child** — the ``asl run`` process stays alive with the lock
    held, so re-running the same supervision command re-attaches.

    When ``reply_text`` is supplied this launches the resume path: ``asl run``
    is invoked with ``--reply-stdin`` appended and the reply is streamed on the
    child's stdin. The reply never appears in ``argv`` so it cannot leak into
    process listings or persisted argv — the runner reads it from stdin.
    """
    argv = list(parsed["argv"])
    is_reply = reply_text is not None
    if is_reply:
        argv.append("--reply-stdin")
    try:
        proc = popen(
            argv,
            env=_child_env(parsed),
            cwd=parsed.get("cwd"),
            stdin=subprocess.PIPE if is_reply else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "paused": False,
            "reason": "run-error",
            "error": f"could not start `asl run`: {exc}",
            "_invoke_error": True,
        }

    observed_status_probe = status_probe
    if status_probe is not None and mailbox_watcher is not None:

        def observed_status_probe():
            status = status_probe()
            mailbox_watcher.observe_status(status, now=clock())
            return status

    def refresh_unbound_mailbox_run() -> None:
        """Best-effort rebind after the child has written lock-owned state."""
        if (
            mailbox_watcher is None
            or mailbox_watcher.run_id is not None
            or observed_status_probe is None
        ):
            return
        with contextlib.suppress(Exception):
            observed_status_probe()

    # Prefer the event-driven tailer (opened from the first `asl status`
    # payload's events_path); keep `status_probe` as the per-tick fallback seam.
    tailer = events_tailer
    if tailer is None:
        tailer = _build_events_tailer(observed_status_probe, clock, stall_threshold)

    hang_escalation = _HangEscalation(
        probes=hang_escalation_probes, idle_ceiling=hang_idle_ceiling
    )

    def worker_hung_marker(escalation: dict) -> dict:
        # Escalate without killing or waiting on the child: probe status once
        # for fresh plan/state/events/artifact paths, then hand control back.
        escalation_status: object = None
        if observed_status_probe is not None:
            try:
                escalation_status = observed_status_probe()
            except Exception:  # noqa: BLE001
                escalation_status = None
        marker = {
            "ok": False,
            "paused": False,
            "reason": "worker_hung",
            "_worker_hung": True,
            "hang": _hang_payload(
                escalation,
                parsed,
                escalation_status,
                supervised_child_pid=getattr(proc, "pid", None),
                detached=True,
            ),
        }
        if isinstance(escalation_status, dict):
            marker["status"] = escalation_status
        if mailbox_watcher is not None:
            marker["outbox"] = mailbox_watcher.outbox_payload()
        return marker

    start = clock()
    heartbeat_deadline = start + heartbeat_interval
    heartbeat(
        f"chunk {chunk_index}: "
        + ("resumed `asl run --reply-stdin`" if is_reply else "started `asl run`")
    )
    stdout_parts: list = []
    stderr_parts: list = []
    # `input` is delivered on the first communicate(); after a heartbeat
    # timeout it has already been written, so subsequent calls pass None to
    # avoid re-sending the reply.
    pending_input: str | None = reply_text
    while True:
        now = clock()
        deadlines = [heartbeat_deadline]
        mailbox_deadline: float | None = None
        if mailbox_watcher is not None:
            mailbox_deadline = mailbox_watcher.next_deadline()
            if mailbox_deadline is not None:
                deadlines.append(mailbox_deadline)
        timeout = max(0.0, min(deadlines) - now)
        try:
            out, err = proc.communicate(input=pending_input, timeout=timeout)
            if out:
                stdout_parts.append(out)
            if err:
                stderr_parts.append(err)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
            now = clock()
            if mailbox_watcher is not None:
                if mailbox_deadline is not None and now >= mailbox_deadline:
                    refresh_unbound_mailbox_run()
                _poll_mailbox_watcher(
                    mailbox_watcher,
                    now=now,
                    heartbeat=heartbeat,
                )
            if now < heartbeat_deadline:
                continue

            elapsed = now - start
            message: str | None = None
            tick_snapshot: dict = {}
            if tailer is not None:
                try:
                    snapshot, events_stale = tailer.snapshot()
                except Exception:  # noqa: BLE001
                    # Disable the tailer for the rest of the chunk and fall back
                    # to the subprocess probe seam on this and later ticks.
                    tailer = None
                else:
                    tick_snapshot = snapshot
                    # Only trust the tailer when it yields a usable
                    # (running/verifying) snapshot. An unknown/paused snapshot
                    # means the event stream told us nothing this tick — a
                    # missing or not-yet-readable events file makes
                    # `follow_events` yield empty batches without raising, so the
                    # snapshot stays "unknown". Falling through to the
                    # authoritative status probe (rather than emitting a plain
                    # line that would suppress it) keeps enriched progress and
                    # stall detection from being disabled indefinitely, and the
                    # tailer stays live so it resumes once the file appears.
                    _, detail = _heartbeat_detail(
                        snapshot, stall_threshold, events_stale
                    )
                    if detail is not None:
                        message = _format_heartbeat(
                            chunk_index,
                            elapsed,
                            snapshot,
                            stall_threshold,
                            events_stale=events_stale,
                        )
            if message is None and observed_status_probe is not None:
                try:
                    snapshot = _status_snapshot(observed_status_probe())
                except Exception:  # noqa: BLE001
                    snapshot = {"state": "unknown"}
                tick_snapshot = snapshot
                message = _format_heartbeat(
                    chunk_index, elapsed, snapshot, stall_threshold
                )
            if message is None:
                message = f"chunk {chunk_index}: running, elapsed {int(elapsed)}s"
            heartbeat(message)
            escalation = hang_escalation.observe(tick_snapshot)
            if escalation is not None:
                heartbeat(
                    f"chunk {chunk_index}: escalating worker_hung — "
                    f"{escalation['class']}; leaving the `asl run` child alive"
                )
                return worker_hung_marker(escalation)
            steps = max(
                1,
                int((now - heartbeat_deadline) // heartbeat_interval) + 1,
            )
            heartbeat_deadline += steps * heartbeat_interval

    stdout = "".join(stdout_parts).strip()
    stderr = "".join(stderr_parts).strip()
    payload: object = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    if mailbox_watcher is not None:
        refresh_unbound_mailbox_run()
        if isinstance(payload, dict) and mailbox_watcher.run_id is None:
            # A fast child can finish before the first status-backed watcher
            # bind. Its direct result is authoritative for this subprocess, so
            # use that identity for the final notice drain without accepting a
            # potentially stale unlocked status identity.
            mailbox_watcher.set_run_id(payload.get("run_id"), now=clock())
        _poll_mailbox_watcher(
            mailbox_watcher,
            now=clock(),
            heartbeat=heartbeat,
            force_read=True,
        )
    if not isinstance(payload, dict):
        result = {
            "ok": False,
            "paused": False,
            "reason": "run-error",
            "error": (
                f"`asl run` did not emit JSON (exit {proc.returncode}): {stderr[:400]}"
            ),
            "_invoke_error": True,
            "returncode": proc.returncode,
        }
        if mailbox_watcher is not None:
            result["outbox"] = mailbox_watcher.outbox_payload()
        return result
    payload.setdefault("returncode", proc.returncode)
    if mailbox_watcher is not None:
        payload["outbox"] = mailbox_watcher.outbox_payload()
    return payload


def _questions_argv(parsed: dict) -> list:
    """Build the ``asl questions <plan> [--state-dir DIR] --json`` argv."""
    argv = [parsed["executable"], "questions", parsed["plan_dir"]]
    if parsed.get("state_dir") is not None:
        argv += ["--state-dir", parsed["state_dir"]]
    argv.append("--json")
    return argv


def _invoke_questions(parsed: dict, *, runner=subprocess.run) -> dict:
    """Fetch the paused-run question context via ``asl questions --json``.

    ``asl questions --json`` prints a JSON payload on stdout for both the
    success case (``ok: True`` with the resolver prompt, last message, pause
    metadata, and artifact paths) and its recognized error cases (``ok: False``
    with a stable ``reason`` such as ``no-paused-run``), so we parse stdout
    regardless of exit code and only synthesize a transport error when there is
    no JSON to read.
    """
    argv = _questions_argv(parsed)
    try:
        completed = runner(
            argv,
            env=_child_env(parsed),
            cwd=parsed.get("cwd"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "reason": "questions-error",
            "error": f"could not run `asl questions`: {exc}",
        }
    text = (completed.stdout or "").strip()
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload
    detail = (completed.stderr or "").strip()[:400]
    return {
        "ok": False,
        "reason": "questions-error",
        "error": f"`asl questions` exited {completed.returncode}: {detail}",
    }
