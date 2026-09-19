"""Canonical observations and incremental history, beside ASL's own vocabulary.

The Companion imports ASL's ``events.jsonl`` files into seven-day history. That
import is the only place in either application that knows what a run's *ending*
means -- and it lives in the other repository, mirroring ASL's constants from
memory and reading a corpus it does not own. This module is the interpretation
half of that code, moved next to the definitions it depends on, so a reason
added to ASL's vocabulary fails a test here rather than arriving on the Pi as an
unrecognised string.

Four rules are the content of the module. Each is code rather than a convention:

* **An outcome comes from a terminal record, never from an absence.**
  :func:`classify_outcome` names exactly one combination of recorded fields
  ``completed``; everything missing, unrecognised, or reconstructed lands on
  ``unknown`` or ``abandoned``. :func:`project_entry` then removes the last way
  a success could be invented: a ``completed`` record with no
  ``run_finished_at`` is downgraded, because the one outcome rendered as a
  success has to say when it succeeded.

* **Attribution is by run id, never by recency.** Two runs of one plan minutes
  apart append to the same file, so "the last start I saw" is not the run that
  just finished. :meth:`SourceCheckpoint.find` joins each terminal record to its
  own ``run_started``, and a record naming no run is dropped rather than
  attached to whichever run was open.

* **Nothing is consumed until it has been delivered.** :func:`consume_records`
  returns the checkpoint that *would* consume the bytes it read; the caller
  persists it only after the entries have been handed on. A crash in that gap
  re-reads the same bytes and re-derives byte-identical entries with the same
  ids, which an upsert absorbs -- whereas advancing first loses a run's ending
  permanently on one badly timed crash.

* **One projection feeds both consumers.** :func:`build_view` and
  :func:`batches` are two renderings of the same :class:`HistoryEntry` objects,
  not two derivations from the same bytes. Local presentation and connector
  publication therefore cannot disagree about what a run did, which they could
  and did while the Companion held its own copy of the interpretation.

Nothing here touches the filesystem. ``agent_session_loop.workbench_history``
discovers the state directories, reads the bytes, and persists the checkpoints;
this module is given bytes and parsed documents and returns records, which is
what keeps it loadable on a Pi that has no ASL corpus of its own.

Identity is not re-derived: :mod:`asl_workbench.catalog` already holds the
digest scheme, the label sanitiser, and the project naming, and history reuses
all three so an entry's ``plan_key`` is the same sixteen characters the catalog
publishes for the same plan.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from .catalog import (
    MAX_LABEL_CHARS,
    digest_of,
    display_label,
    plan_key,
    project_names,
    run_key,
    safe_label,
)

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

#: ASL's ``run_coordination`` wait reasons that name a specific ending. Every
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
    that ended in 1970.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    try:
        stamp = moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return stamp if stamp >= 0 else None


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


# -- Classification ---------------------------------------------------------


def classify_outcome(payload: Mapping[str, object]) -> str:
    """Name how a run ended, from the fields its terminal record recorded.

    The order is the point. A reconstructed record is classified as abandoned
    before its ``ok``/``paused`` flags are read, because those describe the state
    the dead run was last seen in rather than a decision it made. A paused run is
    classified before a failed one, because ASL records a pause as ``ok: false``
    and a pause is a run waiting for a person, not a run that failed.

    ``completed`` requires all three of ``ok``, ``paused``, and ASL's
    ``all-complete`` reason to agree. A record missing any of them, or carrying a
    reason this vocabulary does not hold, is ``unknown``: a cockpit can show an
    unknown ending and an operator can go and look, whereas a guessed completion
    is a run nobody ever looks at again.
    """
    if (
        payload.get("synthetic") is True
        or payload.get("outcome") == ASL_OUTCOME_ABANDONED
    ):
        return HISTORY_OUTCOME_ABANDONED
    paused = payload.get("paused")
    ok = payload.get("ok")
    reason = payload.get("reason")
    if paused is True:
        return HISTORY_OUTCOME_PAUSED
    if not isinstance(ok, bool) or not isinstance(paused, bool):
        return HISTORY_OUTCOME_UNKNOWN
    if ok:
        if reason == ASL_REASON_ALL_COMPLETE:
            return HISTORY_OUTCOME_COMPLETED
        if reason in ASL_CLEAN_STOP_REASONS:
            return HISTORY_OUTCOME_STOPPED
        return HISTORY_OUTCOME_UNKNOWN
    return HISTORY_OUTCOME_FAILED


def pause_reason_code(payload: Mapping[str, object], outcome: str) -> str | None:
    """The closed pause code for a paused run, and nothing for any other ending.

    ASL puts its pause reason in ``pause_reason`` and mirrors it into ``reason``,
    so both are consulted.
    """
    if outcome != HISTORY_OUTCOME_PAUSED:
        return None
    for key in ("pause_reason", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value in PAUSE_REASONS:
            return value
    return PAUSE_REASON_OTHER


def question_outcome(reason: object) -> str:
    """Map one ``wait-cleared`` reason onto the closed question vocabulary."""
    for wait_reason, outcome in WAIT_REASON_OUTCOMES:
        if reason == wait_reason:
            return outcome
    return QUESTION_OUTCOME_UNRESOLVED


def _review_outcome(payload: Mapping[str, object]) -> str | None:
    verdict = payload.get("review_verdict")
    if not isinstance(verdict, Mapping):
        return None
    outcome = verdict.get("outcome")
    return outcome if isinstance(outcome, str) and outcome in REVIEW_OUTCOMES else None


def _session_ids(payload: Mapping[str, object]) -> tuple[str, ...]:
    values = payload.get("completed_session_ids")
    if not isinstance(values, list):
        values = payload.get("completed_sessions")
    if not isinstance(values, list):
        return ()
    canonical: list[str] = []
    for value in values:
        session = _session_id(value)
        if session is not None and session not in canonical:
            canonical.append(session)
        if len(canonical) >= MAX_COMPLETED_SESSION_IDS:
            break
    return tuple(canonical)


def _count(payload: Mapping[str, object], key: str) -> int | None:
    counts = payload.get("counts")
    if not isinstance(counts, Mapping):
        return None
    value = counts.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _duration(payload: Mapping[str, object]) -> float | None:
    value = payload.get("duration_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    # A negative or non-finite duration is a clock fault, not a duration. It is
    # dropped rather than clamped: zero would read as an instantaneous run.
    if not math.isfinite(number) or number < 0:
        return None
    return number


# -- Plan facts -------------------------------------------------------------


@dataclass(frozen=True)
class PlanFacts:
    """What a plan's ``state.json`` says about where and what the plan is.

    The last fallback for a plan directory, used when neither the terminal
    record nor a remembered ``run_started`` supplied one, and the source of the
    project names when the terminal record carries no repository list.
    """

    plan_dir: str | None = None
    workspace_root: str | None = None
    repository_count: int = 0
    projects: tuple[str, ...] = ()


NO_PLAN_FACTS = PlanFacts()
"""The facts of a plan that said nothing, shared because it is immutable."""


def plan_facts_from_state(document: object) -> PlanFacts:
    """Read plan facts from a parsed ``state.json`` document.

    Anything that is not a mapping produces empty facts rather than an error:
    one damaged plan must not stop the import of the other several hundred, and
    the caller has no better answer to give than "this plan said nothing".
    """
    if not isinstance(document, Mapping):
        return NO_PLAN_FACTS
    workspace_root = _nonempty(document.get("workspace_root")) or _nonempty(
        document.get("cwd")
    )
    repositories = document.get("repositories")
    return PlanFacts(
        plan_dir=_nonempty(document.get("plan_dir")),
        workspace_root=workspace_root,
        repository_count=_repository_count(repositories),
        projects=project_names(
            repositories if isinstance(repositories, list) else (),
            workspace_root=workspace_root,
        ),
    )


# -- Checkpoints ------------------------------------------------------------


@dataclass(frozen=True)
class OpenRun:
    """The facts a ``run_started`` carries that its terminal record does not.

    ASL's compact ``run_finished`` payload drops the plan directory, the
    harness, and the configured model, so a terminal record alone cannot name
    the plan it belongs to. These ride the checkpoint because the start that
    carried them may have been consumed by a pass that ran days ago.
    """

    run_id: str
    plan_dir: str | None = None
    harness: str | None = None
    model: str | None = None
    started_at: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "plan_dir": self.plan_dir,
            "harness": self.harness,
            "model": self.model,
            "started_at": self.started_at,
        }

    @classmethod
    def from_payload(cls, document: object) -> OpenRun | None:
        if not isinstance(document, Mapping):
            return None
        run_id = _nonempty(document.get("run_id"))
        if run_id is None:
            return None
        return cls(
            run_id=run_id,
            plan_dir=_nonempty(document.get("plan_dir")),
            harness=_nonempty(document.get("harness")),
            model=_nonempty(document.get("model")),
            started_at=epoch(document.get("started_at")),
        )


@dataclass(frozen=True)
class OpenQuestion:
    """A live mailbox wait whose closing record has not been read yet.

    Carried so a question that was asked and never cleared -- because the run
    died holding it -- is recorded as unresolved history rather than vanishing.
    """

    question_id: str
    run_id: str | None = None
    session_id: str | None = None
    asked_at: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "asked_at": self.asked_at,
        }

    @classmethod
    def from_payload(cls, document: object) -> OpenQuestion | None:
        if not isinstance(document, Mapping):
            return None
        question_id = _nonempty(document.get("question_id"))
        if question_id is None:
            return None
        return cls(
            question_id=question_id,
            run_id=_nonempty(document.get("run_id")),
            session_id=_session_id(document.get("session_id")),
            asked_at=epoch(document.get("asked_at")),
        )


@dataclass(frozen=True)
class SourceCheckpoint:
    """How far one event file has been consumed, and what the file was.

    ``device``/``inode`` and ``head_digest`` are what make resuming safe.
    Together they answer "is this the same file I was reading", and the three
    ways the answer can be no -- replaced, truncated, rewritten in place -- all
    resolve to re-reading from the start, which duplicate suppression absorbs.

    ``head_length`` is recorded alongside the digest, and the pair is the whole
    trick: the digest covers a *fixed* prefix rather than "the first few
    kilobytes", so an ordinary append leaves it unchanged. Digesting whatever
    happens to be at the head today would make every append look like a rewrite
    on a file still shorter than the sample, and the reader would restart on
    every pass.
    """

    source_key: str
    device: int = -1
    inode: int = -1
    offset: int = 0
    head_digest: str = ""
    head_length: int = 0
    records: int = 0
    plan_dir: str | None = None
    open_runs: tuple[OpenRun, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
    delivered: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "source_key": self.source_key,
            "device": self.device,
            "inode": self.inode,
            "offset": self.offset,
            "head_digest": self.head_digest,
            "head_length": self.head_length,
            "records": self.records,
            "plan_dir": self.plan_dir,
            "open_runs": [run.to_payload() for run in self.open_runs],
            "open_questions": [
                question.to_payload() for question in self.open_questions
            ],
            "delivered": list(self.delivered),
        }

    @classmethod
    def from_payload(cls, document: object) -> SourceCheckpoint | None:
        if not isinstance(document, Mapping):
            return None
        key = _nonempty(document.get("source_key"))
        if key is None:
            return None
        runs = document.get("open_runs")
        open_runs = (
            tuple(
                run
                for run in (OpenRun.from_payload(entry) for entry in runs)
                if run is not None
            )
            if isinstance(runs, list)
            else ()
        )
        questions = document.get("open_questions")
        open_questions = (
            tuple(
                question
                for question in (
                    OpenQuestion.from_payload(entry) for entry in questions
                )
                if question is not None
            )
            if isinstance(questions, list)
            else ()
        )
        delivered = document.get("delivered")
        digest = document.get("head_digest")
        return cls(
            source_key=key,
            device=_integer(document.get("device"), -1),
            inode=_integer(document.get("inode"), -1),
            offset=max(0, _integer(document.get("offset"), 0)),
            head_digest=digest if isinstance(digest, str) else "",
            head_length=max(0, _integer(document.get("head_length"), 0)),
            records=max(0, _integer(document.get("records"), 0)),
            plan_dir=_nonempty(document.get("plan_dir")),
            open_runs=open_runs[-MAX_OPEN_RUNS:],
            open_questions=open_questions[-MAX_OPEN_QUESTIONS:],
            delivered=(
                tuple(entry for entry in delivered if isinstance(entry, str))[
                    -MAX_DELIVERED_IDS:
                ]
                if isinstance(delivered, list)
                else ()
            ),
        )

    def remember(self, run: OpenRun) -> SourceCheckpoint:
        """Record one run's start facts, replacing any earlier entry for it."""
        kept = tuple(entry for entry in self.open_runs if entry.run_id != run.run_id)
        return replace(self, open_runs=(*kept, run)[-MAX_OPEN_RUNS:])

    def find(self, run_id: str | None) -> OpenRun | None:
        """The remembered start for ``run_id``, joined by id and never by order."""
        if run_id is None:
            return None
        for entry in reversed(self.open_runs):
            if entry.run_id == run_id:
                return entry
        return None

    def remember_question(self, question: OpenQuestion) -> SourceCheckpoint:
        """Record one live wait, replacing any earlier entry for it."""
        kept = tuple(
            entry
            for entry in self.open_questions
            if entry.question_id != question.question_id
        )
        return replace(self, open_questions=(*kept, question)[-MAX_OPEN_QUESTIONS:])

    def find_question(self, question_id: str | None) -> OpenQuestion | None:
        if question_id is None:
            return None
        for entry in reversed(self.open_questions):
            if entry.question_id == question_id:
                return entry
        return None

    def forget_question(self, question_id: str) -> SourceCheckpoint:
        kept = tuple(
            entry for entry in self.open_questions if entry.question_id != question_id
        )
        return replace(self, open_questions=kept)

    def questions_for_run(self, run_id: str | None) -> tuple[OpenQuestion, ...]:
        """Every still-open wait attributed to ``run_id``."""
        if run_id is None:
            return ()
        return tuple(entry for entry in self.open_questions if entry.run_id == run_id)

    def record_delivered(self, entry_ids: Iterable[str]) -> SourceCheckpoint:
        known = set(self.delivered)
        fresh = [entry for entry in entry_ids if entry not in known]
        if not fresh:
            return self
        return replace(self, delivered=(*self.delivered, *fresh)[-MAX_DELIVERED_IDS:])


def checkpoint_document(
    checkpoints: Sequence[SourceCheckpoint],
) -> dict[str, object]:
    """The durable JSON document one set of checkpoints serialises to."""
    return {
        "version": CHECKPOINT_SCHEMA_VERSION,
        "sources": [
            checkpoint.to_payload()
            for checkpoint in sorted(checkpoints, key=lambda item: item.source_key)
        ],
    }


def checkpoints_from_document(document: object) -> dict[str, SourceCheckpoint]:
    """Read a checkpoint document, discarding anything it cannot vouch for.

    A document from an unknown schema, or one that is not a mapping, or one
    whose ``sources`` is not a list, yields no checkpoints. Starting the window
    again costs a re-read that duplicate suppression absorbs; guessing at a
    corrupted offset costs a run's ending.
    """
    if not isinstance(document, Mapping):
        return {}
    if document.get("version") != CHECKPOINT_SCHEMA_VERSION:
        return {}
    sources = document.get("sources")
    if not isinstance(sources, list):
        return {}
    loaded: dict[str, SourceCheckpoint] = {}
    for entry in sources:
        checkpoint = SourceCheckpoint.from_payload(entry)
        if checkpoint is not None:
            loaded[checkpoint.source_key] = checkpoint
    return loaded


def restart_required(
    checkpoint: SourceCheckpoint,
    *,
    device: int,
    inode: int,
    size: int,
    head: bytes = b"",
) -> bool:
    """Whether this file has to be read from the beginning again.

    Three separate things make a resume unsafe, and each is checked because each
    happens: the file was replaced (new inode), it was truncated or compacted to
    shorter than the offset, or it was rewritten in place at the same or greater
    length. Only the last needs the file's contents, and it is decided by
    re-digesting exactly the prefix the checkpoint digested -- a fixed byte range
    an append cannot disturb.

    ``head`` is the file's first :attr:`SourceCheckpoint.head_length` bytes. The
    caller reads them rather than this function, because this half of the
    program does not open files; the read is bounded by
    :data:`HEAD_SAMPLE_BYTES`, so it costs one four-kilobyte read per source per
    pass whether or not the answer turns out to need it.
    """
    if checkpoint.offset <= 0:
        return False
    if (checkpoint.device, checkpoint.inode) != (device, inode):
        return True
    if size < checkpoint.offset:
        return True
    if not checkpoint.head_digest or checkpoint.head_length <= 0:
        return False
    if len(head) < checkpoint.head_length:
        return True
    return head_digest(head[: checkpoint.head_length]) != checkpoint.head_digest


# -- Entries ----------------------------------------------------------------


@dataclass(frozen=True)
class HistoryEntry:
    """One finished run, as both the local view and the remote provider see it.

    Every field is a digest, a timestamp, a counter, or a member of a vocabulary
    declared in this module. There is no path, no operator-typed prose, and no
    harness output, because none of those has a name here to be copied into:
    :func:`project_entry` reads the terminal record field by field rather than
    copying it, so a future ASL that adds a field adds nothing to this entry.
    """

    entry_id: str
    source_key: str
    plan_key: str
    run_key: str
    outcome: str
    recorded_at: float
    event_schema_version: int
    project: str
    projects: tuple[str, ...] = ()
    run_id: str | None = None
    harness: str | None = None
    model: str | None = None
    pause_reason: str | None = None
    run_started_at: float | None = None
    run_finished_at: float | None = None
    duration_seconds: float | None = None
    turn_count: int | None = None
    completed_session_count: int | None = None
    pending_session_count: int | None = None
    completed_session_ids: tuple[str, ...] = ()
    review_completed: bool | None = None
    review_outcome: str | None = None
    reconstructed: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether this entry may be rendered as a successful run."""
        return self.outcome in HISTORY_SUCCESS_OUTCOMES

    def to_payload(self) -> dict[str, object]:
        """The wire and storage shape, with absent facts absent rather than null."""
        payload: dict[str, object] = {
            "entry_id": self.entry_id,
            "source_key": self.source_key,
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "project": self.project,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "event_schema_version": self.event_schema_version,
        }
        if self.projects:
            payload["projects"] = list(self.projects)
        for key, value in (
            ("run_id", self.run_id),
            ("harness", self.harness),
            ("model", self.model),
            ("pause_reason", self.pause_reason),
            ("run_started_at", self.run_started_at),
            ("run_finished_at", self.run_finished_at),
            ("duration_seconds", self.duration_seconds),
            ("turn_count", self.turn_count),
            ("completed_session_count", self.completed_session_count),
            ("pending_session_count", self.pending_session_count),
            ("review_completed", self.review_completed),
            ("review_outcome", self.review_outcome),
        ):
            if value is not None:
                payload[key] = value
        if self.completed_session_ids:
            payload["completed_session_ids"] = list(self.completed_session_ids)
        if self.reconstructed:
            payload["reconstructed"] = True
        return payload


@dataclass(frozen=True)
class QuestionEntry:
    """One question a run asked, and how the wait for it ended.

    The question's text never appears, here or anywhere below it: what history
    records is that a run stopped to ask, when, and whether anybody answered.
    ``question_key`` is a digest rather than ASL's own question id, for the same
    reason ``plan_key`` is a digest rather than a path -- history publishes its
    own identifiers, not another program's.
    """

    entry_id: str
    source_key: str
    plan_key: str
    run_key: str
    question_key: str
    outcome: str
    recorded_at: float
    session_id: str | None = None
    asked_at: float | None = None
    resolved_at: float | None = None
    waited_seconds: float | None = None

    @property
    def answered(self) -> bool:
        return self.outcome == QUESTION_OUTCOME_ANSWERED

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "entry_id": self.entry_id,
            "source_key": self.source_key,
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "question_key": self.question_key,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
        }
        for key, value in (
            ("session_id", self.session_id),
            ("asked_at", self.asked_at),
            ("resolved_at", self.resolved_at),
            ("waited_seconds", self.waited_seconds),
        ):
            if value is not None:
                payload[key] = value
        return payload


def entry_id_for(plan: str, run: str) -> str:
    """The stable id of one run's history entry."""
    return digest_of(["history-entry", plan, run])


def question_key_for(plan: str, question_id: str) -> str:
    """The stable, opaque identity of one question of one plan."""
    return digest_of(["history-question-key", plan, question_id])


def question_entry_id_for(plan: str, run: str, question: str) -> str:
    """The stable id of one question's history entry."""
    return digest_of(["history-question", plan, run, question])


def project_entry(
    body: Mapping[str, object],
    *,
    source_key: str,
    plan_dir: str,
    facts: PlanFacts,
    started: OpenRun | None,
    recorded_at: float,
    version: int,
) -> HistoryEntry:
    """Build one history entry from one terminal record.

    Every field is read by name. Nothing is copied wholesale, so the fields that
    carry prose -- ``pause_message``, ``pause_details``, the review verdict's
    status line, the assurance report -- have no name in the entry to be copied
    into.
    """
    plan = plan_key(plan_dir)
    run_id = _nonempty(body.get("run_id"))
    run = run_key(plan, run_id)
    outcome = classify_outcome(body)

    workspace_root = _nonempty(body.get("workspace_root")) or facts.workspace_root
    repositories = body.get("repositories")
    count = _repository_count(repositories) or facts.repository_count
    if isinstance(repositories, list) and repositories:
        projects = project_names(repositories, workspace_root=workspace_root)
    elif facts.projects:
        projects = facts.projects
    else:
        projects = project_names((), workspace_root=workspace_root)

    finished_at = epoch(body.get("run_finished_at"))
    if finished_at is None and outcome == HISTORY_OUTCOME_COMPLETED:
        # The one outcome rendered as a success has to say when it succeeded.
        # Without that it is a claim with no evidence behind it, and the honest
        # label for a claim with no evidence is already in the vocabulary.
        outcome = HISTORY_OUTCOME_UNKNOWN

    return HistoryEntry(
        entry_id=entry_id_for(plan, run),
        source_key=source_key,
        plan_key=plan,
        run_key=run,
        outcome=outcome,
        recorded_at=recorded_at,
        event_schema_version=version,
        project=display_label(
            workspace_root=workspace_root,
            plan_dir=plan_dir,
            repository_count=count,
        ),
        projects=projects,
        run_id=run_id,
        harness=_nonempty(body.get("harness"))
        or (started.harness if started else None),
        model=safe_label(
            _nonempty(body.get("observed_model"))
            or _nonempty(body.get("model"))
            or (started.model if started else None),
            limit=MAX_LABEL_CHARS,
        ),
        pause_reason=pause_reason_code(body, outcome),
        run_started_at=epoch(body.get("run_started_at"))
        or (started.started_at if started else None),
        run_finished_at=finished_at,
        duration_seconds=_duration(body),
        turn_count=_count_field(body.get("turn_count")),
        completed_session_count=_count(body, "complete"),
        pending_session_count=_count(body, "pending"),
        completed_session_ids=_session_ids(body),
        review_completed=_flag(body.get("review_completed")),
        review_outcome=_review_outcome(body),
        reconstructed=outcome == HISTORY_OUTCOME_ABANDONED,
    )


def _flag(value: object) -> bool | None:
    """A recorded boolean, or ``None`` when the field said anything else."""
    return value if isinstance(value, bool) else None


def _count_field(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def project_question(
    *,
    source_key: str,
    plan_dir: str,
    question_id: str,
    run_id: str | None,
    session_id: str | None,
    asked_at: float | None,
    resolved_at: float | None,
    recorded_at: float,
    outcome: str,
) -> QuestionEntry:
    """Build one question entry from a wait's opening and closing facts."""
    plan = plan_key(plan_dir)
    run = run_key(plan, run_id)
    question = question_key_for(plan, question_id)
    waited = (
        resolved_at - asked_at
        if asked_at is not None and resolved_at is not None and resolved_at >= asked_at
        else None
    )
    return QuestionEntry(
        entry_id=question_entry_id_for(plan, run, question),
        source_key=source_key,
        plan_key=plan,
        run_key=run,
        question_key=question,
        outcome=outcome,
        recorded_at=recorded_at,
        session_id=session_id,
        asked_at=asked_at,
        resolved_at=resolved_at,
        waited_seconds=waited,
    )


# -- Reading ----------------------------------------------------------------


@dataclass(frozen=True)
class SourceRead:
    """The outcome of interpreting one pass over one event file."""

    checkpoint: SourceCheckpoint
    entries: tuple[HistoryEntry, ...] = ()
    questions: tuple[QuestionEntry, ...] = ()
    restarted: bool = False
    malformed: int = 0
    duplicates: int = 0
    unattributed: int = 0
    expired: int = 0
    undatable: int = 0
    unsupported: int = 0
    oversized: int = 0
    deferred_bytes: int = 0
    read_bytes: int = 0
    unreadable: bool = False
    unresolved: bool = False


def consume_records(
    payload: bytes,
    *,
    checkpoint: SourceCheckpoint,
    source_key: str,
    window_start: float,
    facts: PlanFacts = NO_PLAN_FACTS,
) -> SourceRead:
    """Interpret a block of whole records, advancing ``checkpoint`` as it goes.

    ``payload`` must already have been trimmed to whole records by
    :func:`complete_records`. The returned checkpoint is the one that *would*
    consume them; the caller persists it only after the entries have been
    delivered.
    """
    entries: dict[str, HistoryEntry] = {}
    questions: dict[str, QuestionEntry] = {}
    malformed = duplicates = unattributed = expired = undatable = unsupported = 0
    oversized = 0
    records = checkpoint.records
    unresolved = False

    def delivered_already(entry_id: str) -> bool:
        return (
            entry_id in entries
            or entry_id in questions
            or entry_id in checkpoint.delivered
        )

    for line in payload.split(b"\n"):
        if not line.strip():
            continue
        # A cheap substring test before any parse. Turn records dominate the
        # stream and carry harness output; not decoding them is both the
        # performance decision and a privacy one.
        if not any(probe in line for probe in EVENT_NAME_PROBES):
            continue
        if len(line) > MAX_RECORD_BYTES:
            # A record this size is a transcript, not a run's ending: the
            # compact terminal payload is a few hundred bytes of scalars.
            # Stepped over unparsed so a corpus cannot make this process decode
            # a multi-megabyte harness turn that merely mentions the event name.
            oversized += 1
            continue
        try:
            record = json.loads(line, parse_constant=refuse_non_finite)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(record, Mapping):
            malformed += 1
            continue
        name = record.get("name")
        if name not in IMPORTED_EVENT_NAMES:
            continue
        records += 1
        try:
            version = event_schema_version(record)
        except ValueError:
            malformed += 1
            continue
        if version not in SUPPORTED_EVENT_SCHEMA_VERSIONS:
            unsupported += 1
            continue
        body = record.get("payload")
        if not isinstance(body, Mapping):
            malformed += 1
            continue

        if name == RUN_STARTED_EVENT:
            run_id = _nonempty(body.get("run_id"))
            if run_id is None:
                unattributed += 1
                continue
            checkpoint = checkpoint.remember(
                OpenRun(
                    run_id=run_id,
                    plan_dir=_nonempty(body.get("plan_dir")),
                    harness=_nonempty(body.get("harness")),
                    model=_nonempty(body.get("model")),
                    started_at=epoch(body.get("run_started_at"))
                    or epoch(record.get("created_at")),
                )
            )
            continue

        if name == MAILBOX_EVENT:
            action = body.get("action")
            if action not in (
                MAILBOX_ACTION_WAIT_STARTED,
                MAILBOX_ACTION_WAIT_CLEARED,
            ):
                continue
            question_id = _nonempty(body.get("question_id"))
            if question_id is None:
                unattributed += 1
                continue
            run_id = _nonempty(body.get("run_id"))
            asked_at = epoch(body.get("started_at"))
            session = _session_id(body.get("session_id"))
            if action == MAILBOX_ACTION_WAIT_STARTED:
                checkpoint = checkpoint.remember_question(
                    OpenQuestion(
                        question_id=question_id,
                        run_id=run_id,
                        session_id=session,
                        asked_at=asked_at or epoch(record.get("created_at")),
                    )
                )
                continue
            recorded_at = epoch(record.get("created_at"))
            if recorded_at is None:
                undatable += 1
                continue
            opened = checkpoint.find_question(question_id)
            checkpoint = checkpoint.forget_question(question_id)
            if recorded_at < window_start:
                expired += 1
                continue
            plan_dir = _resolve_plan_dir(
                checkpoint,
                run_id=run_id or (opened.run_id if opened else None),
                declared=_nonempty(body.get("plan_dir")),
                facts=facts,
            )
            if plan_dir is None:
                unresolved = True
                continue
            question = project_question(
                source_key=source_key,
                plan_dir=plan_dir,
                question_id=question_id,
                run_id=run_id or (opened.run_id if opened else None),
                session_id=session or (opened.session_id if opened else None),
                asked_at=asked_at or (opened.asked_at if opened else None),
                resolved_at=recorded_at,
                recorded_at=recorded_at,
                outcome=question_outcome(body.get("reason")),
            )
            if delivered_already(question.entry_id):
                duplicates += 1
                continue
            questions[question.entry_id] = question
            checkpoint = replace(checkpoint, plan_dir=plan_dir)
            continue

        run_id = _nonempty(body.get("run_id"))
        if run_id is None:
            # Attaching this to whichever run was last seen starting is how a
            # rapid rerun inherits the previous run's ending. Dropped.
            unattributed += 1
            continue
        recorded_at = epoch(record.get("created_at")) or epoch(
            body.get("run_finished_at")
        )
        if recorded_at is None:
            undatable += 1
            continue
        if recorded_at < window_start:
            expired += 1
            continue

        started = checkpoint.find(run_id)
        plan_dir = _resolve_plan_dir(
            checkpoint,
            run_id=run_id,
            declared=_nonempty(body.get("plan_dir")),
            facts=facts,
        )
        if plan_dir is None:
            unresolved = True
            continue

        # A wait this run never cleared is history, not live state: the run has
        # ended, so nothing will ever answer it. Recorded before the run's own
        # entry so the two travel in the same batch.
        for stranded in checkpoint.questions_for_run(run_id):
            checkpoint = checkpoint.forget_question(stranded.question_id)
            abandoned = project_question(
                source_key=source_key,
                plan_dir=plan_dir,
                question_id=stranded.question_id,
                run_id=run_id,
                session_id=stranded.session_id,
                asked_at=stranded.asked_at,
                resolved_at=None,
                recorded_at=recorded_at,
                outcome=QUESTION_OUTCOME_UNRESOLVED,
            )
            if delivered_already(abandoned.entry_id):
                duplicates += 1
                continue
            questions[abandoned.entry_id] = abandoned

        entry = project_entry(
            body,
            source_key=source_key,
            plan_dir=plan_dir,
            facts=facts,
            started=started,
            recorded_at=recorded_at,
            version=version,
        )
        if delivered_already(entry.entry_id):
            # The same run's ending seen twice: appended twice by ASL, or
            # re-read after a file replacement. The first record wins, which is
            # the rule ASL's own terminal-evidence reader follows.
            duplicates += 1
            continue
        entries[entry.entry_id] = entry
        checkpoint = replace(checkpoint, plan_dir=plan_dir)

    checkpoint = replace(checkpoint, records=records)
    checkpoint = checkpoint.record_delivered((*entries, *questions))
    return SourceRead(
        checkpoint=checkpoint,
        entries=tuple(entries.values()),
        questions=tuple(questions.values()),
        malformed=malformed,
        duplicates=duplicates,
        unattributed=unattributed,
        expired=expired,
        undatable=undatable,
        unsupported=unsupported,
        oversized=oversized,
        unresolved=unresolved,
    )


def _resolve_plan_dir(
    checkpoint: SourceCheckpoint,
    *,
    run_id: str | None,
    declared: str | None,
    facts: PlanFacts,
) -> str | None:
    """The plan directory a record belongs to, by the only four ways to know it.

    The remembered ``run_started`` first, because it is the record that named
    the plan for this run specifically. Then whatever the record itself
    declared, then the last plan this source resolved, then the plan's own
    ``state.json``. Nothing below that: a record whose plan cannot be named is
    dropped and the pass says so, because attributing it to a neighbour is how
    one plan's history acquires another plan's runs.
    """
    started = checkpoint.find(run_id)
    return (
        (started.plan_dir if started else None)
        or declared
        or checkpoint.plan_dir
        or facts.plan_dir
    )


# -- Import results ---------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    """Everything one import pass produced, and everything it could not."""

    entries: tuple[HistoryEntry, ...] = ()
    questions: tuple[QuestionEntry, ...] = ()
    checkpoints: tuple[SourceCheckpoint, ...] = ()
    window_start: float = 0.0
    window_end: float = 0.0
    sources: int = 0
    restarted_sources: int = 0
    unreadable_sources: int = 0
    unresolved_sources: int = 0
    malformed: int = 0
    duplicates: int = 0
    unattributed: int = 0
    expired: int = 0
    undatable: int = 0
    unsupported: int = 0
    oversized: int = 0
    deferred_bytes: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether this pass saw the whole window it claims to describe.

        False while any source is unread or unidentifiable, while any bytes
        remain to consume, and whenever a record was dropped -- unparseable,
        undatable, oversized, or naming no run. Every one of those is a run
        whose ending may be missing, and a receiver treats an incomplete import
        the way it treats a truncated snapshot: as proof of nothing about what
        is not in it.

        Expired records are deliberately not counted. Being outside the
        retention window is the filter working, not evidence going missing.
        """
        return (
            self.unreadable_sources == 0
            and self.unresolved_sources == 0
            and self.deferred_bytes == 0
            and self.malformed == 0
            and self.unattributed == 0
            and self.undatable == 0
            and self.unsupported == 0
            and self.oversized == 0
        )


def _entry_order(entry: HistoryEntry) -> tuple[float, str]:
    """Oldest ending first, with the entry id breaking ties deterministically."""
    return (entry.recorded_at, entry.entry_id)


def _question_order(entry: QuestionEntry) -> tuple[float, str]:
    return (entry.recorded_at, entry.entry_id)


def combine(
    reads: Sequence[SourceRead],
    *,
    base: Mapping[str, SourceCheckpoint],
    window_start: float,
    window_end: float,
    sources: int,
) -> ImportResult:
    """Fold every source's read into one pass result.

    Deduplication happens across sources as well as within one: two state
    directories can describe the same plan after a plan directory moves, and an
    entry id is a function of the run rather than of the file it was read from.

    ``base`` is the checkpoint set the pass started from. Checkpoints for
    sources this pass did not see are carried forward rather than dropped, so a
    state directory that disappears and comes back does not re-deliver the
    history it already delivered.
    """
    checkpoints = dict(base)
    entries: list[HistoryEntry] = []
    questions: list[QuestionEntry] = []
    totals = {
        "restarted": 0,
        "unreadable": 0,
        "unresolved": 0,
        "malformed": 0,
        "duplicates": 0,
        "unattributed": 0,
        "expired": 0,
        "undatable": 0,
        "unsupported": 0,
        "oversized": 0,
        "deferred": 0,
    }
    for read in reads:
        checkpoints[read.checkpoint.source_key] = read.checkpoint
        entries.extend(read.entries)
        questions.extend(read.questions)
        totals["restarted"] += 1 if read.restarted else 0
        totals["unreadable"] += 1 if read.unreadable else 0
        totals["unresolved"] += 1 if read.unresolved else 0
        totals["malformed"] += read.malformed
        totals["duplicates"] += read.duplicates
        totals["unattributed"] += read.unattributed
        totals["expired"] += read.expired
        totals["undatable"] += read.undatable
        totals["unsupported"] += read.unsupported
        totals["oversized"] += read.oversized
        totals["deferred"] += read.deferred_bytes

    seen: set[str] = set()
    unique_entries: list[HistoryEntry] = []
    for entry in entries:
        if entry.entry_id in seen:
            totals["duplicates"] += 1
            continue
        seen.add(entry.entry_id)
        unique_entries.append(entry)
    unique_questions: list[QuestionEntry] = []
    for question in questions:
        if question.entry_id in seen:
            totals["duplicates"] += 1
            continue
        seen.add(question.entry_id)
        unique_questions.append(question)
    unique_entries.sort(key=_entry_order)
    unique_questions.sort(key=_question_order)

    warnings: list[str] = []
    for label, count in (
        ("could not be read", totals["unreadable"]),
        ("could not be identified", totals["unresolved"]),
    ):
        if count:
            warnings.append(f"{count} ASL plan(s) {label} during this import")
    if totals["malformed"]:
        warnings.append(f"{totals['malformed']} event record(s) were unreadable")
    if totals["unattributed"]:
        warnings.append(f"{totals['unattributed']} terminal record(s) named no run")
    if totals["oversized"]:
        warnings.append(f"{totals['oversized']} event record(s) were too large to read")
    if totals["deferred"]:
        warnings.append("more history remains to be imported")

    return ImportResult(
        entries=tuple(unique_entries),
        questions=tuple(unique_questions),
        checkpoints=tuple(
            checkpoint
            for _, checkpoint in sorted(checkpoints.items(), key=lambda item: item[0])
        ),
        window_start=window_start,
        window_end=window_end,
        sources=sources,
        restarted_sources=totals["restarted"],
        unreadable_sources=totals["unreadable"],
        unresolved_sources=totals["unresolved"],
        malformed=totals["malformed"],
        duplicates=totals["duplicates"],
        unattributed=totals["unattributed"],
        expired=totals["expired"],
        undatable=totals["undatable"],
        unsupported=totals["unsupported"],
        oversized=totals["oversized"],
        deferred_bytes=totals["deferred"],
        warnings=tuple(warnings[:MAX_WARNINGS]),
    )


# -- Projections ------------------------------------------------------------


@dataclass(frozen=True)
class PlanHistory:
    """One plan's runs in the window, rolled up for a local list."""

    plan_key: str
    project: str
    runs: int
    succeeded: int
    questions: int
    answered: int
    last_recorded_at: float
    outcomes: tuple[tuple[str, int], ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "plan_key": self.plan_key,
            "project": self.project,
            "runs": self.runs,
            "succeeded": self.succeeded,
            "questions": self.questions,
            "answered": self.answered,
            "last_recorded_at": self.last_recorded_at,
            "outcomes": {outcome: count for outcome, count in self.outcomes},
        }


@dataclass(frozen=True)
class HistoryView:
    """The local rendering of one import: the same entries, grouped by plan."""

    entries: tuple[HistoryEntry, ...] = ()
    questions: tuple[QuestionEntry, ...] = ()
    plans: tuple[PlanHistory, ...] = ()
    window_start: float = 0.0
    window_end: float = 0.0
    complete: bool = True
    warnings: tuple[str, ...] = ()

    def outcome_counts(self) -> dict[str, int]:
        """How many runs ended each way, with every outcome named even at zero."""
        counts = dict.fromkeys(HISTORY_OUTCOMES, 0)
        for entry in self.entries:
            counts[entry.outcome] = counts.get(entry.outcome, 0) + 1
        return counts

    def question_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(QUESTION_OUTCOMES, 0)
        for question in self.questions:
            counts[question.outcome] = counts.get(question.outcome, 0) + 1
        return counts

    def to_payload(self) -> dict[str, object]:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "complete": self.complete,
            "entry_count": len(self.entries),
            "question_count": len(self.questions),
            "outcomes": self.outcome_counts(),
            "questions": self.question_counts(),
            "plans": [plan.to_payload() for plan in self.plans],
            "entries": [entry.to_payload() for entry in self.entries],
            "question_entries": [question.to_payload() for question in self.questions],
        }


def build_view(result: ImportResult) -> HistoryView:
    """Group one import's entries for local presentation.

    The entries are the objects :func:`batches` publishes, not copies of them,
    so the cockpit on the Mac and the cockpit on the Pi cannot disagree about
    what a run did.
    """
    plans: dict[str, list[HistoryEntry]] = {}
    for entry in result.entries:
        plans.setdefault(entry.plan_key, []).append(entry)
    questions: dict[str, list[QuestionEntry]] = {}
    for question in result.questions:
        questions.setdefault(question.plan_key, []).append(question)

    rollups: list[PlanHistory] = []
    for key in sorted(plans.keys() | questions.keys()):
        runs = plans.get(key, [])
        asked = questions.get(key, [])
        outcomes: dict[str, int] = {}
        for entry in runs:
            outcomes[entry.outcome] = outcomes.get(entry.outcome, 0) + 1
        moments = [entry.recorded_at for entry in runs]
        moments.extend(question.recorded_at for question in asked)
        rollups.append(
            PlanHistory(
                plan_key=key,
                project=runs[0].project if runs else "",
                runs=len(runs),
                succeeded=sum(1 for entry in runs if entry.succeeded),
                questions=len(asked),
                answered=sum(1 for question in asked if question.answered),
                last_recorded_at=max(moments) if moments else 0.0,
                outcomes=tuple(sorted(outcomes.items())),
            )
        )
    rollups.sort(key=lambda plan: (-plan.last_recorded_at, plan.plan_key))

    return HistoryView(
        entries=result.entries,
        questions=result.questions,
        plans=tuple(rollups),
        window_start=result.window_start,
        window_end=result.window_end,
        complete=result.complete,
        warnings=result.warnings,
    )


@dataclass(frozen=True)
class HistoryBatch:
    """One bounded, idempotent chunk of an import, ready for a transport.

    The batch id is a digest of the entry ids it carries, so re-sending the same
    import produces byte-identical batches rather than a new one the receiver
    has to decide about.
    """

    batch_id: str
    index: int
    generated_at: float
    window_start: float
    window_end: float
    complete: bool
    entries: tuple[HistoryEntry, ...] = ()
    questions: tuple[QuestionEntry, ...] = ()
    more: bool = False
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "batch_id": self.batch_id,
            "generated_at": self.generated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "entry_count": len(self.entries),
            "question_count": len(self.questions),
            "complete": self.complete,
            "entries": [entry.to_payload() for entry in self.entries],
            "questions": [question.to_payload() for question in self.questions],
        }
        if self.more:
            payload["more"] = True
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


def batches(result: ImportResult, *, now: float) -> tuple[HistoryBatch, ...]:
    """Split one import into bounded, idempotent upload batches.

    An import with nothing in it still produces one batch: "the window holds no
    history" and "the connector has not looked" are different claims, and only
    the first is worth knowing.
    """
    size = MAX_HISTORY_ENTRIES_PER_BATCH
    runs = [
        result.entries[index : index + size]
        for index in range(0, len(result.entries), size)
    ]
    asked = [
        result.questions[index : index + size]
        for index in range(0, len(result.questions), size)
    ]
    count = max(len(runs), len(asked), 1)
    built: list[HistoryBatch] = []
    for index in range(count):
        chunk = runs[index] if index < len(runs) else ()
        question_chunk = asked[index] if index < len(asked) else ()
        built.append(
            HistoryBatch(
                batch_id=digest_of(
                    [
                        "history-batch",
                        index,
                        [entry.entry_id for entry in chunk],
                        [question.entry_id for question in question_chunk],
                    ]
                ),
                index=index,
                generated_at=now,
                window_start=result.window_start,
                window_end=result.window_end,
                complete=result.complete,
                entries=tuple(chunk),
                questions=tuple(question_chunk),
                more=index + 1 < count,
                warnings=result.warnings,
            )
        )
    return tuple(built)
