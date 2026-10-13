"""The ``supervise_run`` supervision loop."""

from __future__ import annotations

import time

from asl_autopilot_constants import (
    DEFAULT_ATTACH_MAX_SECONDS,
    DEFAULT_HANG_ESCALATION_PROBES,
    DEFAULT_HANG_IDLE_CEILING,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_MAX_CHUNKS,
    DEFAULT_MAX_QUOTA_RESUMES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUOTA_RESET_BUFFER_SECONDS,
    DEFAULT_QUOTA_WAIT_CEILING_SECONDS,
    DEFAULT_STALL_IDLE_THRESHOLD,
    QUOTA_WAIT_SLICE_SECONDS,
)
from asl_autopilot_mailbox_watcher import _MailboxWatcher
from asl_autopilot_quota import _utc_now
from asl_autopilot_status import (
    _emit_heartbeat,
    _events_size,
    _invoke_status,
)
from asl_autopilot_supervision_session import _SupervisionSession


def supervise_run(
    parsed: dict,
    *,
    status_invoker=_invoke_status,
    run_invoker=None,
    reply_text: str | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
    heartbeat=_emit_heartbeat,
    events_stat=_events_size,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    stall_threshold: float = DEFAULT_STALL_IDLE_THRESHOLD,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    attach_max_seconds: float = DEFAULT_ATTACH_MAX_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    mailbox_watcher_factory=_MailboxWatcher.from_status,
    hang_escalation_probes: int = DEFAULT_HANG_ESCALATION_PROBES,
    hang_idle_ceiling: float = DEFAULT_HANG_IDLE_CEILING,
    wait_quota_reset: bool = False,
    wall_clock=_utc_now,
    quota_reset_buffer_seconds: float = DEFAULT_QUOTA_RESET_BUFFER_SECONDS,
    quota_wait_ceiling_seconds: float = DEFAULT_QUOTA_WAIT_CEILING_SECONDS,
    max_quota_resumes: int = DEFAULT_MAX_QUOTA_RESUMES,
    quota_wait_slice_seconds: float = QUOTA_WAIT_SLICE_SECONDS,
) -> dict:
    """Drive an ``asl run`` to completion within a global session budget.

    Polls ``asl status --json`` for authoritative state, then on each iteration:

    - a status probe failure stops with ``status-error``;
    - an active lock for a different run stops with ``user_required``;
    - an active matching lock means another run is driving the plan, so the
      supervisor sleeps ``poll_interval`` and re-polls (attach), even when the
      transient status snapshot reports zero pending sessions. The attached
      run's ``run_id`` and the pending count at attach are recorded so that,
      once its lock clears, the sessions it finished are attributed to
      ``completed`` and its persisted terminal reason is read back. Mailbox
      supervision uses the same status-derived durable cursor as a self-launched
      child, renewing its lease and delivering each newly observed question once
      until supervision returns;
    - ``--max-sessions`` is a global budget: the drop in pending across chunks
      accumulates the implementation sessions completed this run, and once that
      count reaches the cap with impl sessions still pending the run stops
      cleanly with ``max-sessions`` (``ok: true``, reporting ``completed`` and
      ``pending``). A budget spent exactly as pending reaches 0 falls through to
      the normal complete/review path rather than pre-empting it;
    - a runner that honored an out-of-band ``asl control stop-after-current``
      or ``asl control abort`` request stops the supervision the same way with
      ``stopped`` (``ok: true``, reporting ``completed``, ``pending``, and a
      ``stop_kind`` of ``stop-after-current`` or ``abort``) instead of
      relaunching the remaining sessions and undoing the request. Both routes
      to the plan are covered: a chunk this supervisor launched exiting cleanly
      with ``reason: "stopped"``, and an *attached* run releasing its lock
      having persisted ``ok``/``reason: "stopped"``/``run_finished_at``. That
      state must be attributable to the attached run — by the ``run_id``
      observed while attached, or, when the run finished before it ever
      persisted one, by having started at or after the attached lock's
      ``created_at`` — which keeps a stale ``stopped`` state from an earlier run
      from blocking a new one (see :func:`_attached_stop_honored`);
    - without a lock, pending sessions launch normally, while an all-complete
      plan launches only a ``review_resume`` initial-review requested by the
      command's review harness, a materialize-repairs action allowed by the
      effective absolute cap, or an eligible cap-free rereview; only repair
      materialization is cap-gated, and terminal/ineligible continuations stop
      with ``complete``;
    - otherwise it starts one ``asl run`` chunk. A recoverable same-session
      pause (``verify-failed``, ``transport-failed``, ``timeout``,
      ``no-status-change``) is relaunched up to ``max_retries`` times per
      (session, reason) before it is surfaced. Only ``transport-failed`` backs
      off, sleeping the :data:`TRANSPORT_RETRY_DELAYS_SECONDS` schedule (5s then
      30s) before each relaunch; the others relaunch immediately. A
      non-retryable harness failure that needs a human and has no useful Q&A
      context (``quota-exhausted``/``auth-failed``/``invalid-model``) stops
      immediately with ``user_required`` — carrying any reset (``retry_at``)
      information — and is never retried. Under ``wait_quota_reset`` a
      ``quota-exhausted`` pause whose ``retry_at`` resolves to a reset within
      ``quota_wait_ceiling_seconds`` is the one exception: the supervisor sleeps
      to that reset plus ``quota_reset_buffer_seconds`` and relaunches through
      the normal chunk machinery, up to ``max_quota_resumes`` times. Any
      declined guard falls through to the same ``user_required`` stop, carrying
      a ``quota_wait`` block that names the guard. Any other pause stops with
      ``paused`` for agent/user reply handling. A non-paused failure stops with
      ``blocked``. A clean stop re-polls: the runner drives every pending session
      in one chunk, so relaunching normally only recurs for review continuations
      (or recoverable pause retries), never to chunk through pending sessions.

    The per-(session, reason) retry counts and accumulated backoff delay persist
    across attach polls and self-launched relaunches, and the surfaced result's
    ``retries`` records the attempted delay (``delayed_seconds``), the runner's
    failure ``classification``, and whether the recoverable budget was
    ``exhausted``.

    A ``user_required`` stop on one of the two recovery pauses
    (:data:`RECOVERY_DECISION_PAUSE_REASONS`) additionally carries a
    ``recovery`` block naming the decision being handed over, the command that
    reads its evidence, the one-use relaunch that authorizes it (or ``None``
    when no authorization exists), and an explicit ``auto_resume: false``. See
    :func:`_recovery_payload`: the block is derived, never acted on — this
    helper does not discard a saved conversation and does not start a worker
    beside a predecessor it cannot show stopped.

    When ``reply_text`` is supplied the first self-started chunk resumes the
    paused run via ``asl run --reply-stdin`` (reply streamed on stdin); later
    chunks run normally so the supervisor can carry the plan the rest of the way.

    ``max_chunks`` bounds self-launched relaunches; attach polls are bounded
    instead by ``attach_max_seconds`` of wall clock per attach episode (reset
    once the attached lock clears), since a poll costs 5–30s depending on how
    chatty the run is and a count would bound nothing consistent. A genuinely
    wedged attached run is caught by the hang bars below, not by this deadline.

    Both heartbeat sites carry live progress and pending outbox counts.
    Self-launched chunks derive each tick from the plan's ``events.jsonl`` (see
    :func:`_invoke_run`), and the attach-wait heartbeat reuses the ``status``
    payload already fetched this iteration to surface the same session/idle
    detail and a ``WARN possibly stalled`` line once ``idle_seconds`` crosses
    ``stall_threshold``. Above that soft warning sit two hard hang bars
    (``hang_escalation_probes`` consecutive observations of the runner's
    ``result_received_process_alive`` suspicion, or any observation with idle at
    or beyond ``hang_idle_ceiling``): a self-launched chunk's ``_worker_hung``
    marker and the attach path's own :class:`_HangEscalation` tracker both map
    to ``run_result(False, "worker_hung", hang=...)`` — the ``asl run`` child is
    never killed or waited on, so re-running the same command re-attaches. A
    bounded ``waiting_for_reply`` state suppresses event-silence warnings only
    until its unsupervised ceiling; an overdue or malformed deadline is reported
    as possibly stalled. Between attach heartbeats the loop only ``os.stat``s
    ``events.jsonl`` (via the ``events_stat`` seam) and services watcher
    deadlines rather than re-running ``asl status`` every poll; a fresh
    full probe — which keeps lock/pending classification authoritative — runs
    once the heartbeat interval elapses or the events file grows.
    """
    session = _SupervisionSession(
        parsed,
        status_invoker=status_invoker,
        run_invoker=run_invoker,
        reply_text=reply_text,
        clock=clock,
        sleep=sleep,
        heartbeat=heartbeat,
        events_stat=events_stat,
        poll_interval=poll_interval,
        heartbeat_interval=heartbeat_interval,
        stall_threshold=stall_threshold,
        max_chunks=max_chunks,
        attach_max_seconds=attach_max_seconds,
        max_retries=max_retries,
        mailbox_watcher_factory=mailbox_watcher_factory,
        hang_escalation_probes=hang_escalation_probes,
        hang_idle_ceiling=hang_idle_ceiling,
        wait_quota_reset=wait_quota_reset,
        wall_clock=wall_clock,
        quota_reset_buffer_seconds=quota_reset_buffer_seconds,
        quota_wait_ceiling_seconds=quota_wait_ceiling_seconds,
        max_quota_resumes=max_quota_resumes,
        quota_wait_slice_seconds=quota_wait_slice_seconds,
    )
    while True:
        result = session.step()
        if result is not None:
            return result
