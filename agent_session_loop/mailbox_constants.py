"""Size limits, schema versions, and rejection/error codes for the mailbox."""

from __future__ import annotations

MAILBOX_FILE_NAME = "mailbox.jsonl"


MAILBOX_LEASE_FILE_NAME = "mailbox-lease.json"


MAILBOX_SUPERVISION_CURSOR_FILE_TEMPLATE = "mailbox-supervision-{supervision_id}.json"


MAILBOX_SUPERVISION_CURSOR_GLOB = "mailbox-supervision-*.json"


MAILBOX_SUPERVISION_CURSOR_LOCK_FILE_NAME = "mailbox-supervision-cursors.lock"


MAILBOX_SUPERVISION_CURSOR_VERSION = 1


MAILBOX_SUPERVISION_CURSOR_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


MAILBOX_LEGACY_SCHEMA_VERSION = 1


MAILBOX_PREVIOUS_SCHEMA_VERSION = 2


# The version that introduced the wait/receipt lifecycle. Wait strictness is a
# statement about a question's own vintage, not about the current write
# version, so it is pinned here instead of tracking ``MAILBOX_SCHEMA_VERSION``:
# a v4 writer must keep treating a v3 wait-mode question exactly as v3 did.
MAILBOX_WAIT_SCHEMA_VERSION = 3


# The version that introduced ``guidance-delivery`` records.
MAILBOX_SCHEMA_VERSION = 4


MAILBOX_SUPPORTED_SCHEMA_VERSIONS = (
    MAILBOX_LEGACY_SCHEMA_VERSION,
    MAILBOX_PREVIOUS_SCHEMA_VERSION,
    MAILBOX_WAIT_SCHEMA_VERSION,
    MAILBOX_SCHEMA_VERSION,
)


MAILBOX_LEASE_VERSION = 2


MAILBOX_LEASE_SUPPORTED_VERSIONS = (1, MAILBOX_LEASE_VERSION)


MAILBOX_MAX_BYTES = 4 * 1024 * 1024


# The smallest compact record produced with normal generated metadata is an
# acknowledgement with a one-digit ``upto`` value, a canonical UUID, and a
# generated UTC timestamp with six fractional-second digits. Once less than this
# remains, no ordinary mailbox append can succeed.
MAILBOX_MIN_APPEND_BYTES = 128


# The smallest compact reply produced with normal generated metadata uses a
# one-character body plus canonical UUIDs for both its id and target.
MAILBOX_MIN_REPLY_BYTES = 176


# The smallest compact resolution uses ``pause``, a one-character run id, and
# canonical UUIDs for both its id and target.
MAILBOX_MIN_RESOLUTION_BYTES = 200


# Headroom below the cap that only question-closing records may consume.
# Guidance, questions, notices, and acknowledgements stop at the reduced cap
# so they cannot strand a waiting run by consuming its closure capacity.
MAILBOX_CLOSURE_RESERVE_BYTES = 512 * 1024


# Lifecycle records must remain appendable after ordinary messages and even a
# maximal closure have exhausted their respective tiers.  The three nested
# limits are intentionally conservative: a closure cannot consume the bytes
# reserved for ending its wait and recording consumption, and a wait-end cannot
# consume the final receipt reserve.
MAILBOX_WAIT_END_RESERVE_BYTES = 192 * 1024


MAILBOX_RECEIPT_RESERVE_BYTES = 96 * 1024


# Headroom below the non-closure cap that only ``guidance-delivery`` records may
# consume. Guidance that is already durable must always be able to record what
# happened to it, so ordinary appends — including further guidance — stop one
# tier earlier and leave this band for the ledger they create work for.
MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES = 128 * 1024


# The smallest compact guidance-delivery record produced with normal generated
# metadata: canonical UUIDs for both its id and its guidance target, a generated
# UTC timestamp, and the shortest channel/state/reason triple (``live`` /
# ``deferred`` / ``stale-turn``). A real attempt that also carries a turn
# identity and a harness name is far larger, which is why the reserve above is
# sized for many records rather than for one.
MAILBOX_MIN_GUIDANCE_DELIVERY_BYTES = 253


# Backward-compatible names for callers that compute the old reply/question
# reserve. Both aliases intentionally name the shared closure reserve.
MAILBOX_REPLY_RESERVE_BYTES = MAILBOX_CLOSURE_RESERVE_BYTES


MAILBOX_QUESTION_RESERVE_BYTES = MAILBOX_CLOSURE_RESERVE_BYTES


# Questions record the run that owns them. A runner id is
# ``<timestamp>-<pid>`` (~30 characters); the bound only keeps a hostile or
# corrupt value from consuming the mailbox capacity budget.
MAILBOX_MAX_RUN_ID_CHARS = 128


MAILBOX_MAX_SESSION_ID_CHARS = 128


# Harness names are short identifiers chosen by this repository ("claude",
# "codex", ...). The bound only keeps a hostile or corrupt value from consuming
# the mailbox capacity budget.
MAILBOX_MAX_HARNESS_CHARS = 64


# Mailbox sequence and turn fields are operational counters, not arbitrary
# precision values. A signed 64-bit ceiling keeps every accepted value safely
# serializable even when Python's integer-string conversion guard is enabled.
MAILBOX_MAX_INTEGER = (1 << 63) - 1


MAILBOX_LEASE_BYTES = 512


MAILBOX_LEASE_FRESH_SECONDS = 90.0


MAILBOX_CHANGE_TOKEN_UNAVAILABLE = object()


# Stable record-rejection reasons. These are intentionally data, not prose:
# later CLI and status surfaces can branch on them without parsing exceptions.
REJECT_EMPTY_LINE = "empty-line"


REJECT_INVALID_JSON = "invalid-json"


REJECT_NOT_OBJECT = "not-object"


REJECT_UNSUPPORTED_VERSION = "unsupported-version"


REJECT_UNKNOWN_KIND = "unknown-kind"


REJECT_INVALID_ID = "invalid-id"


REJECT_DUPLICATE_ID = "duplicate-id"


REJECT_INVALID_CREATED_AT = "invalid-created-at"


REJECT_MISSING_TEXT = "missing-text"


REJECT_INVALID_TARGET = "invalid-target"


REJECT_QUESTION_NOT_FOUND = "question-not-found"


REJECT_QUESTION_NOT_OPEN = "question-not-open"


REJECT_QUESTION_ALREADY_ANSWERED = "question-already-answered"


REJECT_QUESTION_ALREADY_RESOLVED = "question-already-resolved"


REJECT_INVALID_RUN_ID = "invalid-run-id"


REJECT_INVALID_SESSION_ID = "invalid-session-id"


REJECT_INVALID_TURN_INDEX = "invalid-turn-index"


REJECT_INVALID_WAITER_ORIGIN = "invalid-waiter-origin"


REJECT_INVALID_ACTION = "invalid-action"


REJECT_INVALID_NOTE = "invalid-note"


REJECT_INVALID_ACKNOWLEDGEMENT = "invalid-acknowledgement"


REJECT_INVALID_DELIVERY_MODE = "invalid-delivery-mode"


REJECT_INVALID_SUPERVISION_OUTCOME = "invalid-supervision-outcome"


REJECT_INVALID_SUPERVISION_ID = "invalid-supervision-id"


REJECT_QUESTION_NOT_WAITING = "question-not-waiting"


REJECT_QUESTION_ALREADY_WAITING = "question-already-waiting"


REJECT_INVALID_WAIT_ID = "invalid-wait-id"


REJECT_INVALID_WAIT_END_REASON = "invalid-wait-end-reason"


REJECT_INVALID_CLOSURE_ID = "invalid-closure-id"


REJECT_CLOSURE_NOT_FOUND = "closure-not-found"


REJECT_CLOSURE_ALREADY_CONSUMED = "closure-already-consumed"


REJECT_GUIDANCE_NOT_FOUND = "guidance-not-found"


REJECT_GUIDANCE_ALREADY_DELIVERED = "guidance-already-delivered"


REJECT_INVALID_DELIVERY_STATE = "invalid-delivery-state"


REJECT_INVALID_DELIVERY_REASON = "invalid-delivery-reason"


REJECT_INVALID_DELIVERY_CHANNEL = "invalid-delivery-channel"


REJECT_INVALID_HARNESS = "invalid-harness"


REJECT_INCOMPLETE_TURN_IDENTITY = "incomplete-turn-identity"


# A delivery record must never carry the guidance body it reports on. The
# rejection is not defensive tidiness: it is the durable enforcement point for
# "no guidance text outside the guidance record".
REJECT_UNEXPECTED_TEXT = "unexpected-text"


# Stable operation errors. Validation failures use the corresponding rejection
# reason where possible so callers receive the same reason for live input and
# persisted corruption.
ERROR_MAILBOX_FULL = "mailbox-full"


ERROR_MAILBOX_READ = "mailbox-read-error"


ERROR_MAILBOX_WRITE = "mailbox-write-error"


ERROR_LEASE_READ = "lease-read-error"


ERROR_LEASE_WRITE = "lease-write-error"


LEASE_MISSING = "missing"


LEASE_INVALID_SIZE = "invalid-size"


LEASE_EMPTY = "empty"


LEASE_INVALID_JSON = "invalid-json"


LEASE_NOT_OBJECT = "not-object"


LEASE_UNSUPPORTED_VERSION = "unsupported-version"


LEASE_INVALID_SUPERVISOR_ID = "invalid-supervisor-id"


LEASE_INVALID_UPDATED_AT = "invalid-updated-at"


LEASE_INVALID_RUN_ID = "invalid-run-id"


LEASE_INVALID_PROJECTION_SEQ = "invalid-projection-seq"


LEASE_INVALID_UNANSWERED_QUESTIONS = "invalid-unanswered-questions"


LEASE_INVALID_LATEST_QUESTION_ID = "invalid-latest-question-id"


LEASE_INVALID_LATEST_QUESTION_CREATED_AT = "invalid-latest-question-created-at"


LEASE_INCOMPLETE_QUESTION_PROJECTION = "incomplete-question-projection"


LEASE_FUTURE_UPDATED_AT = "future-updated-at"


LEASE_EXPIRED = "expired"
