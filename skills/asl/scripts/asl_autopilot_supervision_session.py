"""Per-run state and loop steps behind ``supervise_run``."""

from __future__ import annotations

from asl_autopilot_attach_step import _attach_step
from asl_autopilot_constants import (
    STOP_KIND_AFTER_CURRENT,
)
from asl_autopilot_hang import _HangEscalation
from asl_autopilot_invoke import _invoke_run
from asl_autopilot_launch_policy import (
    _argv_with_max_sessions,
    _parsed_with_status_workspace,
    _pending_count,
    _should_launch_for_status,
    _status_workspace,
    _workspace_launch_error,
)
from asl_autopilot_lock_state import (
    _attached_stop_honored,
    _classify_lock,
    _lost_launch_stop_honored,
    _runner_state,
    _stop_kind,
)
from asl_autopilot_mailbox_watcher import _MailboxWatcher
from asl_autopilot_pause_step import _handle_pause_step
from asl_autopilot_quota import _utc_now
from asl_autopilot_records import _run_result

from agent_session_loop import review_transitions as _review_transitions


class _SupervisionSession:
    """Mutable state and per-iteration steps of one ``supervise_run`` call.

    :meth:`step` runs one iteration of the supervision loop: it returns the
    final result payload when supervision is over, or ``None`` to iterate again.
    """

    def __init__(
        self,
        parsed: dict,
        *,
        status_invoker,
        run_invoker,
        reply_text: str | None,
        clock,
        sleep,
        heartbeat,
        events_stat,
        poll_interval: float,
        heartbeat_interval: float,
        stall_threshold: float,
        max_chunks: int,
        attach_max_seconds: float,
        max_retries: int,
        mailbox_watcher_factory,
        hang_escalation_probes: int,
        hang_idle_ceiling: float,
        wait_quota_reset: bool,
        wall_clock,
        quota_reset_buffer_seconds: float,
        quota_wait_ceiling_seconds: float,
        max_quota_resumes: int,
        quota_wait_slice_seconds: float,
    ) -> None:
        self.parsed = parsed
        self.status_invoker = status_invoker
        self.clock = clock
        self.sleep = sleep
        self.heartbeat = heartbeat
        self.events_stat = events_stat
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.stall_threshold = stall_threshold
        self.max_chunks = max_chunks
        self.attach_max_seconds = attach_max_seconds
        self.max_retries = max_retries
        self.mailbox_watcher_factory = mailbox_watcher_factory
        self.wait_quota_reset = wait_quota_reset
        self.wall_clock = wall_clock
        self.quota_reset_buffer_seconds = quota_reset_buffer_seconds
        self.quota_wait_ceiling_seconds = quota_wait_ceiling_seconds
        self.max_quota_resumes = max_quota_resumes
        self.quota_wait_slice_seconds = quota_wait_slice_seconds

        self.mailbox_watcher: _MailboxWatcher | None = None
        self.chunks = 0
        self.attach_start: float | None = None
        self.last_attach_beat: float | None = None
        self.retry_counts: dict = {}
        self.retry_delays: dict = {}
        # `--max-sessions` is a global budget: `budget` is the requested cap (or
        # `None`), `completed_total` accumulates implementation sessions
        # completed across every chunk (measured by the drop in pending between
        # launches, an exact classification-free mirror of the runner's own
        # impl-session count), and `launched_pending` records the pending count
        # at the last launch so the next iteration can attribute the delta. Once
        # `completed_total` reaches the budget with impl sessions still pending,
        # the run stops cleanly.
        self.budget: int | None = parsed.get("max_sessions")
        self.completed_total = 0
        self.launched_pending: int | None = None
        # Set when the run that last held this plan stopped cleanly with
        # `reason: "stopped"`, i.e. an out-of-band `asl control stop-after-current`
        # or `asl control abort` was honored — either a chunk we launched (from
        # its exit payload) or a run we attached to (from the state it
        # persisted). Recorded here so it survives into the next iteration,
        # where the guard stops instead of relaunching; `stop_kind` carries which
        # of the two requests it was into the result.
        self.stop_requested = False
        self.stop_kind: str | None = None
        # Identity and pending baseline of the run currently being attached to (a
        # foreign `asl run` driving this plan). `attached_pending` doubles as the
        # "we are attached" flag: it is the pending count at the first attach
        # poll, so the sessions that run finishes while we watch can be
        # attributed once it releases the lock, while `attached_run_id` (when the
        # holder got far enough to persist it) and `attached_lock_created_at` let
        # us read that specific run's terminal reason out of the persisted
        # `runner_state`.
        self.attached_run_id: str | None = None
        self.attached_lock_created_at: str | None = None
        self.attached_pending: int | None = None
        # Quota resets slept through so far under `wait_quota_reset`. Bounds a
        # provider that keeps rejecting immediately after each reset: every such
        # attempt spends one resume, so the loop converges to `user_required`.
        self.quota_resumes = 0
        # Hard hang bars for the attach path: each full status probe is one
        # observation, mirroring the per-tick observations a self-launched chunk
        # feeds from its events tailer.
        self.attach_hang_escalation = _HangEscalation(
            probes=hang_escalation_probes, idle_ceiling=hang_idle_ceiling
        )

        if run_invoker is None:

            def run_invoker(target: dict, chunk_index: int) -> dict:
                # Inject the operator reply only on the first self-started chunk;
                # subsequent relaunches drive remaining sessions normally.
                chunk_reply = reply_text if chunk_index == 1 else None
                return _invoke_run(
                    target,
                    chunk_index=chunk_index,
                    reply_text=chunk_reply,
                    clock=clock,
                    heartbeat=heartbeat,
                    heartbeat_interval=heartbeat_interval,
                    status_probe=lambda: status_invoker(parsed),
                    mailbox_watcher=self.mailbox_watcher,
                    stall_threshold=stall_threshold,
                    hang_escalation_probes=hang_escalation_probes,
                    hang_idle_ceiling=hang_idle_ceiling,
                )

        self.run_invoker = run_invoker

    def run_result(self, *args, **kwargs) -> dict:
        if self.mailbox_watcher is not None:
            kwargs.setdefault("outbox", self.mailbox_watcher.outbox_payload())
            self.mailbox_watcher.stop()
        return _run_result(*args, **kwargs)

    def step(self) -> dict | None:
        status = self.status_invoker(self.parsed)
        rejection, disposition = self._reject_status(status)
        if rejection is not None:
            return rejection

        pending = _pending_count(status)
        if disposition == "attach":
            return _attach_step(self, status, pending)

        self._settle_finished_runs(status, pending)
        final = self._stop_or_complete(status, pending)
        if final is not None:
            return final
        # disposition in ("clear", "stale") -> start one chunk ourselves.
        return self._launch_chunk(status, pending)

    def _reject_status(self, status: object) -> tuple[dict | None, str]:
        parsed = self.parsed
        if not isinstance(status, dict) or not status.get("ok"):
            error = (
                status.get("error")
                if isinstance(status, dict)
                else "status probe returned no payload"
            )
            return (
                self.run_result(
                    False,
                    "status-error",
                    status=status if isinstance(status, dict) else None,
                    chunks=self.chunks,
                    error=error,
                ),
                "",
            )

        workspace_error = _workspace_launch_error(parsed, status)
        if workspace_error is not None:
            return (
                self.run_result(
                    False,
                    "user_required",
                    status=status,
                    chunks=self.chunks,
                    error=workspace_error,
                    workspace=_status_workspace(status),
                ),
                "",
            )
        disposition = _classify_lock(parsed, status)
        if disposition == "mismatch":
            return (
                self.run_result(
                    False,
                    "user_required",
                    status=status,
                    chunks=self.chunks,
                    error=(
                        "an active `asl run` lock holds this plan/state for a "
                        "different run; resolve it before supervising"
                    ),
                ),
                disposition,
            )
        return None, disposition

    def _settle_finished_runs(self, status: dict, pending: int) -> None:
        # Attribute sessions completed by the previous chunk to the global
        # budget. Pending strictly decreases through the implementation phase
        # (review sessions are only appended once pending hits 0, which the
        # budget stops us reaching), so the drop is an exact count of completed
        # impl sessions; clamp at 0 defensively.
        if self.launched_pending is not None:
            self.completed_total += max(0, self.launched_pending - pending)
            self.launched_pending = None

        # A run we were attached to has released its lock. Attribute the sessions
        # it finished while we watched (same pending-drop measure the chunk
        # accounting uses, so `completed` stays accurate for work the supervisor
        # did not launch itself), then derive that run's terminal reason from the
        # state it persisted: `asl status` already exposes the finished run's
        # `run_id`/`run_started_at`/`ok`/`reason`/`run_finished_at`, and a clean
        # `stopped` from *that same run* means the runner honored an out-of-band
        # `asl control` request. Feeding it into `stop_requested` routes both the
        # self-launched and attached cases through the single guard in
        # `_stop_or_complete`, so we never launch a chunk that would silently
        # undo the operator's request.
        if self.attached_pending is not None:
            self.completed_total += max(0, self.attached_pending - pending)
            if _attached_stop_honored(
                status, self.attached_run_id, self.attached_lock_created_at
            ):
                self.stop_requested = True
                self.stop_kind = _stop_kind(_runner_state(status))
            self.attached_pending = None
            self.attached_run_id = None
            self.attached_lock_created_at = None
            self.attach_start = None

    def _stop_or_complete(self, status: dict, pending: int) -> dict | None:
        # A spent budget with impl sessions still pending is a clean, intentional
        # early stop — never a failure or a pause. When the budget is spent
        # exactly as pending reaches 0, `pending > 0` is false so we fall through
        # to the normal complete/review path instead of pre-empting it.
        if (
            self.budget is not None
            and self.completed_total >= self.budget
            and pending > 0
        ):
            return self.run_result(
                True,
                "max-sessions",
                status=status,
                chunks=self.chunks,
                completed=self.completed_total,
                pending=pending,
            )

        # A stop the runner honored (`asl control stop-after-current` or
        # `asl control abort`, sent out of band) is the same kind of clean,
        # intentional early stop as a spent budget: the run — ours or the one we
        # attached to — exited `ok`, so relaunching here would silently undo the
        # operator's request. An abort is no more an anomaly than a boundary
        # stop; it just reverted the session it killed, which `stop_kind` tells
        # the caller so the counts are not misread. The stop covers everything
        # the loop would do next, review continuations included, so the guard is
        # exactly "would we otherwise launch": a stop landing on a genuinely
        # finished plan still falls through to the normal complete path.
        should_launch = _should_launch_for_status(self.parsed, status, pending)
        if self.stop_requested and should_launch:
            return self.run_result(
                True,
                "stopped",
                status=status,
                chunks=self.chunks,
                completed=self.completed_total,
                pending=pending,
                stop_kind=self.stop_kind or STOP_KIND_AFTER_CURRENT,
            )

        if not should_launch:
            return self.run_result(True, "complete", status=status, chunks=self.chunks)
        return None

    def _launch_chunk(self, status: dict, pending: int) -> dict | None:
        parsed = self.parsed
        if self.chunks >= self.max_chunks:
            return self.run_result(
                False,
                "capped",
                status=status,
                chunks=self.chunks,
                error=(
                    f"reached max supervision chunks ({self.max_chunks}) with "
                    f"{pending} pending session(s) or a review continuation remaining"
                ),
            )
        self.chunks += 1
        # Record pending at launch so the next iteration can attribute the drop
        # to the global session budget.
        self.launched_pending = pending
        # Cap this chunk at the sessions still remaining in the budget. The
        # budget-exhausted guard guarantees `budget - completed_total >= 1`
        # here, so a relaunch after a pause that already completed some sessions
        # (the only case that recurs now that the runner drains all pending in
        # one chunk) cannot overshoot the total.
        bound_parsed = _parsed_with_status_workspace(parsed, status)
        if self.budget is not None:
            remaining = self.budget - self.completed_total
            launch_parsed = {
                **bound_parsed,
                "argv": _argv_with_max_sessions(bound_parsed["argv"], remaining),
            }
        else:
            launch_parsed = bound_parsed
        if not parsed.get("no_mailbox"):
            launch_now = self.clock()
            if self.mailbox_watcher is None:
                candidate = self.mailbox_watcher_factory(status, clock=self.clock)
                if candidate is not None and candidate.establish(now=launch_now):
                    self.mailbox_watcher = candidate
                    self.mailbox_watcher.prepare_for_launch(
                        now=launch_now,
                        renew_lease=False,
                    )
            else:
                self.mailbox_watcher.prepare_for_launch(now=launch_now)
        launched_at = _utc_now().isoformat()
        run_payload = self.run_invoker(launch_parsed, self.chunks)
        return self._interpret_chunk(status, run_payload, launched_at)

    def _interpret_chunk(
        self, status: dict, run_payload: object, launched_at: str
    ) -> dict | None:
        chunks = self.chunks
        if not isinstance(run_payload, dict) or run_payload.get("_invoke_error"):
            # A lost launch says nothing about why the run ended, so ask the
            # plan before calling it an error: a runner killed while it was
            # finalizing an operator's stop had already recorded that stop, and
            # reporting it as `run-error` would blame the operator's own
            # request. Only an unexplained death keeps the error.
            final_status = self.status_invoker(self.parsed)
            if isinstance(final_status, dict) and final_status.get("ok"):
                if _lost_launch_stop_honored(final_status, launched_at):
                    return self.run_result(
                        True,
                        "stopped",
                        status=final_status,
                        chunks=chunks,
                        stop_kind=_stop_kind(_runner_state(final_status)),
                    )
                status = final_status
            return self.run_result(
                False,
                "run-error",
                status=status,
                chunks=chunks,
                run=run_payload if isinstance(run_payload, dict) else None,
                error=(
                    run_payload.get("error")
                    if isinstance(run_payload, dict)
                    else "run invocation returned no payload"
                ),
            )
        if run_payload.get("_worker_hung"):
            # The chunk escalated a hard hang; the `asl run` child is still
            # alive and holds the plan lock, so surface it untouched for the
            # operator (re-running the same command re-attaches).
            marker_status = run_payload.get("status")
            hang = run_payload.get("hang")
            return self.run_result(
                False,
                "worker_hung",
                status=marker_status if isinstance(marker_status, dict) else status,
                chunks=chunks,
                hang=hang if isinstance(hang, dict) else None,
            )
        if run_payload.get("paused"):
            return _handle_pause_step(self, status, run_payload)
        # Unresolved review findings are a completed run with an unclean
        # verdict, not a blocked one. The loop re-polls and the caller's
        # `_attach_review_context` re-tags the result `review_findings`, which
        # carries the severity counts a bare `blocked` would throw away.
        if run_payload.get("ok") is False and (
            run_payload.get("reason")
            != _review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON
        ):
            return self.run_result(
                False,
                "blocked",
                status=status,
                chunks=chunks,
                run=run_payload,
                error=(f"run stopped without completing: {run_payload.get('reason')}"),
            )
        # Clean stop; loop re-polls. Pending 0 -> complete/review continuation;
        # a spent `--max-sessions` budget -> clean `max-sessions` stop; a runner
        # that honored a `stop-after-current` or `abort` request -> clean
        # `stopped` stop; otherwise relaunch (the runner drives all pending
        # sessions in one chunk, so this normally only recurs for review
        # continuations).
        self.stop_requested = run_payload.get("reason") == "stopped"
        self.stop_kind = _stop_kind(run_payload) if self.stop_requested else None
        return None
