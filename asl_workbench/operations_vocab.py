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
    # way out. An expired mutation receipt proves the connector never attempted
    # the command, even if link silence had earlier made it unconfirmed.
    OPERATION_UNCONFIRMED: frozenset(
        {
            OPERATION_CONSUMED,
            OPERATION_REJECTED,
            OPERATION_EXPIRED,
            OPERATION_SUPERSEDED,
        }
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

__all__ = ["hashlib", "json", "Mapping", "Sequence", "dataclass", "Any", "METHOD_LAUNCH", "METHOD_REPLY", "METHOD_QUESTION_REPLY", "METHOD_STOP_AFTER_CURRENT", "METHOD_ABORT", "METHOD_SELECT_HARNESS", "METHOD_REDIRECT", "METHOD_UNLOCK", "METHOD_PLAN_ARCHIVE", "METHOD_ARCHIVE_RESTORE", "METHOD_ARCHIVE_DELETE", "METHOD_ARCHIVE_SETTINGS", "OPERATION_METHODS", "UNSCOPED_MUTATIONS", "REMOTE_METHOD_ALIASES", "RUN_STARTING_METHODS", "SCOPE_RUN_START", "REPLY_BEARING_METHODS", "MAX_OPERATION_ID_CHARS", "OPERATION_PENDING", "OPERATION_DISPATCHED", "OPERATION_CANCELLED", "OPERATION_ACCEPTED", "OPERATION_CONSUMED", "OPERATION_REJECTED", "OPERATION_EXPIRED", "OPERATION_SUPERSEDED", "OPERATION_UNCONFIRMED", "ASL_SETTLED_STATUSES", "OPERATION_STATUSES", "OPERATION_OUTSTANDING_STATUSES", "OPERATION_TERMINAL_STATUSES", "OPERATION_TRANSITIONS", "DETAIL_DEADLINE_PASSED", "DETAIL_OWNER_CANCELLED", "DETAIL_CONNECTOR_RESTARTED", "DETAIL_NO_RESULT", "DETAIL_BRIDGE_UNAVAILABLE", "DETAIL_ACTION_TOKEN_REFUSED", "DETAIL_RECONCILED", "DETAIL_LAUNCH_REFUSED", "DETAIL_REPLACED_UNCERTAIN", "DETAIL_REPLY_WITNESSED", "OPERATION_DETAIL_CODES", "OPERATION_PERSIST_FIELDS", "OPERATION_EXPORT_FIELDS", "FORBIDDEN_PERSISTED_KEYS", "REASON_INVALID_METHOD", "REASON_INVALID_OPERATION_ID", "REASON_INVALID_STATUS", "REASON_INVALID_TRANSITION", "REASON_BODY_LEAK", "REASON_UNKNOWN_OPERATION", "REASON_OPERATION_CONFLICT", "REASON_OPERATION_OUTSTANDING", "REASON_OPERATION_UNCERTAIN", "_STATUS_BY_REASON", "CONFLICT_METHOD", "CONFLICT_TARGET"]  # fmt: skip
