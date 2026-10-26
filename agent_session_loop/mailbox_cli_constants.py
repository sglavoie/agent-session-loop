"""Constants shared by the mailbox command-line modules."""

from __future__ import annotations

from . import (
    mailbox_rendering,
)

COMMAND_INBOX_GUIDANCE = "inbox-guidance"
COMMAND_INBOX_REPLY = "inbox-reply"
COMMAND_INBOX_RESOLVE = "inbox-resolve"
COMMAND_INBOX_READ = "inbox-read"
COMMAND_INBOX_ACKNOWLEDGE = "inbox-acknowledge"
COMMAND_ASK = "ask"
COMMAND_NOTIFY = "notify"
COMMAND_OUTBOX_WAIT = "outbox-wait"
COMMAND_OUTBOX_SUPERVISE = "outbox-supervise"
OUTBOX_INCLUDE_QUESTIONS = "questions"
OUTBOX_INCLUDE_NOTICES = "notices"
OUTBOX_INCLUDE_ALL = "all"
OUTBOX_INCLUDE_MODES = (
    OUTBOX_INCLUDE_QUESTIONS,
    OUTBOX_INCLUDE_NOTICES,
    OUTBOX_INCLUDE_ALL,
)
# Deliberately outside OUTBOX_INCLUDE_MODES: that tuple is the `wait` option
# surface, and `wait` must not gain a fourth mode.
OUTBOX_INCLUDE_SUPERVISION = "supervision"
SUPERVISE_TOKEN_IDLE = "IDLE"
SUPERVISE_TOKEN_ACTION = "ACTION"
SUPERVISE_TOKEN_DONE = "DONE"
SUPERVISE_TOKEN_LOST = "LOST"
SUPERVISE_LOST_HELPER_EXITED = "helper-exited-without-record"
SUPERVISE_LOST_HELPER_NEVER_STARTED = "helper-never-started"
# What a LOST report's run evidence says about the runner state file.
RUNNER_STATE_FRESH = "fresh"
RUNNER_STATE_ABSENT = "absent"
RUNNER_STATE_UNREADABLE = "unreadable"
RUNNER_STATE_FINISHED = "finished"
RUNNER_STATE_STALE = "stale"
# Only the launch race: a helper stamps its liveness record about a second in,
# so this is the window in which a missing record is not yet evidence of
# anything. A probe-confirmed death never waits it out.
SUPERVISE_LIVENESS_GRACE_SECONDS = 120.0
DEFAULT_ASK_WAIT_TIMEOUT_SECONDS = 90.0
# A guidance delivery wait is bounded much tighter than an answer wait: it ends
# on the run's own next delivery decision, not on a human's, and reporting
# ``queued`` promptly is more useful to a supervisor than blocking for one.
DEFAULT_GUIDANCE_DELIVERED_TIMEOUT_SECONDS = 30.0
DEFAULT_OUTBOX_WAIT_TIMEOUT_SECONDS = 300.0
DEFAULT_OUTBOX_SUPERVISE_TIMEOUT_SECONDS = 550.0
# A question an earlier call already reported, still open when this one
# starts, is reported again after at most this long instead of IDLE: the
# cursor has moved past it, and a supervisor whose reply never landed would
# otherwise see IDLE until the question expired. Not immediately, so a
# supervisor reissuing while a human considers the question does not spin.
SUPERVISE_CARRIED_QUESTION_DELAY_SECONDS = 30.0
MAX_WAIT_TIMEOUT_SECONDS = 30.0 * 60.0
LEASE_RENEW_INTERVAL_SECONDS = 30.0
ASK_WAIT_POLL_INTERVAL_SECONDS = 2.0
OUTBOX_WAIT_POLL_INTERVAL_SECONDS = 5.0
ERROR_STDIN_READ = "stdin-read-error"
ERROR_INVALID_TEXT_SOURCE = "invalid-text-source"
ERROR_INVALID_NOTE_SOURCE = "invalid-note-source"
ERROR_NO_ACTIVE_RUN = "no-active-run"
MAILBOX_MESSAGE_BEGIN = mailbox_rendering.MAILBOX_MESSAGE_BEGIN
MAILBOX_MESSAGE_END = mailbox_rendering.MAILBOX_MESSAGE_END
MAILBOX_TOKEN_UNSEEN = object()
