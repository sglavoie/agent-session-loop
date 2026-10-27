"""What a launch, a reply, and a resume mean -- shared by both surfaces.

:mod:`asl_workbench.operations` says whether a mutation may be *attempted* and
what an interrupted one means. This module says what its *outcome* means, which
is a different question and the one the two Workbench surfaces have been
answering differently:

* ``POST /api/launch`` and ``POST /api/reply`` spawn a process and settle the
  submission the moment ``Popen`` returns.
* The bridge's ``run.resume`` settles the same way, and its poller then marks
  the operation ``consumed`` as soon as the plan is no longer paused.

Only one of those three claims is about the operator's reply. Spelling them
apart is this module's whole job, and the separation is not hypothetical --
the Companion grew its own copy of it precisely because ASL's answer was wrong:

- **A process was created.** A ``Popen`` returned. For a *launch* that is the
  entire promise: an operator asked for a run and a run was started. For a
  reply it is the weakest of the three claims.
- **The plan left the paused state.** A fact about the plan, not about this
  operation. *Any* run starting on that plan leaves it -- one an operator
  started at the Mac a second earlier, a supervisor's retry, a scheduled run --
  so on its own it attributes someone else's work to this reply.
- **A worker consumed the reply.** The only one that means the operator's
  answer was acted on, and the only evidence for it is run-bound: the run
  appends a ``reply_provided`` record naming itself and the origin the text
  arrived by.

So a reply-bearing mutation is ``accepted`` when the process exists and
``consumed`` only when a run says it read the reply. When neither can be
established the answer is "nobody can say", never "it worked".

Nothing here reads a clock, opens a file, or spawns anything. It parses bytes a
provider hands it and returns what those bytes establish, which is what lets the
same interpretation run on the Pi against an imported log and on the Mac against
a live one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .operations import (
    METHOD_LAUNCH,
    METHOD_QUESTION_REPLY,
    METHOD_REPLY,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
    REPLY_BEARING_METHODS,
    require_method,
)
from .timestamps import iso_epoch

# -- Actions and methods ----------------------------------------------------

#: ASL's own action names, as ``intervention`` spells them. Mirrored rather
#: than imported: this module must not reach into the worker runtime, and the
#: names are a vocabulary both halves have to agree on anyway.
ACTION_LAUNCH = "launch"
ACTION_REPLY = "reply"
ACTION_RESUME = "resume"

#: One table, read in both directions, so a surface cannot route a resume
#: through the reply gate by accident. ``resume`` is a *reply that restarts a
#: paused run*, which is why it maps to :data:`METHOD_REPLY` and not to a
#: method of its own -- it contends for the same thing a launch does.
METHOD_FOR_ACTION: Mapping[str, str] = {
    ACTION_LAUNCH: METHOD_LAUNCH,
    ACTION_RESUME: METHOD_REPLY,
    ACTION_REPLY: METHOD_QUESTION_REPLY,
}

ACTION_FOR_METHOD: Mapping[str, str] = {
    method: action for action, method in METHOD_FOR_ACTION.items()
}


def method_for_action(action: str) -> str:
    """The operation method one ASL action admits under."""
    method = METHOD_FOR_ACTION.get(action)
    if method is None:
        raise ValueError(f"no operation method for action {action!r}")
    return method


# -- The launch profile contract --------------------------------------------

#: Every field a launch profile may carry, in the order the payload spells
#: them. Named here rather than in the route that parses them so both surfaces
#: -- and the Pi, which may only ever forward one -- agree on the shape without
#: importing the Mac dataclass that implements it.
LAUNCH_PROFILE_FIELDS: tuple[str, ...] = (
    "cwd",
    "harness",
    "harness_model",
    "effort",
    "timeout",
    "commit_mode",
    "review_harness",
    "review_harness_model",
    "max_review_fix_cycles",
    "unsafe_bypass",
    "allow_not_loop_ready",
    "note",
)

#: The one profile field a registered plan does not get to choose. ``asl
#: prepare`` binds a plan to a prepared workspace root, and a launch that
#: names a different one is refused rather than quietly rebound -- so a
#: surface forwarding a profile is forwarding a *request*, and the binding
#: remains the authority.
PROFILE_FIELD_BOUND_TO_WORKSPACE = "cwd"


def unknown_profile_fields(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Names in a profile payload that no launch profile has, sorted."""
    return tuple(sorted(set(payload) - set(LAUNCH_PROFILE_FIELDS)))


# -- What a launch failure establishes --------------------------------------

#: ``LaunchError.reason``, mirrored. The first four are raised before anything
#: is spawned; the fifth is raised on both sides of the spawn.
LAUNCH_INVALID_PLAN = "invalid-plan"
LAUNCH_INVALID_PROFILE = "invalid-profile"
LAUNCH_EMPTY_REPLY = "empty-reply"
LAUNCH_INVALID_REPLY = "invalid-reply"
LAUNCH_PROCESS_FAILED = "process-failed"

#: Reasons that prove no process was created. ``process-failed`` is
#: deliberately absent: it covers both "the spawn failed" and "the spawn
#: succeeded, the reply could not be written to the child's stdin, and the
#: child was killed". The second has a process that existed, ran, and may have
#: written to the plan before it died.
REFUSING_LAUNCH_REASONS: frozenset[str] = frozenset(
    {
        LAUNCH_INVALID_PLAN,
        LAUNCH_INVALID_PROFILE,
        LAUNCH_EMPTY_REPLY,
        LAUNCH_INVALID_REPLY,
    }
)


def launch_closure(reason: Any, *, spawned: bool = False) -> str:
    """What a failed launch establishes, from ASL's reason for refusing it.

    ``spawned`` is what a local caller knows and a remote one does not: the
    launcher raises ``process-failed`` from two places, and only the second of
    them has a child that existed. A caller that cannot tell leaves it false
    and gets the conservative answer, which is the same one the Companion
    reaches across the wire.
    """
    if reason in REFUSING_LAUNCH_REASONS:
        return OPERATION_REJECTED
    return OPERATION_UNCONFIRMED if spawned else OPERATION_REJECTED


# -- What proves a reply was read -------------------------------------------

#: The event a run appends when it takes an operator reply into a turn, and the
#: origins it records for the two ways one can arrive. ``reply_length`` rides
#: with them; the text never does.
REPLY_PROVIDED_EVENT = "reply_provided"
REPLY_ORIGIN_STDIN = "stdin"
REPLY_ORIGIN_MAILBOX = "mailbox"

#: Which origin settles which method. A reply streamed into a resumed run's
#: stdin and a reply a supervisor wrote into the mailbox are different
#: deliveries, and either one settling the other would credit one operator's
#: answer to the other's operation. Every method in
#: :data:`asl_workbench.operations.REPLY_BEARING_METHODS` has a row here, and
#: nothing else does.
CONSUMING_ORIGIN: Mapping[str, str] = {
    METHOD_REPLY: REPLY_ORIGIN_STDIN,
    METHOD_QUESTION_REPLY: REPLY_ORIGIN_MAILBOX,
}


def consuming_origin(method: str) -> str | None:
    """The reply origin that settles ``method``, or ``None`` if it bears none."""
    return CONSUMING_ORIGIN.get(require_method(method))


#: How much of one event log a single look will read. A live run appends
#: steadily and a look happens on every poll, so the scan is bounded and the
#: mark advances past whatever it has already read. The ceiling is far above a
#: ``reply_provided`` record and far below a harness transcript.
MAX_WITNESS_BYTES_PER_LOOK = 512 * 1024
MAX_WITNESS_RECORD_BYTES = 64 * 1024


@dataclass(frozen=True)
class ConsumptionEvidence:
    """What one look at a plan's event log established about a reply."""

    consumed: bool = False
    #: The run that recorded reading the reply. Carried so a caller can say
    #: which run acted, and so a test can prove it was not the paused one: a
    #: resumed run is a *new* process with a new id, and requiring it to match
    #: the id the operation named would refuse every successful resume.
    run_id: str | None = None
    question_id: str | None = None
    #: The log is not the file that was marked: replaced, compacted, or
    #: truncated since. Distinct from "nothing found yet", because the evidence
    #: that would have settled this operation may have been in the bytes that
    #: went.
    replaced: bool = False
    #: There was no readable log to look at.
    readable: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "consumed": self.consumed,
            "run_id": self.run_id,
            "question_id": self.question_id,
            "replaced": self.replaced,
            "readable": self.readable,
        }


NO_CONSUMPTION = ConsumptionEvidence()


def reply_record(
    line: bytes,
    *,
    origin: str,
    question_id: str | None = None,
    since: float | None = None,
) -> ConsumptionEvidence | None:
    """One event line, read as proof a run took a reply of ``origin``.

    ``since`` is the weaker of the two ways to establish that a record is *this*
    operation's: it compares the record's own ``created_at`` against a moment
    the caller recorded before dispatching. A caller that can hold a byte mark
    across the request should do that instead and leave this unset -- a mark
    cannot be confused by a clock -- but a caller reconciling a row written by
    a process that is gone has only what the row persisted, and the dispatch
    moment is what it persisted.

    Every failure is ``None`` rather than an exception: an event log holds
    records from many ASL versions, and one this reader cannot parse is not
    evidence, which is exactly what ``None`` means to the caller.
    """
    if len(line) > MAX_WITNESS_RECORD_BYTES:
        return None
    if REPLY_PROVIDED_EVENT.encode() not in line:
        # A cheap gate ahead of the parse. The overwhelming majority of records
        # in a real log are turns, and decoding them to discover that is the
        # one cost worth avoiding here.
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("name") != REPLY_PROVIDED_EVENT:
        return None
    if since is not None and not _recorded_after(record.get("created_at"), since):
        # Written before this operation dispatched, so it answers something
        # else. A record whose timestamp cannot be read is treated the same
        # way: unattributable is not attributable-to-us.
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if not isinstance(source, dict) or source.get("origin") != origin:
        # A reply that reached the run some other way is somebody else's
        # delivery.
        return None
    answered = _text(source.get("question_id")) or _text(payload.get("question_id"))
    if question_id is not None and answered != question_id:
        # The operation named a question. A run that read a different one read
        # somebody else's answer, however recently.
        return None
    return ConsumptionEvidence(
        consumed=True,
        run_id=_text(payload.get("run_id")) or _text(source.get("run_id")),
        question_id=answered,
    )


def scan_replies(
    chunk: bytes,
    *,
    origin: str,
    question_id: str | None = None,
    since: float | None = None,
) -> tuple[ConsumptionEvidence, int]:
    """Read whole records out of ``chunk`` and report the first that settles.

    Returns the evidence and how many bytes of ``chunk`` were consumed, so a
    caller following a growing log advances its mark by the second and never
    past a record still being written. A partial final line is left behind
    unless it is already longer than anything this reader will parse, in which
    case stepping over it is the only way past a record it cannot use.
    """
    end = chunk.rfind(b"\n")
    if end < 0:
        # Not one whole record yet. The mark stays where it is unless the
        # partial line is already past anything this reader will parse.
        stride = len(chunk) if len(chunk) > MAX_WITNESS_RECORD_BYTES else 0
        return NO_CONSUMPTION, stride
    for line in chunk[: end + 1].splitlines():
        evidence = reply_record(
            line, origin=origin, question_id=question_id, since=since
        )
        if evidence is not None:
            return evidence, end + 1
    return NO_CONSUMPTION, end + 1


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _recorded_after(created_at: Any, since: float) -> bool:
    """Whether a record's own timestamp places it after ``since``.

    Unreadable answers ``False``. The direction matters: a record nobody can
    place in time must not settle an operation, because settling is the claim
    that cannot be taken back.
    """
    moment = iso_epoch(created_at)
    return moment is not None and moment >= since


__all__ = [
    "ACTION_FOR_METHOD",
    "ACTION_LAUNCH",
    "ACTION_REPLY",
    "ACTION_RESUME",
    "CONSUMING_ORIGIN",
    "LAUNCH_EMPTY_REPLY",
    "LAUNCH_INVALID_PLAN",
    "LAUNCH_INVALID_PROFILE",
    "LAUNCH_INVALID_REPLY",
    "LAUNCH_PROCESS_FAILED",
    "LAUNCH_PROFILE_FIELDS",
    "MAX_WITNESS_BYTES_PER_LOOK",
    "MAX_WITNESS_RECORD_BYTES",
    "METHOD_FOR_ACTION",
    "NO_CONSUMPTION",
    "PROFILE_FIELD_BOUND_TO_WORKSPACE",
    "REFUSING_LAUNCH_REASONS",
    # Re-exported so a surface routing an action reads one vocabulary: which
    # methods bear a reply is decided in `operations`, what proves one was read
    # is decided here.
    "REPLY_BEARING_METHODS",
    "REPLY_ORIGIN_MAILBOX",
    "REPLY_ORIGIN_STDIN",
    "REPLY_PROVIDED_EVENT",
    "ConsumptionEvidence",
    "consuming_origin",
    "launch_closure",
    "method_for_action",
    "reply_record",
    "scan_replies",
    "unknown_profile_fields",
]
