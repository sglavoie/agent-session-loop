"""Quota-reset parsing and opt-in wait support for the autopilot."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from asl_autopilot_constants import QUOTA_WAIT_SLICE_SECONDS
from asl_autopilot_status import _emit_heartbeat

from agent_session_loop import events as _events
from agent_session_loop import retry_hints as _retry_hints

# Shapes a provider's reset hint actually takes once
# `agent_session_loop.harness_failures._parse_retry_at`
# has trimmed it to <=60 characters of free text: an ISO 8601 timestamp (only
# tried when it opens with a date, so a bare `10:30` is not mistaken for one), a
# relative `N hours` / `N minutes`, or a wall-clock time of day with an optional
# `(Area/City)` zone. A bare digit string (`Retry-After`) is handled directly.
_QUOTA_RESET_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_QUOTA_RESET_DURATION_RE = re.compile(r"^(\d+)\s*(hours?|minutes?)$", re.IGNORECASE)
_QUOTA_RESET_CLOCK_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
    r"(?:\s*\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)\))?$",
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    """Wall-clock now, timezone-aware. The quota wait's default clock seam.

    Deliberately wall clock rather than the supervision loop's ``clock``
    (``time.monotonic``): a reset hint names an absolute instant, and on macOS
    the monotonic clock does not advance while the machine is suspended, so
    only wall clock makes a suspend visible to the wait below.
    """
    return datetime.now(UTC)


def _shift_datetime(base: datetime, seconds: float) -> datetime | None:
    """``base`` plus ``seconds``, or ``None`` when that lands off the calendar.

    A reset hint is bounded to 60 characters but not in magnitude, so a
    provider printing a 60-digit ``Retry-After`` names a span no ``timedelta``
    can hold, and a merely large one names an instant no ``datetime`` can
    reach. Both raise ``OverflowError``; declining is the same fail-closed
    answer the caller already gives an unparseable hint.
    """
    try:
        return base + timedelta(seconds=seconds)
    except OverflowError:
        return None


def _parse_quota_reset_at(retry_at: object, *, now: datetime) -> datetime | None:
    """Resolve a runner ``retry_at`` hint to the absolute instant quota resets.

    ``retry_at`` is the advisory free text
    ``agent_session_loop.harness_failures._parse_retry_at`` extracted
    from harness output (bounded to 60 characters); ``now`` is a
    timezone-aware wall-clock instant. Recognizes a bare ``Retry-After`` second
    count, an ISO 8601 timestamp (a trailing ``Z`` is tolerated; a naive one is
    read as local time), a relative ``N hours`` / ``N minutes``, and a time of
    day with an optional ``(Area/City)`` zone resolved to its next occurrence.
    Anything else — including a zone name this host cannot resolve, or a value
    naming an instant outside the representable calendar — returns ``None`` so
    the caller keeps today's ``user_required`` stop instead of sleeping on a
    guess.
    """
    return _retry_hints.resolve_retry_at(retry_at, now=now)


def _parse_quota_reset_clock(value: str, *, now: datetime) -> datetime | None:
    """Resolve a ``10:30pm (America/New_York)`` style hint to the next such instant."""
    match = _QUOTA_RESET_CLOCK_RE.match(value)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()
    zone_name = match.group(4)
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    if zone_name:
        try:
            local_now = now.astimezone(ZoneInfo(zone_name))
        except KeyError, ValueError, OSError:
            return None
    else:
        local_now = now.astimezone()
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target


def _quota_wait_decision(
    *,
    enabled: bool,
    pause_reason: str,
    classification: dict | None,
    resumes: int,
    max_resumes: int,
    now: datetime,
    buffer_seconds: float,
    ceiling_seconds: float,
) -> tuple[datetime | None, dict | None]:
    """Decide whether a ``requires_user`` pause may be slept through.

    Returns ``(deadline, block)``. ``deadline`` is the absolute instant to wait
    until — the parsed reset plus ``buffer_seconds`` — or ``None`` when a guard
    declined. ``block`` is the ``quota_wait`` report; it reaches a result only
    on the declined path, where its ``skipped`` names the guard so a stop that
    *could* have waited always says why it did not. With the feature off both
    are ``None``: the default-off result stays byte-identical to the behavior
    that predates the flag.

    ``auth-failed`` and ``invalid-model`` reach this with the feature on and
    are declined by the reason guard, so they are never slept through.
    """
    if not enabled:
        return None, None

    retry_at = (
        classification.get("retry_at") if isinstance(classification, dict) else None
    )
    block: dict = {
        "resumes": resumes,
        "max_resumes": max_resumes,
        "retry_at": retry_at if isinstance(retry_at, str) else None,
        "ceiling_seconds": ceiling_seconds,
    }

    def declined(skipped: str) -> tuple[None, dict]:
        return None, {**block, "skipped": skipped}

    if pause_reason != _events.PAUSE_REASON_QUOTA_EXHAUSTED:
        return declined("reason-not-quota")
    if resumes >= max_resumes:
        return declined("max-resumes-spent")
    if block["retry_at"] is None:
        return declined("no-reset-hint")
    reset_at = _parse_quota_reset_at(block["retry_at"], now=now)
    if reset_at is None:
        return declined("unparseable-reset-hint")

    # A reset already past (or moments away) still waits the buffer: the hint
    # said quota is back, the harness said otherwise, so relaunching instantly
    # would just re-fail. Each such attempt spends a resume, which is what
    # makes a permanently stuck provider converge to `user_required`.
    #
    # The span is measured by subtracting two instants rather than by adding
    # the buffer to the reset, so a hint at the end of the calendar is declined
    # by the ceiling instead of overflowing on its way there. A deadline the
    # calendar cannot hold is a wait no workable ceiling admits, so it declines
    # by the same guard.
    wait_seconds = max((reset_at - now).total_seconds(), 0.0) + buffer_seconds
    block["wait_seconds"] = wait_seconds
    block["reset_at"] = reset_at.isoformat()
    deadline = _shift_datetime(now, wait_seconds)
    if wait_seconds > ceiling_seconds or deadline is None:
        return declined("wait-exceeds-ceiling")
    return deadline, block


def _wait_for_quota_reset(
    deadline: datetime,
    *,
    now_fn=_utc_now,
    sleep=time.sleep,
    heartbeat=_emit_heartbeat,
    slice_seconds: float = QUOTA_WAIT_SLICE_SECONDS,
) -> float:
    """Sleep until ``deadline``, in short slices, and return the seconds slept.

    Every slice recomputes the remaining time from the wall clock instead of
    accumulating slept durations, which makes the wait suspend-safe by
    construction: a machine that sleeps through the reset wakes to a slice that
    observes a past deadline and returns at once, rather than serving out the
    remainder of a span that already elapsed.
    """
    slept = 0.0
    while True:
        remaining = (deadline - now_fn()).total_seconds()
        if remaining <= 0:
            return slept
        heartbeat(f"quota reset wait: {int(remaining)}s remaining")
        duration = min(slice_seconds, remaining)
        sleep(duration)
        slept += duration
