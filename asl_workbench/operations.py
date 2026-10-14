"""The portable half of the Workbench's mutation admission.

:mod:`asl_workbench.catalog` says what may be published and
:mod:`asl_workbench.details` says what may be fetched. This module says what
may be *done*: which mutations exist, what names one of them, which of them may
proceed right now, what a journal row about one may hold, and -- when a process
dies in the middle -- what the honest answer about it is afterwards.

The failure it exists to prevent is the one the program's risk register names
first: *a lost response causes a repeated action*. An operator taps "Reply",
the connection drops before the answer arrives, the browser resends, and a
second run starts. Three rules stand between that and a repeat, and each is a
refusal decided here rather than at a route:

* **Identity is bound to the action and its target.** An operation id is not a
  bare string a client picks; :func:`operation_fingerprint` digests the method
  and the target alongside it, and :func:`admit` refuses an id whose recorded
  fingerprint disagrees. A retry of the same request replays; an id reused for
  a *different* action is a producer bug and is refused rather than silently
  overwriting a mutation that may already have run. This is the rule
  ``redirect_store_validation.replay_conflict`` already applies to corrections, stated
  once for every mutation.

* **One outstanding mutation per contended scope.** ``unconfirmed`` counts as
  outstanding on purpose: an uncertain mutation is exactly the case where a
  second one must not be started. The two spawning methods share one scope
  because they both start a process; every other method contends only with a
  repeat of itself.

* **Nothing is ever repeated automatically.** :func:`attemptable` admits only
  ``pending`` -- intent recorded, nothing tried -- and a ``pending`` row from a
  process that is gone is reconciled to ``expired``, not retried. A
  ``dispatched`` row whose process died becomes ``unconfirmed``: a mutation may
  have run and nobody can say. That is a worse-looking answer and a truer one.

Two things this module deliberately does not do. It does not decide whether a
mutation is *permitted* -- ASL does, through the plan lock and its own
refusals, and admission here is a gate in front of that authority rather than a
replacement for it. And it touches no filesystem: the durable journal, the
cross-process lock, and the reconciliation evidence all live in
``agent_session_loop.workbench_operations``, so these rules load on the Pi
unchanged and a remote connector refuses a mutation for the same reason and
with the same word as the local server.

The status vocabulary, the transition table, the detail codes, the forbidden
keys, and the export projection are byte-compatible with the Companion's
``protocol.py``. That is not imitation: the Pi already holds operation rows
written under those names, and a unified application that renamed them would
orphan every row an operator can still see.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# -- The mutation vocabulary ------------------------------------------------

#: The plan-scoped mutations this admission gate covers, one per mutating
#: Workbench route that acts on a single plan, plus the Companion's
#: question-reply. A method is a word from a closed set rather than a route
#: string, so a transport carrying a request carries something a later version
#: cannot rename out from under a stored row.
METHOD_LAUNCH = "plan.launch"
METHOD_REPLY = "plan.reply"
METHOD_QUESTION_REPLY = "question.reply"
METHOD_STOP_AFTER_CURRENT = "plan.stop-after-current"
METHOD_ABORT = "plan.abort"
METHOD_SELECT_HARNESS = "plan.select-harness"
METHOD_REDIRECT = "plan.redirect"
METHOD_UNLOCK = "plan.unlock"
METHOD_PLAN_ARCHIVE = "plan.archive"
METHOD_ARCHIVE_RESTORE = "archive.restore"
METHOD_ARCHIVE_DELETE = "archive.delete"
METHOD_ARCHIVE_SETTINGS = "archive.settings"

OPERATION_METHODS: tuple[str, ...] = (
    METHOD_LAUNCH,
    METHOD_REPLY,
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_ABORT,
    METHOD_SELECT_HARNESS,
    METHOD_REDIRECT,
    METHOD_UNLOCK,
    METHOD_PLAN_ARCHIVE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_SETTINGS,
)

#: Mutations that are *not* plan-scoped, named here rather than left out, so a
#: later session adding one to the journal has to decide where its exclusion
#: scope lives rather than discovering the question. The registry already has
#: its own serialized transaction (``workbench_registry.mutate_registry`` holds a flock
#: on ``workbench.json.lock``), and the archive collection is a separate
#: boundary with its own confirmation contract.
UNSCOPED_MUTATIONS: tuple[str, ...] = (
    "registry.pin",
    "registry.unpin",
    "plans.archive",
)

#: The Companion's bridge method names, mapped onto this vocabulary. A command
#: arriving from the Pi admits through the same gate as a local click, which is
#: the whole point of a *shared* admission: ``run.resume`` is a reply that
#: restarts a paused run, which is what ``POST /api/reply`` does.
REMOTE_METHOD_ALIASES: Mapping[str, str] = {
    "run.resume": METHOD_REPLY,
    "question.reply": METHOD_QUESTION_REPLY,
}

#: The two methods that start a process. They share one exclusion scope
#: because the thing they contend for is the same thing -- a run holding the
#: plan -- and admitting one while the other is outstanding is how two runs
#: race for a lock only one of them can win.
RUN_STARTING_METHODS: frozenset[str] = frozenset({METHOD_LAUNCH, METHOD_REPLY})

SCOPE_RUN_START = "run-start"

#: The methods that carry an operator's words. What they promise is that those
#: words were *read*, which is a strictly stronger claim than the one a launch
#: makes -- a launch promises a process, and a process is what starting it
#: produces. So these two reach ``consumed`` only on run-bound evidence that a
#: run took the reply into a turn; see :mod:`asl_workbench.actions`, which owns
#: what that evidence looks like.
REPLY_BEARING_METHODS: frozenset[str] = frozenset({METHOD_REPLY, METHOD_QUESTION_REPLY})

#: The longest operation id this gate will store, matching the ceiling the
#: redirect journal puts on the same kind of client-chosen key. An idempotency
#: key is an identifier a client picks, and an unbounded one is an unbounded
#: allocation on this side of the wire.
MAX_OPERATION_ID_CHARS = 64

# -- Statuses ---------------------------------------------------------------

#: Admitted, recorded, and not yet attempted. A row in this state names a
#: mutation that has demonstrably not happened, which is why an interruption
#: here settles as ``expired`` rather than ``unconfirmed``.
OPERATION_PENDING = "pending"

#: Written before the mutation is attempted, so an interrupted process leaves
#: dispatch intent behind instead of nothing at all.
OPERATION_DISPATCHED = "dispatched"

#: Withdrawn while still pending -- the only window in which withdrawal means
#: anything, because after dispatch nothing can promise the mutation did not
#: run.
OPERATION_CANCELLED = "cancelled"

#: The five outcomes ASL itself settles, carried through unchanged.
OPERATION_ACCEPTED = "accepted"
OPERATION_CONSUMED = "consumed"
OPERATION_REJECTED = "rejected"
OPERATION_EXPIRED = "expired"
OPERATION_SUPERSEDED = "superseded"

#: The outcome could not be established from a receipt or from ASL's own
#: records. Displayed as uncertain and never replayed.
OPERATION_UNCONFIRMED = "unconfirmed"

ASL_SETTLED_STATUSES: tuple[str, ...] = (
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_REJECTED,
    OPERATION_EXPIRED,
    OPERATION_SUPERSEDED,
)

OPERATION_STATUSES: tuple[str, ...] = (
    OPERATION_PENDING,
    OPERATION_DISPATCHED,
    *ASL_SETTLED_STATUSES,
    OPERATION_UNCONFIRMED,
    OPERATION_CANCELLED,
)

#: The statuses that still count against the one-outstanding-mutation-per-scope
#: rule. ``unconfirmed`` is in here on purpose.
OPERATION_OUTSTANDING_STATUSES: frozenset[str] = frozenset(
    {
        OPERATION_PENDING,
        OPERATION_DISPATCHED,
        OPERATION_ACCEPTED,
        OPERATION_UNCONFIRMED,
    }
)

#: ``unconfirmed`` is absent: it is terminal for display but still open to late
#: evidence, which is what distinguishes it from a settled outcome.
OPERATION_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        OPERATION_CONSUMED,
        OPERATION_REJECTED,
        OPERATION_EXPIRED,
        OPERATION_SUPERSEDED,
        OPERATION_CANCELLED,
    }
)

#: A state that permits no transition out of it is a state no replay can leave.
OPERATION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    # ``expired`` is reachable without ever being dispatched: the process that
    # admitted the operation died before attempting it, so the mutation
    # demonstrably never ran.
    OPERATION_PENDING: frozenset(
        {OPERATION_DISPATCHED, OPERATION_CANCELLED, OPERATION_EXPIRED}
    ),
    # ``expired`` is reachable from here too, in one narrow way: the deadline
    # was already past when the attempt was about to start, so nothing ran.
    OPERATION_DISPATCHED: frozenset(
        {
            OPERATION_ACCEPTED,
            # Synchronous Mac actions complete in the delivery attempt itself.
            OPERATION_CONSUMED,
            OPERATION_REJECTED,
            OPERATION_EXPIRED,
            OPERATION_UNCONFIRMED,
        }
    ),
    OPERATION_ACCEPTED: frozenset(
        {
            OPERATION_CONSUMED,
            OPERATION_REJECTED,
            OPERATION_EXPIRED,
            OPERATION_SUPERSEDED,
            OPERATION_UNCONFIRMED,
        }
    ),
    # Late reconciliation from a receipt or from ASL's own records is the only
    # way out, and it never goes back to ``dispatched`` or ``accepted``.
    OPERATION_UNCONFIRMED: frozenset(
        {OPERATION_CONSUMED, OPERATION_REJECTED, OPERATION_SUPERSEDED}
    ),
    OPERATION_CONSUMED: frozenset(),
    OPERATION_REJECTED: frozenset(),
    OPERATION_EXPIRED: frozenset(),
    OPERATION_SUPERSEDED: frozenset(),
    OPERATION_CANCELLED: frozenset(),
}

#: Why an operation reached the state it is in, as a code rather than a
#: sentence: a sentence composed on the Mac is a sentence the Pi would have to
#: carry without being able to check it.
DETAIL_DEADLINE_PASSED = "deadline-passed"
DETAIL_OWNER_CANCELLED = "owner-cancelled"
DETAIL_CONNECTOR_RESTARTED = "connector-restarted"
DETAIL_NO_RESULT = "no-result"
DETAIL_BRIDGE_UNAVAILABLE = "bridge-unavailable"
DETAIL_ACTION_TOKEN_REFUSED = "action-token-refused"
DETAIL_RECONCILED = "reconciled"
DETAIL_LAUNCH_REFUSED = "launch-refused"

#: An operation nobody could account for, replaced by a later submission once
#: its target was demonstrably idle. Deliberately not a claim that the old
#: mutation did not run -- ``superseded`` says it was replaced, which is the
#: only thing anyone can honestly say about it.
DETAIL_REPLACED_UNCERTAIN = "replaced-uncertain"

#: A run recorded reading the reply this operation carried. The only detail
#: code that settles a reply-bearing mutation as ``consumed``, and the reason
#: "the plan is no longer paused" no longer does: that one is a fact about the
#: plan, this one is a fact about the operator's answer.
DETAIL_REPLY_WITNESSED = "reply-witnessed"

OPERATION_DETAIL_CODES: tuple[str, ...] = (
    DETAIL_DEADLINE_PASSED,
    DETAIL_OWNER_CANCELLED,
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_NO_RESULT,
    DETAIL_BRIDGE_UNAVAILABLE,
    DETAIL_ACTION_TOKEN_REFUSED,
    DETAIL_RECONCILED,
    DETAIL_LAUNCH_REFUSED,
    DETAIL_REPLACED_UNCERTAIN,
    DETAIL_REPLY_WITNESSED,
)

# -- Privacy ----------------------------------------------------------------

#: What an operation journal row may hold, on either end. Written out rather
#: than derived from a request shape, so widening a request cannot widen what
#: is kept. Two fields are conspicuously absent and must stay absent: the reply
#: text, and the action token that would let it be sent again.
#:
#: Four names extend the Companion's list, and each is an identifier rather
#: than content. ``fingerprint`` and ``scope`` are what bind an id to its
#: action and its contended resource. ``pid`` and ``hostname`` are what let a
#: restarted process tell "the mutation I recorded is still being carried out"
#: from "its owner is gone" -- the distinction the in-memory launch table could
#: not survive, and the reason a restart no longer has to answer "unknown" for
#: an operation whose process is demonstrably alive.
OPERATION_PERSIST_FIELDS: frozenset[str] = frozenset(
    {
        "operation_id",
        "command_id",
        "method",
        "status",
        "plan_key",
        "run_key",
        "run_id",
        "question_id",
        "requested_at",
        "deliver_by",
        "dispatched_at",
        "settled_at",
        "detail_code",
        "fingerprint",
        "scope",
        "pid",
        "hostname",
    }
)

#: What a dashboard may read back about an operation. ``plan_key`` stays
#: behind: it is the Mac's name for a directory on the Mac. So do ``pid`` and
#: ``hostname``, which name a process table no remote cockpit can consult.
OPERATION_EXPORT_FIELDS: frozenset[str] = frozenset(
    {
        "operation_id",
        "command_id",
        "method",
        "status",
        "run_key",
        "run_id",
        "question_id",
        "requested_at",
        "deliver_by",
        "dispatched_at",
        "settled_at",
        "detail_code",
    }
)

#: Any key here appearing anywhere in a record bound for storage or export is a
#: leak, whatever the key happens to contain. Names rather than values, because
#: by the time a value is recognisable as prose it has already been written.
FORBIDDEN_PERSISTED_KEYS: frozenset[str] = frozenset(
    {
        "answer",
        "body",
        "guidance",
        "message",
        "note",
        "pause_details",
        "pause_message",
        "prompt",
        "prose",
        "question_body",
        "reply",
        "reply_body",
        "reply_text",
        "text",
        "transcript",
        "turns",
    }
)

# -- Refusals ---------------------------------------------------------------

REASON_INVALID_METHOD = "invalid-operation-method"
REASON_INVALID_OPERATION_ID = "invalid-operation-id"
REASON_INVALID_STATUS = "invalid-operation-status"
REASON_INVALID_TRANSITION = "invalid-operation-transition"
REASON_BODY_LEAK = "operation-body-leak"
REASON_UNKNOWN_OPERATION = "unknown-operation"
REASON_OPERATION_CONFLICT = "operation-conflict"
REASON_OPERATION_OUTSTANDING = "operation-outstanding"
REASON_OPERATION_UNCERTAIN = "operation-uncertain"

_STATUS_BY_REASON: Mapping[str, int] = {
    REASON_INVALID_METHOD: 400,
    REASON_INVALID_OPERATION_ID: 400,
    REASON_INVALID_STATUS: 400,
    REASON_INVALID_TRANSITION: 409,
    REASON_BODY_LEAK: 500,
    REASON_UNKNOWN_OPERATION: 404,
    REASON_OPERATION_CONFLICT: 409,
    REASON_OPERATION_OUTSTANDING: 409,
    REASON_OPERATION_UNCERTAIN: 409,
}

#: Which half of an operation's identity a reused id disagreed about. Reported
#: rather than collapsed into "conflict", so an operator is told what to
#: reconcile.
CONFLICT_METHOD = "method"
CONFLICT_TARGET = "target"


class OperationError(Exception):
    """A mutation refused before anything was attempted.

    Carries the status an HTTP surface answers with, so a refusal decided in
    this module is the refusal a client sees whether it reached the local
    server or the remote connector.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = (
            status if status is not None else _STATUS_BY_REASON.get(reason, 400)
        )
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": self.reason, "error": self.message}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


# -- Identity ---------------------------------------------------------------


@dataclass(frozen=True)
class OperationTarget:
    """What a mutation acts on, as identifiers rather than paths or prose.

    ``plan_key`` is the digest :func:`asl_workbench.catalog.plan_key` mints, so
    a target names a plan the same way a published catalog entry does.
    ``selector`` narrows it where a method needs narrowing -- the question id a
    reply answers, the harness a selection picks, the turn a correction aims
    at -- and is empty for a method that acts on the plan as a whole.

    It is part of the fingerprint, so an operation id reused against a
    different question or a different turn is a conflict rather than a replay.
    """

    plan_key: str
    selector: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.plan_key, str) or not self.plan_key:
            raise OperationError(
                REASON_INVALID_OPERATION_ID, "an operation target needs a plan_key"
            )

    def to_payload(self) -> dict[str, Any]:
        return {"plan_key": self.plan_key, "selector": self.selector}


def operation_fingerprint(method: str, target: OperationTarget) -> str:
    """The digest that binds an operation id to the action and its target.

    Short and stable: a canonical JSON rendering of the method and the target,
    digested. It names the request without carrying it, which is what lets a
    journal row refuse a conflicting reuse while holding nothing an operator
    typed.
    """
    canonical = json.dumps(
        {
            "method": require_method(method),
            "plan_key": target.plan_key,
            "selector": target.selector,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def require_method(method: str) -> str:
    """Return ``method``, resolving a remote alias, or refuse an unknown one."""
    resolved = REMOTE_METHOD_ALIASES.get(method, method)
    if resolved not in OPERATION_METHODS:
        raise OperationError(
            REASON_INVALID_METHOD,
            f"unknown operation method: {method!r}",
            details={"method": method},
        )
    return resolved


def require_operation_id(operation_id: str) -> str:
    """Return ``operation_id`` when it is a bounded, non-empty identifier."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise OperationError(
            REASON_INVALID_OPERATION_ID, "operation_id must be a non-empty string"
        )
    if len(operation_id) > MAX_OPERATION_ID_CHARS:
        raise OperationError(
            REASON_INVALID_OPERATION_ID,
            f"operation_id exceeds {MAX_OPERATION_ID_CHARS} characters",
            details={"length": len(operation_id)},
        )
    if any(character.isspace() for character in operation_id):
        raise OperationError(
            REASON_INVALID_OPERATION_ID, "operation_id must not contain whitespace"
        )
    return operation_id


def method_scope(method: str) -> str:
    """The resource this method contends for.

    The two spawning methods share :data:`SCOPE_RUN_START`; every other method
    is its own scope, so a queued stop does not refuse an abort while still
    refusing a second identical stop.
    """
    resolved = require_method(method)
    return SCOPE_RUN_START if resolved in RUN_STARTING_METHODS else resolved


# -- Transitions ------------------------------------------------------------


def next_status(current: str, proposed: str) -> str:
    """Return ``proposed`` when the transition is legal, otherwise refuse it."""
    if current not in OPERATION_STATUSES:
        raise OperationError(REASON_INVALID_STATUS, f"unknown status: {current!r}")
    if proposed not in OPERATION_STATUSES:
        raise OperationError(REASON_INVALID_STATUS, f"unknown status: {proposed!r}")
    if proposed not in OPERATION_TRANSITIONS[current]:
        raise OperationError(
            REASON_INVALID_TRANSITION,
            f"{current} cannot become {proposed}",
            details={"status": current, "proposed": proposed},
        )
    return proposed


def attemptable(status: str) -> bool:
    """Whether the mutation behind this row may be carried out.

    Exactly one state qualifies, and it is the one that has never been tried.
    There is no state from which an attempt may be *repeated*: on this side of
    the wire the mutation is local, so once dispatch has been recorded nothing
    can establish that it did not happen, and a second attempt would be the
    repeat this whole module exists to prevent.
    """
    return status == OPERATION_PENDING


def outstanding(status: str) -> bool:
    """Whether this state still blocks a second mutation in the same scope."""
    return status in OPERATION_OUTSTANDING_STATUSES


def terminal(status: str) -> bool:
    """Whether this state is closed to further evidence."""
    return status in OPERATION_TERMINAL_STATUSES


# -- Projection -------------------------------------------------------------


def assert_body_free(
    document: Any,
    *,
    where: str = "operation record",
    allow: frozenset[str] = frozenset(),
) -> None:
    """Refuse any nested key that could carry question or reply prose.

    ``allow`` exists for the one frame whose whole purpose is a body. Every
    other caller passes nothing, so the check is a property of the document
    rather than of the caller's intentions.
    """
    pending: list[Any] = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            leaked = sorted((FORBIDDEN_PERSISTED_KEYS - allow) & set(node))
            if leaked:
                raise OperationError(
                    REASON_BODY_LEAK,
                    f"{where} carries body fields: {', '.join(leaked)}",
                    details={"fields": leaked},
                )
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def persist_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one operation onto the fields a journal is allowed to store."""
    return _project(record, OPERATION_PERSIST_FIELDS, "persisted operation")


def export_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one operation onto the fields a cockpit may read back."""
    projected = _project(record, OPERATION_EXPORT_FIELDS, "exported operation")
    if projected.get("method") in (
        METHOD_LAUNCH,
        METHOD_PLAN_ARCHIVE,
        METHOD_ARCHIVE_RESTORE,
        METHOD_ARCHIVE_DELETE,
        METHOD_ARCHIVE_SETTINGS,
    ):
        # The remote service's legacy journal requires a non-null run_key. For
        # these plan-scoped operations it stores the real plan key there; that
        # storage adapter must never be presented as a run identity.
        projected.pop("run_key", None)
    return projected


def _project(
    record: Mapping[str, Any], allowed: frozenset[str], where: str
) -> dict[str, Any]:
    projected = {key: value for key, value in record.items() if key in allowed}
    # The allowlist already removed every declared body field. This catches the
    # undeclared ones -- a nested object smuggling prose under an allowed key.
    assert_body_free(projected, where=where)
    return projected


def new_record(
    *,
    operation_id: str,
    method: str,
    target: OperationTarget,
    now: float,
    deliver_by: float | None = None,
    command_id: str | None = None,
    run_key: str | None = None,
    run_id: str | None = None,
    question_id: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Build the ``pending`` row for one freshly admitted mutation.

    ``pending`` rather than ``dispatched``: admission and attempt are separate
    steps so that the window between them -- the interruption that leaves a
    record of something that never happened -- is a window a test can stand
    inside and an operator can be told about precisely.
    """
    resolved = require_method(method)
    return persist_operation(
        {
            "operation_id": require_operation_id(operation_id),
            "fingerprint": operation_fingerprint(resolved, target),
            "method": resolved,
            "scope": method_scope(resolved),
            "status": OPERATION_PENDING,
            "plan_key": target.plan_key,
            "run_key": run_key,
            "run_id": run_id,
            "question_id": question_id,
            "command_id": command_id,
            "requested_at": now,
            "deliver_by": deliver_by,
            "dispatched_at": None,
            "settled_at": None,
            "detail_code": None,
            "pid": pid,
            "hostname": hostname,
        }
    )


def advance_record(
    record: Mapping[str, Any],
    status: str,
    *,
    now: float,
    detail_code: str | None = None,
    run_id: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Return ``record`` moved to ``status``, or refuse an illegal move.

    ``dispatched`` stamps ``dispatched_at`` and every settled status stamps
    ``settled_at``; ``unconfirmed`` does neither on purpose, because it is not
    settled -- it is the absence of an answer, still open to late evidence.
    """
    current = str(record.get("status", ""))
    updated = dict(record)
    if current != status:
        updated["status"] = next_status(current, status)
    if status == OPERATION_DISPATCHED:
        updated["dispatched_at"] = now
    if status in ASL_SETTLED_STATUSES or status == OPERATION_CANCELLED:
        updated["settled_at"] = now
    if detail_code is not None:
        updated["detail_code"] = detail_code
    if run_id is not None:
        updated["run_id"] = run_id
    if pid is not None:
        updated["pid"] = pid
    if hostname is not None:
        updated["hostname"] = hostname
    return persist_operation(updated)


# -- Admission --------------------------------------------------------------

DECISION_ADMIT = "admit"
DECISION_REPLAY = "replay"
DECISION_REFUSE = "refuse"


@dataclass(frozen=True)
class Admission:
    """What may happen to one submitted mutation, and why.

    Exactly three outcomes, and the middle one is the reason this type exists:
    a *replay* is not a second attempt that happened to be deduplicated, it is
    the first attempt's own record handed back. A caller that treats
    :attr:`admitted` and :attr:`replayed` alike will start a second run.
    """

    decision: str
    operation_id: str
    method: str
    scope: str
    fingerprint: str
    record: dict[str, Any] | None = None
    reason: str | None = None
    conflict: str | None = None
    message: str = ""

    @property
    def admitted(self) -> bool:
        return self.decision == DECISION_ADMIT

    @property
    def replayed(self) -> bool:
        return self.decision == DECISION_REPLAY

    @property
    def refused(self) -> bool:
        return self.decision == DECISION_REFUSE

    def error(self) -> OperationError:
        """The refusal this admission carries, as the exception to raise."""
        if not self.refused or self.reason is None:
            raise OperationError(
                REASON_INVALID_STATUS,
                "admission was not a refusal",
                details={"decision": self.decision},
            )
        details: dict[str, Any] = {
            "operation_id": self.operation_id,
            "method": self.method,
            "scope": self.scope,
        }
        if self.conflict is not None:
            details["conflict"] = self.conflict
        if self.record is not None:
            details["operation"] = export_operation(self.record)
        return OperationError(self.reason, self.message, details=details)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "decision": self.decision,
            "operation_id": self.operation_id,
            "method": self.method,
            "scope": self.scope,
            "fingerprint": self.fingerprint,
        }
        if self.record is not None:
            payload["operation"] = export_operation(self.record)
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.conflict is not None:
            payload["conflict"] = self.conflict
        if self.message:
            payload["message"] = self.message
        return payload


def conflicting_reuse(
    recorded: Mapping[str, Any], *, method: str, target: OperationTarget
) -> str | None:
    """Name what a resubmission changed about an operation already recorded.

    The comparison is on identity, never on words: the fingerprint names the
    action and its target together, and the method is compared separately only
    so the answer can say which half disagrees. Returns ``None`` when the
    resubmission *is* the recorded operation.
    """
    resolved = require_method(method)
    if str(recorded.get("method", "")) != resolved:
        return CONFLICT_METHOD
    if str(recorded.get("fingerprint", "")) != operation_fingerprint(resolved, target):
        return CONFLICT_TARGET
    return None


def admit(
    records: Sequence[Mapping[str, Any]],
    *,
    operation_id: str,
    method: str,
    target: OperationTarget,
    now: float,
    deliver_by: float | None = None,
    command_id: str | None = None,
    run_key: str | None = None,
    question_id: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> Admission:
    """Decide what happens to one submitted mutation, given what is recorded.

    Pure, and deliberately so: the caller holds the cross-process lock, reads
    the journal under it, calls this, and writes the result under the same
    lock. Deciding outside that lock would validate against a view another
    producer has already invalidated, which is the whole failure idempotent
    submission has to survive.
    """
    resolved = require_method(method)
    identifier = require_operation_id(operation_id)
    scope = method_scope(resolved)
    fingerprint = operation_fingerprint(resolved, target)

    for recorded in records:
        if str(recorded.get("operation_id", "")) != identifier:
            continue
        conflict = conflicting_reuse(recorded, method=resolved, target=target)
        if conflict is None:
            return Admission(
                decision=DECISION_REPLAY,
                operation_id=identifier,
                method=resolved,
                scope=scope,
                fingerprint=fingerprint,
                record=dict(recorded),
            )
        return Admission(
            decision=DECISION_REFUSE,
            operation_id=identifier,
            method=resolved,
            scope=scope,
            fingerprint=fingerprint,
            record=dict(recorded),
            reason=REASON_OPERATION_CONFLICT,
            conflict=conflict,
            message=(
                f"operation {identifier!r} already names a different "
                f"{conflict}; it cannot be reused for this request"
            ),
        )

    for recorded in records:
        status = str(recorded.get("status", ""))
        if str(recorded.get("scope", "")) != scope or not outstanding(status):
            continue
        if status == OPERATION_UNCONFIRMED:
            return Admission(
                decision=DECISION_REFUSE,
                operation_id=identifier,
                method=resolved,
                scope=scope,
                fingerprint=fingerprint,
                record=dict(recorded),
                reason=REASON_OPERATION_UNCERTAIN,
                message=(
                    "an earlier mutation on this plan has an outcome nobody "
                    "can establish; resolve it before starting another"
                ),
            )
        return Admission(
            decision=DECISION_REFUSE,
            operation_id=identifier,
            method=resolved,
            scope=scope,
            fingerprint=fingerprint,
            record=dict(recorded),
            reason=REASON_OPERATION_OUTSTANDING,
            message=(
                f"another {scope} operation on this plan is still "
                f"{status}: {recorded.get('operation_id')}"
            ),
        )

    return Admission(
        decision=DECISION_ADMIT,
        operation_id=identifier,
        method=resolved,
        scope=scope,
        fingerprint=fingerprint,
        record=new_record(
            operation_id=identifier,
            method=resolved,
            target=target,
            now=now,
            deliver_by=deliver_by,
            command_id=command_id,
            run_key=run_key,
            question_id=question_id,
            pid=pid,
            hostname=hostname,
        ),
    )


# -- Reconciliation ---------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """What the authorities say about one operation's target, right now.

    Assembled by the provider from ASL's own records -- the plan lock, the run
    state, a receipt a dispatcher returned -- because ASL is the authority on
    what actually happened and this module is not. Every field defaults to the
    weakest claim, so evidence nobody gathered can only ever produce an
    uncertain answer rather than a confident wrong one.
    """

    owner_alive: bool = False
    run_active: bool = False
    run_id: str | None = None
    receipt_status: str | None = None
    receipt_detail_code: str | None = None
    #: A run recorded reading the reply this operation carried. Gathered from
    #: the plan's own event log by the provider, because that log is the only
    #: place the claim exists -- the plan lock says a run is there, and a run
    #: being there is not the same as an operator's words having been read.
    reply_consumed: bool = False
    #: Which run read it. A resumed run is a *new* process with a new id, so
    #: this is reported rather than matched against the id the operation named:
    #: requiring a match would refuse every successful resume.
    consuming_run_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "owner_alive": self.owner_alive,
            "run_active": self.run_active,
            "run_id": self.run_id,
            "receipt_status": self.receipt_status,
            "receipt_detail_code": self.receipt_detail_code,
            "reply_consumed": self.reply_consumed,
            "consuming_run_id": self.consuming_run_id,
        }


NO_EVIDENCE = Evidence()


@dataclass(frozen=True)
class Reconciliation:
    """The status an open operation should be moved to, and why."""

    status: str
    detail_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"status": self.status, "detail_code": self.detail_code}


def reconcile(
    record: Mapping[str, Any],
    evidence: Evidence = NO_EVIDENCE,
    *,
    now: float,
    superseding: bool = False,
) -> Reconciliation | None:
    """What to do about one open operation, or ``None`` to leave it alone.

    One step at a time, so every move is a legal transition on its own and a
    caller that stops after the first one has still written something true. A
    caller that wants the settled end state applies this until it returns
    ``None``.

    The rules, in the order they are applied:

    * A receipt wins. ASL is the authority on what happened, so a status it
      settled is carried through unchanged when the transition is legal.
    * An operation whose owning process is still alive is left alone. That
      process is mid-attempt, and settling under it would publish an answer
      about work still in progress. A *witnessed* reply is the one exception,
      because it is not an inference about the owner's progress: the run
      recorded reading the operator's words, which is the whole of what the
      operation promised, and the owner is in no position to contradict it.
      Without the exception a long-lived surface -- a Workbench server that
      answers a question and then goes back to serving pages -- would leave
      every reply it delivered sitting at ``accepted`` for its own lifetime.
    * ``pending`` from a dead owner settles as ``expired``: intent was recorded
      and nothing was attempted, so the mutation demonstrably did not run.
      This is the good case, and it is why dispatch is a separate write.
    * ``dispatched`` from a dead owner becomes ``accepted`` only when the run
      it names is *there to be seen* -- the recorded run id matching the run
      ASL currently holds the plan for. Every other shape settles as
      ``unconfirmed``: a mutation may have run and nobody can say, which is the
      honest answer and never a reason to try again.
    * ``accepted`` is ``consumed`` on the same positive evidence, and
      ``unconfirmed`` when the run it names is gone. Success is inferred from
      presence, never from absence: a run that has vanished may have finished,
      crashed, or been killed, and only its own records tell those apart.
    * A :data:`REPLY_BEARING_METHODS` operation is the exception to the two
      rules above, and in the strict direction. What it promised is that an
      operator's words were read, so a run being *there* does not settle it --
      only :attr:`Evidence.reply_consumed`, which is a run's own record of
      taking that reply into a turn, does. That evidence also resolves an
      earlier ``unconfirmed`` result: uncertainty is deliberately open to a
      late receipt. A resumed run is a new process with a new id, so the
      presence test those rules use cannot apply to it anyway: it would call
      every successful resume somebody else's run.

    ``superseding`` is what a *new* submission passes, and it does one thing:
    an ``unconfirmed`` operation whose target is demonstrably idle stops
    holding its scope, as ``superseded``. That is not a finding about whether
    it ran -- nothing can produce one -- it is the record that an operator
    submitted something in its place. Without it an interrupted process would
    make a plan permanently unmutatable, which would turn "never repeat
    automatically" into "never act again"; with it, the only thing that can
    start a second mutation is a person deciding to, which is the line this
    module actually draws.
    """
    status = str(record.get("status", ""))
    if not outstanding(status):
        return None

    receipt = evidence.receipt_status
    if receipt is not None and receipt in OPERATION_TRANSITIONS.get(
        status, frozenset()
    ):
        return Reconciliation(
            receipt, evidence.receipt_detail_code or DETAIL_RECONCILED
        )

    deliver_by = record.get("deliver_by")
    overdue = isinstance(deliver_by, int | float) and now >= float(deliver_by)
    recorded_run = record.get("run_id")
    bears_reply = str(record.get("method", "")) in REPLY_BEARING_METHODS
    #: Only a row that was actually dispatched can have been read, so a
    #: ``pending`` row is never witnessed however loud the evidence is.
    witnessed = (
        bears_reply
        and evidence.reply_consumed
        and status in (OPERATION_DISPATCHED, OPERATION_ACCEPTED, OPERATION_UNCONFIRMED)
    )

    if evidence.owner_alive and not witnessed:
        return None

    identified = (
        not bears_reply
        and evidence.run_active
        and evidence.run_id is not None
        and recorded_run == evidence.run_id
    )

    if status == OPERATION_PENDING:
        return Reconciliation(
            OPERATION_EXPIRED,
            DETAIL_DEADLINE_PASSED if overdue else DETAIL_CONNECTOR_RESTARTED,
        )

    if status == OPERATION_DISPATCHED:
        if witnessed:
            return Reconciliation(OPERATION_ACCEPTED, DETAIL_REPLY_WITNESSED)
        if identified:
            return Reconciliation(OPERATION_ACCEPTED, DETAIL_RECONCILED)
        if bears_reply and evidence.run_active and not overdue:
            # Something is running on this plan and has not said it read the
            # reply. It may be about to, so waiting is what a look this early
            # is for, and the deadline is what ends the wait.
            return None
        return Reconciliation(
            OPERATION_UNCONFIRMED,
            DETAIL_NO_RESULT if overdue else DETAIL_CONNECTOR_RESTARTED,
        )

    if status == OPERATION_ACCEPTED:
        if witnessed:
            return Reconciliation(OPERATION_CONSUMED, DETAIL_REPLY_WITNESSED)
        if identified:
            return Reconciliation(OPERATION_CONSUMED, DETAIL_RECONCILED)
        if bears_reply and evidence.run_active and not overdue:
            return None
        if evidence.run_active and recorded_run is None:
            # Something holds the plan and this row cannot say whether it is
            # the thing it started. Waiting costs nothing; guessing costs the
            # only guarantee here worth having.
            return None
        return Reconciliation(OPERATION_UNCONFIRMED, DETAIL_NO_RESULT)

    if status == OPERATION_UNCONFIRMED:
        if witnessed:
            return Reconciliation(OPERATION_CONSUMED, DETAIL_REPLY_WITNESSED)
        if superseding and not evidence.run_active:
            return Reconciliation(OPERATION_SUPERSEDED, DETAIL_REPLACED_UNCERTAIN)

    # `unconfirmed` with no receipt and nothing replacing it: this is already
    # the most that can be said.
    return None
