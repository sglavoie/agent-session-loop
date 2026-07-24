"""Single source of truth for pause classification.

Every consumer that needs to know whether a paused run can be safely
relaunched, whether it needs a human, how many same-session retries it gets,
and how long to back off before each retry reads it from here. Payloads carry a
serialized :func:`classify_pause` block so downstream tooling (the ``asl``
supervisor helper, ``asl status``, ``asl questions``) stops hardcoding the
reason sets.

The reason sets compose from the atomic pause-reason constants in
:mod:`agent_session_loop.events`, which owns the raw vocabulary
(``PAUSE_REASON_*``) and the classifier's retryable-harness-failure set. This
module owns the higher-level policy tuples that used to be duplicated in the
supervisor helper.
"""

from __future__ import annotations

from typing import Any

from . import events


# Pause reasons the supervisor treats as transient and same-session retryable.
# ``verify-failed``, ``timeout``, and ``no-status-change`` are cases where
# relaunching the same session can succeed without any human answer, and
# ``transport-failed`` (``events.PAUSE_REASON_TRANSPORT_FAILED``) is the only
# harness-failure *category* the classifier marks retryable
# (``events.RETRYABLE_HARNESS_FAILURE_REASONS``). Unclassified ``nonzero-exit``
# is fail-closed (never retried), and the quota/auth/invalid-model categories
# are deliberately absent so quota and session-limit exhaustion are never
# relaunched. Every other pause (a question/blocked state) needs a reply.
RECOVERABLE_PAUSE_REASONS: tuple[str, ...] = (
    "verify-failed",
    events.PAUSE_REASON_TRANSPORT_FAILED,
    "timeout",
    "no-status-change",
)

# Non-retryable harness-failure categories that always require human
# intervention and carry no useful question/answer context: quota /
# session-limit exhaustion, authentication failures, and invalid-model
# rejections. Consumers surface these as ``user_required`` — carrying any
# parsed reset information — and deliberately skip ``asl questions``, since no
# reply can clear them.
USER_REQUIRED_HARNESS_FAILURE_REASONS: tuple[str, ...] = (
    events.PAUSE_REASON_QUOTA_EXHAUSTED,
    events.PAUSE_REASON_AUTH_FAILED,
    events.PAUSE_REASON_INVALID_MODEL,
)

# How many times a supervisor relaunches the same session for a given
# recoverable pause reason before giving up and surfacing the pause.
DEFAULT_MAX_RETRIES = 2

# Bounded backoff schedule (seconds) applied before each relaunch of a
# transient ``transport-failed`` pause. The classifier marks only
# ``transport-failed`` retryable, so the same session is relaunched at most
# ``len(TRANSPORT_RETRY_DELAYS_SECONDS)`` times, sleeping the scheduled delay
# first (5s, then 30s). The other recoverable pauses keep their immediate,
# zero-delay retry.
TRANSPORT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (5.0, 30.0)


def classify_pause(reason: str | None) -> dict[str, Any]:
    """Classify a pause ``reason`` into a serializable policy block.

    Returns a bounded dict describing how the pause should be handled:

    - ``reason``: the pause reason, echoed back for consumers that persist the
      block next to a payload keyed by something else;
    - ``recoverable``: whether the same session may be relaunched without a
      human answer (``reason`` is in :data:`RECOVERABLE_PAUSE_REASONS`);
    - ``requires_user``: whether the pause is a non-retryable harness failure
      that a human must resolve (``reason`` is in
      :data:`USER_REQUIRED_HARNESS_FAILURE_REASONS`);
    - ``max_retries``: the same-session retry budget (``DEFAULT_MAX_RETRIES``
      when recoverable, else ``0``);
    - ``backoff_seconds``: the per-retry backoff schedule — the
      ``transport-failed`` schedule (5s, 30s) for that reason, else an empty
      list (the other recoverable pauses relaunch immediately).
    """
    recoverable = reason in RECOVERABLE_PAUSE_REASONS
    requires_user = reason in USER_REQUIRED_HARNESS_FAILURE_REASONS
    if reason == events.PAUSE_REASON_TRANSPORT_FAILED:
        backoff_seconds = list(TRANSPORT_RETRY_DELAYS_SECONDS)
    else:
        backoff_seconds = []
    return {
        "reason": reason,
        "recoverable": recoverable,
        "requires_user": requires_user,
        "max_retries": DEFAULT_MAX_RETRIES if recoverable else 0,
        "backoff_seconds": backoff_seconds,
    }
