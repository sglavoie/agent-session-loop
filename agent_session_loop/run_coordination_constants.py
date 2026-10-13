"""Budgets, environment names, and vocabulary shared by run coordination."""

from __future__ import annotations

from . import (
    mailbox_constants,
    qna,
)

# A runner that receives a reply-capable pause remains live for five minutes.
# An active supervisor lease may extend that deadline one minute at a time.
# The ceiling below bounds *unsupervised* time, not wall clock: it is measured
# from the last lease that proved a supervisor alive, so a watched wait outlives
# it while an unwatched one still ends within thirty minutes. Environment
# overrides are deliberately narrow test seams; invalid, non-finite, or
# non-positive values fall back to these production defaults.
MAILBOX_REPLY_WAIT_BASE_SECONDS = 5 * 60.0
MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS = 60.0
MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS = 30 * 60.0
MAILBOX_REPLY_WAIT_POLL_SECONDS = 2.0
MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS = 10.0
# The wait paces itself on the monotonic clock, which freezes across system
# sleep on macOS, while the persisted deadlines are wall clock. Between two
# poll iterations the wall clock can only run ahead of the monotonic clock by
# scheduler jitter, so drift beyond one whole poll interval plus margin must
# be a suspend (or a clock step): the wait then re-anchors the persisted
# deadlines around its surviving monotonic budget. Suspend detection is
# exercised through the injected clock seams, so unlike the budgets above
# this threshold deliberately has no environment override.
MAILBOX_REPLY_WAIT_SUSPEND_SKEW_SECONDS = 5.0
# For a few seconds after wake the on-disk lease is legitimately wall-clock
# ancient: the autopilot has not polled yet, and a lease probe landing in that
# window would kill a healthy wait. After a detected suspend the supervisor
# therefore gets this much *awake* (monotonic) time to re-prove liveness
# before a non-fresh lease ends the wait. It equals one full lease freshness
# window (mailbox_constants.MAILBOX_LEASE_FRESH_SECONDS, 90 seconds) — roughly eighteen
# chances at the autopilot's 5-second poll cadence, the tick that actually
# republishes the projection. Inside the grace a stale lease neither ends the
# wait nor extends the active deadline, and the grace never extends the
# unsupervised ceiling. Like the skew threshold above, this is a detection
# bound rather than an operator budget, exercised through the injected clock
# seams, so it deliberately has no environment override.
MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS = (
    mailbox_constants.MAILBOX_LEASE_FRESH_SECONDS
)
# A question body is the worker's last-message artifact, which is unbounded,
# while the mailbox log is capped at 4 MiB with no rotation — one oversized
# message could consume it. Longer bodies are truncated deterministically to
# this many characters plus a footer naming the on-disk artifact that holds
# the full text. Determinism is load-bearing: a retried wait preparation must
# recompute a byte-identical body or the question-reuse equality check in
# ensure_mailbox_question would degrade the retry to a terminal pause. Like
# the suspend bounds above this is a protocol bound rather than an operator
# budget, so it deliberately has no environment override.
MAILBOX_QUESTION_TEXT_MAX_CHARS = 16000
MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST = "supervisor-lost"
MAILBOX_REPLY_WAIT_PREPARATION_REASONS: tuple[str, ...] = (
    mailbox_constants.LEASE_MISSING,
    mailbox_constants.LEASE_INVALID_SIZE,
    mailbox_constants.LEASE_EMPTY,
    mailbox_constants.LEASE_INVALID_JSON,
    mailbox_constants.LEASE_NOT_OBJECT,
    mailbox_constants.LEASE_UNSUPPORTED_VERSION,
    mailbox_constants.LEASE_INVALID_SUPERVISOR_ID,
    mailbox_constants.LEASE_INVALID_UPDATED_AT,
    mailbox_constants.LEASE_FUTURE_UPDATED_AT,
    mailbox_constants.LEASE_EXPIRED,
    mailbox_constants.ERROR_LEASE_READ,
    mailbox_constants.ERROR_MAILBOX_FULL,
    mailbox_constants.ERROR_MAILBOX_READ,
    mailbox_constants.ERROR_MAILBOX_WRITE,
    *qna.QNA_ERROR_REASONS,
)
MAILBOX_REPLY_WAIT_BASE_ENV_VAR = "ASL_MAILBOX_REPLY_WAIT_BASE_SECONDS"
MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR = (
    "ASL_MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS"
)
MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR = (
    "ASL_MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS"
)
MAILBOX_REPLY_WAIT_POLL_ENV_VAR = "ASL_MAILBOX_REPLY_WAIT_POLL_SECONDS"
MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR = "ASL_MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS"

#: Sentinel for "this wait has never parsed the mailbox", which is distinct
#: from "the change token could not be read". The first forces a parse; the
#: second forces one too, but only because it proves nothing.
MAILBOX_TOKEN_UNSEEN = object()

#: Terminal wait reasons that are *not* an error the caller must degrade on.
WAIT_REASON_REPLY = "reply"
WAIT_REASON_RESOLUTION = "resolution"
WAIT_REASON_TIMEOUT = "timeout"
#: The reason recorded when a wait's own bookkeeping raised before it could
#: report one. It is deliberately not a mailbox reason: nothing was observed.
WAIT_REASON_ERROR = "error"
