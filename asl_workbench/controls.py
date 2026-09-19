"""What a run control and a mid-run correction mean -- shared by both surfaces.

:mod:`asl_workbench.actions` says what starting a run and answering a question
establish. This module answers the neighbouring question for the things an
operator does to a run that is *already* going: stop it after the current
session, abort the turn it is on, choose the harness its next turn uses, send it
a correction, and read what became of one.

Three things are decided here rather than in either surface, because both had
grown their own copy of each:

* **What may be asked, and under what proof.** A control names an ASL action or
  it names none, and a surface either demands a freshness proof for it or it
  does not. Both are stated as data, so "the browser posts a control without a
  token and the bridge refuses one without a live run" is a *policy a caller
  selects* rather than two implementations that happen to differ.
* **What a refusal is about.** A refused control is refused by the request, by
  ASL's own view of the run, by a durable record that disagrees, by the
  correction journal, or by the machine being unable to answer at all. Those
  five need five different things from the client, and a surface that collapses
  them sends a client back to do the one thing that cannot help.
* **What a correction receipt carries.** Not its wire shape -- an HTTP body and
  a bridge frame are allowed to spell fields differently -- but the set of facts
  that must be in it, so neither surface can quietly answer with less.

Nothing here reads a clock, opens a file, or reaches ASL. It validates the shape
of a request and names what an answer means, which is what lets the Pi forward a
control it cannot itself carry out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# -- Controls ---------------------------------------------------------------

#: The three run controls both surfaces expose. Named by what an operator asks
#: for rather than by the queue record it becomes, because the queue command is
#: an implementation of the request and a remote caller forwards the request.
CONTROL_STOP_AFTER_CURRENT = "stop-after-current"
CONTROL_ABORT = "abort"
CONTROL_SELECT_HARNESS = "select-harness"

CONTROL_KINDS: tuple[str, ...] = (
    CONTROL_STOP_AFTER_CURRENT,
    CONTROL_ABORT,
    CONTROL_SELECT_HARNESS,
)

#: ASL's control-queue command name for each control, mirrored rather than
#: imported: this module must not reach into the worker runtime, and the names
#: are a vocabulary both halves have to agree on anyway.
QUEUE_COMMAND_FOR_CONTROL: Mapping[str, str] = {
    CONTROL_STOP_AFTER_CURRENT: "request-stop-after-current",
    CONTROL_ABORT: "request-abort-turn",
    CONTROL_SELECT_HARNESS: "select-future-harness",
}

#: The ASL intervention action a control validates as, where it has one.
#: ``select-harness`` has none on purpose: it changes what a *future* turn runs
#: under, so there is no live turn for a freshness proof to be about, which is
#: also why the bridge -- which authorizes every mutation against one -- has
#: never exposed it.
ASL_ACTION_FOR_CONTROL: Mapping[str, str] = {
    CONTROL_STOP_AFTER_CURRENT: "run.stop_after_current",
    CONTROL_ABORT: "run.abort",
}

#: Submitting a correction is a control too, and it is the one with an action
#: but no queue command: it goes to the redirect journal rather than to the
#: control queue.
ACTION_REDIRECT = "run.redirect"


def queue_command_for(kind: str) -> str:
    """The control-queue command one control becomes."""
    command = QUEUE_COMMAND_FOR_CONTROL.get(kind)
    if command is None:
        raise ControlRequestError(
            REASON_INVALID_CONTROL,
            f"unsupported control {kind!r}",
            details={"supported": list(CONTROL_KINDS)},
        )
    return command


def asl_action_for(kind: str) -> str | None:
    """The ASL action a control validates as, or ``None`` when it has none."""
    return ASL_ACTION_FOR_CONTROL.get(kind)


# -- How hard a surface checks before queueing -------------------------------

#: Queue it and let the runner be the authority. The control queue is durable
#: and a command for a run that has not started yet is a legitimate thing to
#: leave for it, so a caller with no snapshot to be stale is not made to
#: acquire one. This is what the local browser and the CLI have always done.
AUTHORIZATION_OPEN = "open"

#: Refuse anything that is not about the run this caller was shown: a live run
#: must hold the plan, and the freshness proof must still match, checked against
#: a read taken *immediately* before the mutation rather than against whatever
#: the request was first admitted on. A supervision client acts on a row it
#: rendered, so a request that no longer matches it is a bug to surface rather
#: than an instruction to queue.
AUTHORIZATION_STRICT = "strict"

AUTHORIZATION_POLICIES: tuple[str, ...] = (AUTHORIZATION_OPEN, AUTHORIZATION_STRICT)


def require_authorization(policy: str) -> str:
    """Narrow a caller's authorization policy to the two that exist."""
    if policy not in AUTHORIZATION_POLICIES:
        raise ControlRequestError(
            REASON_INVALID_CONTROL,
            f"unsupported authorization policy {policy!r}",
            details={"supported": list(AUTHORIZATION_POLICIES)},
        )
    return policy


# -- What a refusal is about -------------------------------------------------

#: The body is wrong, and resending it unchanged cannot succeed.
SCOPE_REQUEST = "request"
#: ASL's own answer about the run: a stale proof, an action the run does not
#: permit, a target that moved. Resolved by re-reading the plan, not by editing
#: the body.
SCOPE_INTERVENTION = "intervention"
#: A durable record already says something this request contradicts. Resolved by
#: reconciling against that record.
SCOPE_CONFLICT = "conflict"
#: The correction was refused, or could not be written down. Whether resending
#: can help is what ``retryable`` says.
SCOPE_SUBMISSION = "submission"
#: Nothing truthful can be answered right now -- a journal this machine cannot
#: read is a fact about the machine, and answering as though a plan held no
#: corrections would be the wrong kind of wrong.
SCOPE_UNAVAILABLE = "unavailable"

REFUSAL_SCOPES: tuple[str, ...] = (
    SCOPE_REQUEST,
    SCOPE_INTERVENTION,
    SCOPE_CONFLICT,
    SCOPE_SUBMISSION,
    SCOPE_UNAVAILABLE,
)

#: Where a submission refusal came from. Carried alongside ``retryable``
#: because the two surfaces need different halves of it: one reports whether a
#: client should rewrite or retry, the other maps onto a closed, versioned error
#: enum that distinguishes a bad request from a failed mutation.
SOURCE_ADMISSION = "admission"
SOURCE_JOURNAL = "journal"
SOURCE_IO = "io"

SUBMISSION_SOURCES: tuple[str, ...] = (
    SOURCE_ADMISSION,
    SOURCE_JOURNAL,
    SOURCE_IO,
)

#: Refusal reasons this module mints itself. Everything else travels under
#: ASL's own vocabulary, unrenamed, so an operator reading a Workbench error and
#: an operator reading a CLI error see the same word.
REASON_INVALID_CONTROL = "invalid-control"
REASON_INVALID_HARNESS = "invalid-harness"
REASON_INVALID_REQUEST = "invalid-request"
REASON_EMPTY_TEXT = "empty-text"
REASON_NO_ACTIVE_RUN = "no-active-run"


class ControlRequestError(Exception):
    """A control request refused on its own terms, before anything was read."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


# -- Abort notes -------------------------------------------------------------

#: What an abort is going to mean, decided from the run rather than from the
#: queue: appending always succeeds, so the honest part of the answer is what
#: the runner will do with it. The codes are here and the operator-facing
#: sentences stay in ASL's control CLI, so three surfaces cannot print three
#: wordings of one fact.
ABORT_NOTE_NO_RUNNER = "no-runner"
ABORT_NOTE_WILL_APPLY = "will-apply"
ABORT_NOTE_DEGRADES = "degrades"

ABORT_NOTE_CODES: tuple[str, ...] = (
    ABORT_NOTE_NO_RUNNER,
    ABORT_NOTE_WILL_APPLY,
    ABORT_NOTE_DEGRADES,
)


def abort_note_code(*, runner_present: bool, turn_active: bool) -> str:
    """Which note an abort earns, from the two facts that decide it.

    ``runner_present`` is "a live process holds this plan", not ASL's stricter
    "a run is active": an abort queued for a runner that is mid-shutdown or
    paused still reaches a process, and reporting it as though nothing were
    there would understate what is about to happen.
    """
    if not runner_present:
        return ABORT_NOTE_NO_RUNNER
    return ABORT_NOTE_WILL_APPLY if turn_active else ABORT_NOTE_DEGRADES


# -- Corrections -------------------------------------------------------------

#: How many durable redirect operations one projection may carry. The journal is
#: append-only and unbounded, so every projection needs a ceiling or a
#: long-lived plan eventually produces an answer no client can read. One
#: ceiling, because two surfaces with different windows would disagree about
#: which corrections exist.
MAX_REDIRECT_OPERATIONS = 64

#: Every field a client may name when it says which turn it believes it is
#: correcting. Optional as a whole and load-bearing when supplied: a client that
#: rendered a turn and then waited for a person to finish typing is exactly the
#: one whose turn may have ended in between.
EXPECTED_TARGET_FIELDS: tuple[str, ...] = (
    "run_id",
    "session_id",
    "turn_index",
    "harness",
)

#: The facts a correction receipt must carry, whatever a surface calls them.
#: Named so neither surface can answer with less: an operator whose response was
#: lost needs the identity to ask after, whether anything was persisted, and
#: whether the journal it was read from was whole.
REDIRECT_RECEIPT_FACTS: tuple[str, ...] = (
    "operation_id",
    "plan_dir",
    "run_id",
    "outcome",
    "reason",
    "target",
    "waited_seconds",
    "replayed",
    "operation",
    "journal_complete",
)


def require_correction_text(value: Any) -> str:
    """Narrow a correction to text a worker could act on, or refuse it.

    Whitespace is not a correction. Refused before admission so nothing reaches
    the journal that an operator would later have to ask after.
    """
    if not isinstance(value, str) or not value.strip():
        raise ControlRequestError(
            REASON_EMPTY_TEXT,
            "redirect text must contain non-whitespace content",
        )
    return value


def expected_target_fields(raw: Any, *, label: str) -> dict[str, Any]:
    """Read the shape of a named turn, leaving its validity to ASL's store.

    Shape only: this checks that the object is an object and that
    ``turn_index`` is an integer rather than a bool wearing one's clothes,
    because those are the two errors a JSON body produces on its own. Whether
    the ids are well formed is :mod:`agent_session_loop.redirect_store`'s
    answer, and asking it twice is how two surfaces come to disagree.
    """
    if not isinstance(raw, Mapping):
        raise ControlRequestError(REASON_INVALID_REQUEST, f"{label} must be an object")
    turn_index = raw.get("turn_index")
    if isinstance(turn_index, bool) or not isinstance(turn_index, int):
        raise ControlRequestError(
            REASON_INVALID_REQUEST, f"{label}.turn_index must be an integer"
        )
    fields: dict[str, Any] = {"turn_index": turn_index}
    for name in ("run_id", "session_id"):
        value = raw.get(name)
        if not isinstance(value, str) or not value:
            raise ControlRequestError(
                REASON_INVALID_REQUEST, f"{label}.{name} must be a non-empty string"
            )
        fields[name] = value
    harness = raw.get("harness")
    if harness is not None and not isinstance(harness, str):
        raise ControlRequestError(
            REASON_INVALID_REQUEST, f"{label}.harness must be a string when provided"
        )
    fields["harness"] = harness or None
    return fields


def bounded_wait_seconds(
    raw: Any, *, default: float, maximum: float, label: str
) -> float:
    """Bound how long one request may hold the surface answering it.

    Both surfaces answer a redirect on the thread or loop the caller is holding,
    so a wait a client chooses is a wait other clients pay. The ceiling comes
    from admission rather than being restated: it is the same promise measured
    from the other end.
    """
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ControlRequestError(REASON_INVALID_REQUEST, f"{label} must be a number")
    if raw < 0 or raw > maximum:
        raise ControlRequestError(
            REASON_INVALID_REQUEST,
            f"{label} is outside the permitted range",
            details={"maximum": maximum},
        )
    return float(raw)


def newest_operations(
    operations: Sequence[Any], *, limit: int = MAX_REDIRECT_OPERATIONS
) -> list[Any]:
    """The tail of a projection window, newest last.

    Truncated from the *front* when the journal outgrows the window: the oldest
    corrections on a long-lived plan are the settled ones, and an operator
    asking where a correction got to is asking about a recent one.
    """
    return list(operations)[-limit:]


__all__ = [
    "ABORT_NOTE_CODES",
    "ABORT_NOTE_DEGRADES",
    "ABORT_NOTE_NO_RUNNER",
    "ABORT_NOTE_WILL_APPLY",
    "ACTION_REDIRECT",
    "ASL_ACTION_FOR_CONTROL",
    "AUTHORIZATION_OPEN",
    "AUTHORIZATION_POLICIES",
    "AUTHORIZATION_STRICT",
    "CONTROL_ABORT",
    "CONTROL_KINDS",
    "CONTROL_SELECT_HARNESS",
    "CONTROL_STOP_AFTER_CURRENT",
    "EXPECTED_TARGET_FIELDS",
    "MAX_REDIRECT_OPERATIONS",
    "QUEUE_COMMAND_FOR_CONTROL",
    "REASON_EMPTY_TEXT",
    "REASON_INVALID_CONTROL",
    "REASON_INVALID_HARNESS",
    "REASON_INVALID_REQUEST",
    "REASON_NO_ACTIVE_RUN",
    "REDIRECT_RECEIPT_FACTS",
    "REFUSAL_SCOPES",
    "SCOPE_CONFLICT",
    "SCOPE_INTERVENTION",
    "SCOPE_REQUEST",
    "SCOPE_SUBMISSION",
    "SCOPE_UNAVAILABLE",
    "SOURCE_ADMISSION",
    "SOURCE_IO",
    "SOURCE_JOURNAL",
    "SUBMISSION_SOURCES",
    "ControlRequestError",
    "abort_note_code",
    "asl_action_for",
    "bounded_wait_seconds",
    "expected_target_fields",
    "newest_operations",
    "queue_command_for",
    "require_authorization",
    "require_correction_text",
]
