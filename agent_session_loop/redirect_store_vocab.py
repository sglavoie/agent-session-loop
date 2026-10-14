"""Durable append-only journal for urgent redirect requests.

A redirect carries an operator's correction into a turn that is already
running. The correction is the whole point, so the record of it has to survive
everything the control queue does not: ``control-queue.jsonl`` is read with
``ftruncate`` and deduplicated in the draining process's memory, which makes it
a fine transport for "drain now" and an impossible authority for "this
correction was submitted and has not been answered yet". This journal is that
authority. Run state, events, Workbench, and bridge receipts are projections of
it.

Four properties are structural rather than incidental:

- **Nothing is destroyed.** Every state change is another line. The physical
  one-based line number is the sequence number, so an invalid line consumes a
  sequence and never prevents a later valid one from being read, and replay
  after a crash is a re-read rather than a reconstruction.
- **A request is settled once.** ``submitted`` is the only non-terminal state;
  ``accepted``, ``rejected``, and ``expired`` are terminal, and the reducer
  keeps the first terminal record it sees. A second acceptance appended by a
  racing writer is rejected as a line, not applied as a transition, which is
  what stops two turns from being redirected by one request. ``continued``,
  ``delivered``, ``released`` and ``acknowledged`` are annotations *on* an
  accepted request rather than states of it, and none of them may precede the
  acceptance it annotates.
- **Planned delivery and actual delivery are different facts.** ``continued``
  says an attempt bound this correction into the prompt it was assembling;
  ``delivered`` says that attempt's worker was actually launched with it. The
  window between them is real — a prompt still has to be built, a conversation
  still has to be selected, and either can refuse — so a correction that never
  crossed it is given back with ``released`` rather than left claiming a
  delivery to a worker that never ran. A released correction is carryable again;
  a delivered one is not, because only the worker can say what it read and
  :mod:`redirect_ack` exists for that answer.
- **The absence of a delivery only means something inside a window that was
  declared.** ``launching`` is appended at the last instant before the prompt
  can leave the process, and it is what makes "carried, not delivered" two
  distinguishable facts instead of one ambiguous one. Before it, nothing can
  have gone out, so a carry with no launch record is safe to hand back. After
  it, a missing ``delivered`` line means either that the prompt never went out
  *or* that it did and the writer died before saying so — and since the journal
  cannot tell those apart, neither may anything reading it. Such a carry is
  reported as uncertain and is never released automatically. An attempt that
  knows it did not submit says so itself, with ``released``; that is a
  statement by a live process, not an inference from a silence.
- **The feedback text never leaves this module by accident.** No parsed object
  carries it: :class:`RedirectRequest` holds a byte count and a digest, and
  rejected lines are reported without their raw bytes because a malformed line
  can still contain the operator's words. Reading the text at all requires
  calling :func:`read_feedback` and saying which operation you want.
- **A submission cannot consume the capacity that settles it.** The journal is
  bounded, and submissions stop one tier below the cap so a full journal can
  still record the acceptance, rejection, or expiry of what is already in it.

Both of those splits moved the meaning of a line already on disk, which is why
the record version has moved twice. Version 1 wrote ``continued`` only once a
worker was running, so the same line means "a worker holds these words" on a
version-1 journal and "nobody ever ran" on a later one. Version 2 had the split
but no ``launching``, so a version-2 carry with no delivery cannot say whether
its writer died before or after the prompt went out. Replay therefore reads
each line at the version that line carries: a version-1 ``continued`` records
the delivery its writer meant by it, and a version-2 ``continued`` records that
its writer had no way to declare the launch window. Both keep a correction an
upgraded plan's previous run may already have handed over out of the automatic
release and recovery paths and in front of the operator as uncertain.

A ``continued`` line ordinarily names a later turn of the request's own run.
The one exception is marked on the line itself: ``recovered`` says a run that
died was holding this correction and the run resuming the same session took
delivery of it, which is the only continuation whose run id differs from the
target's and whose turn index may be lower. Replay holds an unmarked line to the
ordinary ordering, so the latitude is reachable only from a line that asked for
it, and the matching ``acknowledged`` line has to restate the same marker.

Concurrency is coordinated through an exclusive ``fcntl.flock`` held across
read, decide, and append, so the reducer's view is the one the append is
validated against. Completed appends are flushed with
:func:`agent_session_loop.durability.fullsync` before the lock is released: a
submission a producer was told is durable must be on the device, and a short
write is rolled back to the pre-append size rather than left as a torn tail.
"""

from __future__ import annotations

from typing import Literal

REDIRECT_JOURNAL_FILE_NAME = "redirect-journal.jsonl"
#: Version 1 had one delivery annotation. ``continued`` was written once the
#: worker holding the correction was running, so its presence *was* the proof a
#: worker had been launched. Version 2 splits that into the undertaking and the
#: outcome, which moves the meaning of a line already on disk: a version-2
#: ``continued`` with no ``delivered`` after it says nothing ever ran, while the
#: identical version-1 line says something did. The number has to move with the
#: meaning, because replay has no other way to tell the two apart, and reading
#: an old line under the new rule would hand a correction a worker already acted
#: on back to the next attempt as if it were unseen.
#: Version 3 adds ``launching``, the declaration that the prompt is about to
#: leave this process. Its absence after a version-3 ``continued`` proves the
#: prompt never went out; its absence after a version-2 one proves nothing at
#: all, because that writer had no such line to append. The number moves for the
#: same reason it moved before: the meaning of a silence changed, and replay has
#: nothing but the version to tell which silence it is reading.
REDIRECT_JOURNAL_VERSION = 3
#: The last version whose ``continued`` line was itself the delivery record.
REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION = 1
#: The last version that could not declare the launch window. A ``continued``
#: line at or below it leaves the carry ambiguous rather than releasable.
REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION = 2
#: Read support is per line, not per file. A journal an upgrade caught mid-run
#: holds more than one version, and each line is interpreted as its own writer
#: meant it.
REDIRECT_JOURNAL_SUPPORTED_VERSIONS: tuple[int, ...] = (
    REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION,
    REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION,
    REDIRECT_JOURNAL_VERSION,
)

#: Total journal size. A redirect journal holds one run's corrections, so this
#: is generous rather than tight; the bound exists to keep a runaway producer
#: from filling the state directory, not to ration ordinary use.
REDIRECT_JOURNAL_MAX_BYTES = 4 * 1024 * 1024
#: Headroom below the cap that only terminal records may consume. A submission
#: that used the last byte would leave a request nothing could settle, and an
#: unsettleable request is exactly the "falsely reports acceptance" failure
#: this journal exists to prevent.
REDIRECT_SETTLEMENT_RESERVE_BYTES = 512 * 1024
#: The largest correction one request may carry. Feedback is a message to a
#: worker, not an artifact: anything larger belongs in a file the worker reads.
REDIRECT_MAX_FEEDBACK_BYTES = 64 * 1024
REDIRECT_MAX_OPERATION_ID_CHARS = 64
REDIRECT_MAX_RUN_ID_CHARS = 128
REDIRECT_MAX_SESSION_ID_CHARS = 128
REDIRECT_MAX_HARNESS_CHARS = 64
#: Turn indexes are operational counters. A signed 64-bit ceiling keeps every
#: accepted value serializable even under Python's integer-string conversion
#: guard.
REDIRECT_MAX_TURN_INDEX = (1 << 63) - 1

#: How long a submitted request stays acceptable. The urgent wakeup reaches a
#: live runner in milliseconds, so this is a bound on how long a *missed*
#: wakeup may leave a request live — long enough for the runner's ordinary poll
#: to notice, short enough that a correction cannot surface in a turn the
#: operator has long stopped thinking about.
REDIRECT_DEFAULT_TTL_SECONDS = 120
REDIRECT_MIN_TTL_SECONDS = 1
REDIRECT_MAX_TTL_SECONDS = 3600

RecordKind = Literal[
    "submitted",
    "accepted",
    "rejected",
    "expired",
    "continued",
    "launching",
    "delivered",
    "released",
    "acknowledged",
]
KIND_SUBMITTED: RecordKind = "submitted"
KIND_ACCEPTED: RecordKind = "accepted"
KIND_REJECTED: RecordKind = "rejected"
KIND_EXPIRED: RecordKind = "expired"
KIND_CONTINUED: RecordKind = "continued"
KIND_LAUNCHING: RecordKind = "launching"
KIND_DELIVERED: RecordKind = "delivered"
KIND_RELEASED: RecordKind = "released"
KIND_ACKNOWLEDGED: RecordKind = "acknowledged"
RECORD_KINDS: tuple[RecordKind, ...] = (
    KIND_SUBMITTED,
    KIND_ACCEPTED,
    KIND_REJECTED,
    KIND_EXPIRED,
    KIND_CONTINUED,
    KIND_LAUNCHING,
    KIND_DELIVERED,
    KIND_RELEASED,
    KIND_ACKNOWLEDGED,
)
#: The kinds that describe what happened to an already-accepted request. They
#: are named as a group because the reducer routes on the group rather than on
#: each kind, and because they change no state: ``a request is settled once``
#: holds no matter how many of these a request collects. The settlements are the
#: remainder, which is how :meth:`_Replay._apply` reaches them.
ANNOTATION_KINDS: tuple[RecordKind, ...] = (
    KIND_CONTINUED,
    KIND_LAUNCHING,
    KIND_DELIVERED,
    KIND_RELEASED,
    KIND_ACKNOWLEDGED,
)
#: The annotations that say something about a carry already on the record, as
#: opposed to creating one. All of them name the continuation they are about and
#: are refused unless that is the carry the journal currently holds.
CARRY_ANNOTATION_KINDS: tuple[RecordKind, ...] = (
    KIND_LAUNCHING,
    KIND_DELIVERED,
    KIND_RELEASED,
    KIND_ACKNOWLEDGED,
)

RequestState = Literal["submitted", "accepted", "rejected", "expired"]
STATE_SUBMITTED: RequestState = "submitted"
STATE_ACCEPTED: RequestState = "accepted"
STATE_REJECTED: RequestState = "rejected"
STATE_EXPIRED: RequestState = "expired"
REQUEST_STATES: tuple[RequestState, ...] = (
    STATE_SUBMITTED,
    STATE_ACCEPTED,
    STATE_REJECTED,
    STATE_EXPIRED,
)
#: The states a request never leaves. ``submitted`` is deliberately the only
#: one missing: everything else is an answer, and answers are not revised.
TERMINAL_STATES: tuple[RequestState, ...] = (
    STATE_ACCEPTED,
    STATE_REJECTED,
    STATE_EXPIRED,
)

SettlementReason = Literal[
    "expired",
    "stale-target",
    "terminal-audit",
    "urgent-channel-unavailable",
    "run-ended",
    "superseded",
    "operator-cancelled",
]
REASON_EXPIRED: SettlementReason = "expired"
REASON_STALE_TARGET: SettlementReason = "stale-target"
REASON_TERMINAL_AUDIT: SettlementReason = "terminal-audit"
REASON_URGENT_CHANNEL_UNAVAILABLE: SettlementReason = "urgent-channel-unavailable"
REASON_RUN_ENDED: SettlementReason = "run-ended"
REASON_SUPERSEDED: SettlementReason = "superseded"
REASON_OPERATOR_CANCELLED: SettlementReason = "operator-cancelled"
SETTLEMENT_REASONS: tuple[SettlementReason, ...] = (
    REASON_EXPIRED,
    REASON_STALE_TARGET,
    REASON_TERMINAL_AUDIT,
    REASON_URGENT_CHANNEL_UNAVAILABLE,
    REASON_RUN_ENDED,
    REASON_SUPERSEDED,
    REASON_OPERATOR_CANCELLED,
)

# Failures raised to a caller. Stable strings so the CLI, the Workbench, and
# the bridge all say the same thing about the same refusal.
ERROR_JOURNAL_READ = "redirect-journal-read"
ERROR_JOURNAL_WRITE = "redirect-journal-write"
ERROR_JOURNAL_FULL = "redirect-journal-full"
ERROR_INVALID_TARGET = "invalid-redirect-target"
ERROR_INVALID_FEEDBACK = "invalid-redirect-feedback"
ERROR_INVALID_TTL = "invalid-redirect-ttl"
ERROR_INVALID_OPERATION_ID = "invalid-redirect-operation-id"
ERROR_INVALID_REASON = "invalid-redirect-reason"
ERROR_UNKNOWN_REQUEST = "unknown-redirect-request"
ERROR_ALREADY_SETTLED = "redirect-already-settled"
ERROR_TARGET_MISMATCH = "redirect-target-mismatch"
ERROR_REQUEST_EXPIRED = "redirect-request-expired"
ERROR_INVALID_CONTINUATION = "invalid-redirect-continuation"
ERROR_NOT_ACCEPTED = "redirect-not-accepted"
ERROR_ALREADY_CARRIED = "redirect-already-carried"
ERROR_NOT_CARRIED = "redirect-not-carried"
ERROR_CONTINUATION_MISMATCH = "redirect-continuation-mismatch"
ERROR_ALREADY_LAUNCHING = "redirect-already-launching"
ERROR_ALREADY_DELIVERED = "redirect-already-delivered"
ERROR_ALREADY_ACKNOWLEDGED = "redirect-already-acknowledged"
ERROR_REPLAY_CONFLICT = "redirect-replay-conflict"

#: Which half of a request's identity a resubmission disagreed with. Reported
#: rather than collapsed into one refusal because the two have different
#: answers: changed words are a second correction the operator has to decide
#: about, while a changed target is a client correcting a turn other than the
#: one its id is bound to.
CONFLICT_FEEDBACK = "feedback"
CONFLICT_TARGET = "target"
REPLAY_CONFLICTS: tuple[str, ...] = (CONFLICT_FEEDBACK, CONFLICT_TARGET)

# Why one physical line was not applied. A rejected line is reported without
# its bytes: a malformed submission still carries the operator's words.
REJECT_EMPTY_LINE = "empty-line"
REJECT_INVALID_JSON = "invalid-json"
REJECT_NOT_OBJECT = "not-object"
REJECT_UNSUPPORTED_VERSION = "unsupported-version"
REJECT_UNKNOWN_KIND = "unknown-kind"
REJECT_MISSING_ID = "missing-id"
REJECT_DUPLICATE_ID = "duplicate-id"
REJECT_UNKNOWN_REQUEST = "unknown-request"
REJECT_ALREADY_SETTLED = "already-settled"
REJECT_INVALID_TARGET = "invalid-target"
REJECT_INVALID_FEEDBACK = "invalid-feedback"
REJECT_INVALID_TIMESTAMP = "invalid-timestamp"
REJECT_INVALID_REASON = "invalid-reason"
REJECT_INVALID_CONTINUATION = "invalid-continuation"
REJECT_NOT_ACCEPTED = "not-accepted"
REJECT_ALREADY_CARRIED = "already-carried"
REJECT_NOT_CARRIED = "not-carried"
REJECT_CONTINUATION_MISMATCH = "continuation-mismatch"
REJECT_ALREADY_LAUNCHING = "already-launching"
REJECT_ALREADY_DELIVERED = "already-delivered"
REJECT_ALREADY_ACKNOWLEDGED = "already-acknowledged"
