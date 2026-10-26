"""History primitives."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping

from .timestamps import iso_epoch

# -- ASL's file and event names ---------------------------------------------
#
# Mirrored rather than imported, because this package may not import
# `agent_session_loop`. `tests/test_workbench_history.py` compares every name
# and every vocabulary below against the live ASL constant in both directions,
# so a rename there fails here instead of silently importing nothing.

EVENTS_FILE_NAME = "events.jsonl"


STATE_FILE_NAME = "state.json"


RUN_STARTED_EVENT = "run_started"


RUN_FINISHED_EVENT = "run_finished"


MAILBOX_EVENT = "mailbox"


MAILBOX_ACTION_WAIT_STARTED = "wait-started"


MAILBOX_ACTION_WAIT_CLEARED = "wait-cleared"


#: The only event names that are parsed. Every other record in the stream is
#: stepped over without being turned into objects, which is both what keeps a
#: multi-megabyte corpus affordable and what keeps turn payloads -- the records
#: carrying harness output -- from ever being decoded in this process.
IMPORTED_EVENT_NAMES: frozenset[str] = frozenset(
    {RUN_STARTED_EVENT, RUN_FINISHED_EVENT, MAILBOX_EVENT}
)


#: The byte probes that decide whether a line is worth parsing at all. One
#: substring test per name, run before any JSON parse.
EVENT_NAME_PROBES: tuple[bytes, ...] = (
    RUN_STARTED_EVENT.encode("ascii"),
    RUN_FINISHED_EVENT.encode("ascii"),
    MAILBOX_EVENT.encode("ascii"),
)


#: ASL's ``events.EVENT_SCHEMA_VERSION`` is 2; version 1 records are still on
#: disk in older corpora and are read rather than discarded.
SUPPORTED_EVENT_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2)


# -- Outcome vocabularies ---------------------------------------------------

HISTORY_OUTCOME_COMPLETED = "completed"


HISTORY_OUTCOME_STOPPED = "stopped"


HISTORY_OUTCOME_PAUSED = "paused"


HISTORY_OUTCOME_FAILED = "failed"


HISTORY_OUTCOME_ABANDONED = "abandoned"


HISTORY_OUTCOME_UNKNOWN = "unknown"


HISTORY_OUTCOMES: tuple[str, ...] = (
    HISTORY_OUTCOME_COMPLETED,
    HISTORY_OUTCOME_STOPPED,
    HISTORY_OUTCOME_PAUSED,
    HISTORY_OUTCOME_FAILED,
    HISTORY_OUTCOME_ABANDONED,
    HISTORY_OUTCOME_UNKNOWN,
)


#: The outcomes a dashboard may render as a success. Exactly one, and it is a
#: frozenset so the membership test reads the same wherever it is written.
HISTORY_SUCCESS_OUTCOMES: frozenset[str] = frozenset({HISTORY_OUTCOME_COMPLETED})


#: ASL's ``run_finished`` reason for a run that finished every session.
ASL_REASON_ALL_COMPLETE = "all-complete"


#: The reasons that mean "ended early, on purpose, without fault".
ASL_CLEAN_STOP_REASONS: tuple[str, ...] = ("stopped", "max-sessions", "dry-run")


#: ASL's ``events.RUN_OUTCOME_ABANDONED``: the marker a later process writes
#: onto a run whose corpse it found.
ASL_OUTCOME_ABANDONED = "abandoned"


#: ASL's ``review.REVIEW_OUTCOMES``. The verdict travels as one of these words
#: and nothing else the verdict object holds travels at all.
REVIEW_OUTCOMES: tuple[str, ...] = ("all-clear", "findings")


#: ASL's ``events.PAUSE_REASONS``, mirrored. A pause reason outside this
#: vocabulary degrades to :data:`PAUSE_REASON_OTHER` rather than travelling: the
#: field is declared as a code, and an unrecognised string in a code field has
#: exactly the shape an operator's own words would arrive in.
PAUSE_REASONS: tuple[str, ...] = (
    "nonzero-exit",
    "timeout",
    "no-status-change",
    "multiple-status-changes",
    "malformed-plan",
    "unknown-status",
    "lock-contention",
    "runner-pause",
    "unexpected-status-change",
    "no-pending-but-incomplete",
    "commit-preflight",
    "commit-failed",
    "harness-unavailable",
    "not-loop-ready",
    "unsafe",
    "blocked",
    "dependent",
    "unresolved-prerequisite",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    "interrupted",
    "review-harness-required",
    "review-mutated-worktree",
    "review-guard-unavailable",
    "workspace-write-guard-unavailable",
    "workspace-write-scope-violation",
    "guard-protected-path-changed",
    "guard-protected-path-unavailable",
    "repository-registry-required",
    "workspace-repository-contention",
    "plan-artifact-tampered",
    "required-checks-incomplete",
    "review-not-clear",
    "conversation-recovery-required",
    "redirect-successor-blocked",
    "redirect-submission-unresolved",
    "redirect-delivery-unrecordable",
    "malformed-review-repair",
    "quota-exhausted",
    "auth-failed",
    "invalid-model",
    "transport-failed",
)


PAUSE_REASON_OTHER = "other"


# -- Question vocabularies --------------------------------------------------
#
# ASL's live-wait bookkeeping is the only durable record of a question having
# been asked: `mailbox`/`wait-started` opens one and `mailbox`/`wait-cleared`
# closes it, carrying the reason it closed under. That reason is an open set --
# it can be a mailbox error code or a supervisor-loss code -- so history maps it
# onto the closed vocabulary below rather than republishing whatever string the
# runner happened to record.

QUESTION_OUTCOME_ANSWERED = "answered"


QUESTION_OUTCOME_RESOLVED = "resolved"


QUESTION_OUTCOME_TIMED_OUT = "timed-out"


QUESTION_OUTCOME_UNRESOLVED = "unresolved"


QUESTION_OUTCOMES: tuple[str, ...] = (
    QUESTION_OUTCOME_ANSWERED,
    QUESTION_OUTCOME_RESOLVED,
    QUESTION_OUTCOME_TIMED_OUT,
    QUESTION_OUTCOME_UNRESOLVED,
)


#: ASL's ``run_coordination_constants`` wait reasons that name a specific ending. Every
#: other reason -- and there are many, because an I/O fault ends a wait too --
#: is :data:`QUESTION_OUTCOME_UNRESOLVED`.
WAIT_REASON_OUTCOMES: tuple[tuple[str, str], ...] = (
    ("reply", QUESTION_OUTCOME_ANSWERED),
    ("resolution", QUESTION_OUTCOME_RESOLVED),
    ("timeout", QUESTION_OUTCOME_TIMED_OUT),
)


# -- Bounds -----------------------------------------------------------------

#: The prefix a checkpoint digests to notice a file rewritten in place. A
#: compaction that *replaces* the file changes the inode and is caught by that;
#: one that rewrites it under the same inode to a length at least as long as the
#: old offset would otherwise be invisible, and the reader would resume in the
#: middle of a record that is no longer there.
HEAD_SAMPLE_BYTES = 4096


#: How much one pass reads, from a single events file and across the corpus. A
#: Mac tracking hundreds of plans would otherwise read every backlog in one pass
#: on its first import. Sources are walked in a stable order, so a budget that
#: runs out drains the earlier plans first and reaches the later ones next pass;
#: nothing starves, because the offset only ever advances past whole records and
#: stopping early is indistinguishable from the file having ended there.
MAX_READ_BYTES_PER_PASS = 4 * 1024 * 1024


#: The largest single record this module parses. Real corpora hold event lines
#: far past this -- a harness turn carrying a whole tool transcript -- and a
#: terminal record is never one of them: the compact ``run_finished`` payload is
#: a few hundred bytes of scalars.
MAX_RECORD_BYTES = 1024 * 1024


#: How far past a pass window a reader may scan to find the end of one such
#: record. Without it the reader has no way past a record longer than a whole
#: pass: it would re-read the same newline-free window forever and never reach
#: the plans after it.
MAX_OVERSIZED_SCAN_BYTES = 64 * 1024 * 1024


#: ``state.json`` is read only for a plan directory. A file past this size is a
#: corpus problem rather than a plan path.
MAX_STATE_BYTES = 4 * 1024 * 1024


#: Runs and questions whose opening record was consumed by an earlier pass ride
#: the checkpoint, because the facts they carry are not repeated on the closing
#: record. Bounded so a corpus of unmatched openings cannot grow a checkpoint
#: without limit.
MAX_OPEN_RUNS = 32


MAX_OPEN_QUESTIONS = 32


#: Entry ids already handed to the caller, kept per source so a re-read after a
#: file replacement does not re-deliver history that was already delivered. The
#: receiver's upsert makes a repeat harmless; this makes it rare.
MAX_DELIVERED_IDS = 256


#: Entries per upload envelope, and the ceiling on how many session ids one
#: entry names.
MAX_HISTORY_ENTRIES_PER_BATCH = 100


MAX_COMPLETED_SESSION_IDS = 64


#: Warnings per import. A warning is a count and a noun, so sixteen is more than
#: a pass can distinctly say.
MAX_WARNINGS = 16


CHECKPOINT_SCHEMA_VERSION = 1


#: A session id is a canonical dotted string of positive integers. Held here
#: rather than imported from the catalog's private helper because the two answer
#: different questions: the catalog validates one id on a summary, and history
#: canonicalises a list of them off a terminal record.
_SESSION_ID_RE = re.compile(r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")


class HistoryError(Exception):
    """Raised when a history record or checkpoint cannot be interpreted."""


# -- Primitives -------------------------------------------------------------


def refuse_non_finite(constant: str) -> object:
    """Reject ``Infinity`` and ``NaN`` while parsing an event record.

    Python's JSON reader accepts all three by default and they are not JSON.
    A non-finite number reaching an entry would travel to a store that cannot
    hold it, so the record carrying one is rejected as malformed at the parse
    rather than discovered three layers later.
    """
    raise ValueError(f"non-finite JSON constant: {constant}")


def epoch(value: object) -> float | None:
    """Read one of ASL's ISO-8601 timestamps as a Unix epoch.

    ASL stamps records with ISO strings and history carries numbers, so exactly
    one conversion happens and it happens here. An unparseable stamp becomes
    ``None`` rather than zero: a run whose ending time is unknown is not a run
    that ended in 1970. A naive stamp is UTC, as :mod:`asl_workbench.timestamps`
    reads it everywhere else.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # `1e999` parses as infinity and a 400-digit integer does not fit a
        # float at all; neither is a moment, and neither may end the pass.
        try:
            number = float(value)
        except OverflowError:
            return None
        return number if math.isfinite(number) and number >= 0 else None
    stamp = iso_epoch(value)
    return stamp if stamp is not None and stamp >= 0 else None


def head_digest(payload: bytes) -> str:
    """The sixteen-character digest a checkpoint stores for a file prefix."""
    return hashlib.sha256(payload).hexdigest()[:16]


def event_schema_version(record: Mapping[str, object]) -> int:
    """The schema version one event record declares, defaulting to 1.

    ASL wrote version-1 records before the field existed, so a record without
    one is version 1. A field that is present but is not a positive integer is a
    malformed record rather than a version to guess at.
    """
    value = record.get("version")
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("event schema version must be a positive integer")
    return value


def complete_records(chunk: bytes) -> bytes:
    """The prefix of ``chunk`` that is whole records.

    Only bytes up to the last newline are a record. Whatever follows is a line
    still being appended, and reading half of it once is how a run gets a
    corrupted ending forever.
    """
    boundary = chunk.rfind(b"\n")
    return chunk[: boundary + 1] if boundary >= 0 else b""


def _nonempty(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _integer(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _repository_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _session_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _SESSION_ID_RE.match(text) else None
