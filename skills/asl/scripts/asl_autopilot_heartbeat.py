"""Heartbeat message formatting and stall detection for supervised runs."""

from __future__ import annotations

from datetime import UTC, datetime

from agent_session_loop import state_payloads


def _heartbeat_detail(
    snapshot: dict,
    stall_threshold: float,
    events_stale=None,
    *,
    now: datetime | None = None,
):
    """Distill a snapshot into a ``(is_stall, detail)`` pair for a heartbeat.

    ``detail`` is the enrichment fragment shared by every heartbeat site so the
    in-chunk and attach-wait lines render identically:

    - ``running`` with no stall: ``(False, "session <id> (n/total) | idle
      <n>s")`` — the ``(n/total)`` fragment is omitted when either number is
      absent, and the ``idle`` fragment when ``idle_seconds`` is unavailable.
    - ``running`` stalled: ``(True, "idle <n>s (no output)")`` when a high
      ``idle_seconds`` drove the verdict, else ``(True, "no new events for
      <n>s+ (no output)")`` when event silence did — either way callers render
      their own ``WARN possibly stalled`` line.
    - ``verifying`` (review phase): ``(False, "verifying")`` while the stream is
      fresh; ``(True, "no new events for <n>s+ (no output)")`` when the events
      tailer reports the verification stream went silent past the threshold (the
      heartbeat carries no ``idle_seconds``, so event silence is the only signal).
    - ``finalizing`` (terminal audit): ``(False, "finalizing (<phase>) | <n>s
      elapsed")`` under the same event-silence rule as ``verifying``.
    - ``waiting_for_reply``: healthy when the authoritative
      ``waiting_for_reply_status`` is ``live`` and its valid unsupervised
      ceiling remains in the future. That ceiling rolls forward with the last
      lease activity the record carries, so a watched wait has no terminal
      instant; records predating the rolling ceiling fall back to their static
      ``hard_deadline``. An explicitly non-live status, elapsed ceiling, or
      malformed deadline is reported as a stall. When the status is absent for
      compatibility with older ``asl`` builds, the deadline-only behavior is
      preserved. Event silence alone is ignored inside that bound.
    - any other snapshot (``paused``/``unknown``/errored): ``(False, None)`` so
      callers fall back to their plain line, preserving pre-enrichment behavior.

    ``events_stale`` selects the stall signal. ``None`` (the subprocess-probe
    fallback and the attach-wait heartbeat, whose ``idle_seconds`` was just read
    from a fresh ``asl status``) keeps the historical idle-only trigger: a stall
    iff ``idle_seconds`` crosses ``stall_threshold``. The events tailer passes an
    explicit bool, and there event silence is the authoritative signal: because
    the runner emits a ``turn_heartbeat`` every 30s while alive, a stream silent
    past the threshold means the runner itself stopped — so ``events_stale``
    alone decides the stall, independent of the ``idle_seconds`` frozen at the
    last heartbeat. This is what lets a below-threshold idle sample still stall
    once the stream goes silent (a frozen low idle can no longer suppress it),
    while a still-fresh stream keeps suppressing the warning even when that
    sampled idle is high (the 30s ``state.json`` refresh lag can never raise a
    false positive on its own).
    """
    pending_outbox = snapshot.get("pending_outbox")
    outbox_fragment = (
        f"pending outbox {pending_outbox}"
        if (
            isinstance(pending_outbox, int)
            and not isinstance(pending_outbox, bool)
            and pending_outbox >= 0
        )
        else None
    )

    def with_outbox(detail: str) -> str:
        if outbox_fragment is None:
            return detail
        return f"{detail} | {outbox_fragment}"

    state = snapshot.get("state")
    if state == "running":
        idle = snapshot.get("idle_seconds")
        idle_high = isinstance(idle, (int, float)) and idle >= stall_threshold
        stalled = idle_high if events_stale is None else bool(events_stale)
        if stalled:
            if idle_high:
                return True, with_outbox(f"idle {int(idle)}s (no output)")
            return True, with_outbox(
                f"no new events for {int(stall_threshold)}s+ (no output)"
            )
        label = f"session {snapshot.get('session_id')}"
        number = snapshot.get("session_number")
        total = snapshot.get("total_sessions")
        if number is not None and total is not None:
            label += f" ({number}/{total})"
        idle_fragment = (
            f" | idle {int(idle)}s" if isinstance(idle, (int, float)) else ""
        )
        return False, with_outbox(f"{label}{idle_fragment}")
    if state == "verifying":
        # The verification heartbeat carries no ``idle_seconds``, so event
        # silence is the only stall signal available for this phase. As with
        # ``running``, ``events_stale is None`` (the subprocess-probe/attach-wait
        # path, which has no event-freshness reading) keeps the historical
        # never-stall behavior; an explicit ``True`` from the events tailer means
        # the verification stream itself went silent past the threshold, so warn.
        if events_stale:
            return True, with_outbox(
                f"no new events for {int(stall_threshold)}s+ (no output)"
            )
        return False, with_outbox("verifying")
    if state == "finalizing":
        # Same signal as ``verifying``: the phase publishes its own heartbeat
        # every 30s and carries no ``idle_seconds``, so only a silent stream
        # can say the runner stopped rather than that the audit is slow.
        if events_stale:
            return True, with_outbox(
                f"no new events for {int(stall_threshold)}s+ (no output)"
            )
        phase = snapshot.get("phase")
        detail = "finalizing" if not phase else f"finalizing ({phase})"
        elapsed = snapshot.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            detail += f" | {int(elapsed)}s elapsed"
        return False, with_outbox(detail)
    if state == "waiting_for_reply":
        session_id = snapshot.get("session_id")
        detail = "waiting for reply"
        if session_id is not None:
            detail += f" for session {session_id}"
        waiting_status = snapshot.get("waiting_for_reply_status")
        if waiting_status is not None and waiting_status != "live":
            return True, with_outbox(
                f"{detail}: wait status {waiting_status} (no output)"
            )
        # The ceiling rolls forward with the last lease that proved a
        # supervisor alive, so it is projected from the record rather than
        # read as a static instant. Waits persisted before the ceiling rolled
        # carry only ``hard_deadline`` and keep their original wording.
        ceiling_label = (
            "hard deadline"
            if snapshot.get("unsupervised_deadline_at") is None
            else "unsupervised ceiling"
        )
        ceiling_deadline = state_payloads.waiting_for_reply_ceiling(snapshot)
        if ceiling_deadline is None:
            return True, with_outbox(f"{detail}: invalid {ceiling_label} (no output)")
        current = datetime.now(UTC) if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=UTC)
        else:
            current = current.astimezone(UTC)
        if current >= ceiling_deadline:
            return True, with_outbox(f"{detail}: {ceiling_label} elapsed (no output)")
        # Within the runner's absolute bound, ordinary event silence does not
        # imply a stall because the reply poll intentionally emits no heartbeat.
        return False, with_outbox(detail)
    return False, None


def _format_heartbeat(
    chunk_index: int,
    elapsed: float,
    snapshot: dict,
    stall_threshold: float,
    events_stale=None,
    *,
    now: datetime | None = None,
) -> str:
    """Render a per-tick heartbeat line from a :func:`_status_snapshot`.

    - ``running`` with ``idle_seconds`` below ``stall_threshold``: an enriched
      line carrying the session label, ``(n/total)`` (omitted when either number
      is absent), and ``idle <n>s``.
    - ``running`` with ``idle_seconds`` at or above ``stall_threshold``: a
      ``WARN possibly stalled`` line naming the idle and elapsed seconds.
    - ``verifying`` (review phase): the same running line with a ``verifying``
      label instead of a session.
    - ``waiting_for_reply``: a healthy line while its wait status is live and
      before its unsupervised ceiling, or under the legacy deadline-only
      fallback when the status is absent; a non-live status, elapsed ceiling,
      or malformed deadline produces a stall warning.
    - any other snapshot (``paused``/``unknown``/errored): the legacy plain
      ``running, elapsed Ns`` line, preserving pre-enrichment behavior.
    """
    plain = f"chunk {chunk_index}: running, elapsed {int(elapsed)}s"
    is_stall, detail = _heartbeat_detail(
        snapshot,
        stall_threshold,
        events_stale,
        now=now,
    )
    if detail is None:
        return plain
    if is_stall:
        return (
            f"chunk {chunk_index}: WARN possibly stalled — "
            f"{detail}, elapsed {int(elapsed)}s"
        )
    return f"chunk {chunk_index}: running {int(elapsed)}s | {detail}"


def _snapshot_idle_seconds(snapshot: dict, block: dict | None):
    """Best-available idle reading: the snapshot's, else the hang block's."""
    idle = snapshot.get("idle_seconds")
    if not isinstance(idle, (int, float)) or isinstance(idle, bool):
        idle = block.get("idle_seconds") if block is not None else None
    if isinstance(idle, (int, float)) and not isinstance(idle, bool):
        return float(idle)
    return None


def _snapshot_cpu_progressing(snapshot: dict) -> bool | None:
    """Whether the runner's last CPU sample saw the process tree progressing.

    ``None`` means unknown — no fresh sample, or a malformed block. Only a real
    bool answers the question, so a sampling gap can never read as progress.
    """
    block = snapshot.get("hang_cpu_progress")
    if not isinstance(block, dict):
        return None
    progressing = block.get("progressing")
    return progressing if isinstance(progressing, bool) else None
