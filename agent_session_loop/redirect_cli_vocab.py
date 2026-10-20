# ruff: noqa: F401
"""Operator surface for urgent corrections: submit, look up, and recover.

Three commands sit on top of the redirect subsystem, and the split between them
is the subsystem's own split between what may write and what may not:

``asl control redirect``
    The only one that writes. It submits one correction against the turn that is
    running right now and waits, briefly, for the runner's own record of
    acceptance (:mod:`agent_session_loop.redirect_admission`).
``asl control redirect-status``
    A pure read of what became of corrections already submitted
    (:func:`agent_session_loop.redirect_recovery.inspect_plan_recovery`), with an
    optional observational wait for worker receipts
    (:func:`agent_session_loop.redirect_ack.wait_for_acknowledgment`). It appends
    nothing, settles nothing, and never reclaims a lock, so asking twice gives
    the same answer and asking at all changes nothing.
``asl control recover-conversation``
    A pure read that prints the exact one-use relaunch an unresumable
    conversation needs. It deliberately does not *perform* the recovery: the
    ``--fresh-conversation`` authorization is one-use and is never persisted, so
    a surface that consumed it on the operator's behalf would be storing the
    decision it exists to keep with a human.

**Two names for one fact.** Every operation is reported with both a ``status``
-- the durable projection from :mod:`redirect_recovery`, copied verbatim so this
surface never becomes a second source of truth -- and a ``stage``, which is what
an operator asked "where is my correction?" actually wants. The stage adds one
thing the status cannot carry, because the status is a pure function of the
journal: whether the live run is still on the turn the correction named. An
``accepted`` correction whose target turn is still running is ``interrupting``;
the same record once that turn is gone is ``stopped``. Neither is a new
durable state, and nothing here writes either one down.

**Exit codes** mean the same thing in both reading and writing commands:

``0``
    The command did what was asked.
``1``
    It could not: a usage error, an unresolvable plan, an unreadable journal, or
    a redirect definitely refused. Nothing to retry unchanged.
``2``
    The urgent channel was unavailable. The request is durably settled, so
    nothing will act on it later; retrying once a run is reachable is safe.
``3``
    Unresolved, and the answer is an operation id rather than a verdict. Retry
    by *reconciling* that id -- ``redirect-status``, or a resubmission carrying
    the same ``--operation-id``, which the journal treats as the same request --
    never by submitting the correction again as new work.
"""

from __future__ import annotations

import argparse
import math
import shlex
from pathlib import Path
from typing import Any

from . import (
    cli_common,
    events,
    intervention,
    redirect_ack,
    redirect_ack_attempt,
    redirect_admission,
    redirect_recovery,
    redirect_restart,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_validation,
    redirect_store_vocab,
    responses,
)

# Command names carried in every payload so a supervisor can branch on
# `command` without inspecting argv.
COMMAND_REDIRECT = "control-redirect"
COMMAND_REDIRECT_STATUS = "control-redirect-status"
COMMAND_RECOVER_CONVERSATION = "control-recover-conversation"

TEXT_LABEL_REDIRECT = "control redirect"
TEXT_LABEL_REDIRECT_STATUS = "control redirect-status"
TEXT_LABEL_RECOVER_CONVERSATION = "control recover-conversation"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNAVAILABLE = 2
EXIT_UNRESOLVED = 3

# Input and lookup reasons this surface owns. The journal's, the admission
# layer's, and the acknowledgment protocol's own reasons are reported verbatim
# rather than remapped, so an operator reading a CLI failure and an operator
# reading the journal see the same word for the same refusal.
ERROR_MISSING_TEXT = "missing-text"
ERROR_INVALID_TEXT_SOURCE = "invalid-text-source"
ERROR_STDIN_READ = "stdin-read-error"
ERROR_INVALID_TIMEOUT = "invalid-timeout"
ERROR_INVALID_TARGET = "invalid-target"

#: Stage vocabulary. One string per answer to "where is my correction?".
STAGE_SUBMITTED = "submitted"
STAGE_UNACCEPTED = "unaccepted"
STAGE_INTERRUPTING = "interrupting"
STAGE_STOPPED = "stopped"
STAGE_RESUMING = "resuming"
STAGE_ACKNOWLEDGED = "acknowledged"
STAGE_REJECTED = "rejected"
STAGE_BLOCKED = "blocked"
STAGE_UNCERTAIN = "uncertain"

STAGES: tuple[str, ...] = (
    STAGE_SUBMITTED,
    STAGE_UNACCEPTED,
    STAGE_INTERRUPTING,
    STAGE_STOPPED,
    STAGE_RESUMING,
    STAGE_ACKNOWLEDGED,
    STAGE_REJECTED,
    STAGE_BLOCKED,
    STAGE_UNCERTAIN,
)

#: What each stage means, in the words the command prints. Kept beside the
#: vocabulary so the CLI, the JSON payload, and the documentation cannot drift
#: into three different explanations of the same word.
STAGE_DESCRIPTIONS: dict[str, str] = {
    STAGE_SUBMITTED: "persisted and unanswered; a live run may still take it",
    STAGE_UNACCEPTED: "persisted and unanswered; no run is left to take it",
    STAGE_INTERRUPTING: "accepted; the turn it names is still being stopped",
    STAGE_STOPPED: "accepted; the turn it named is gone and a successor owes it",
    STAGE_RESUMING: "carried into the live attempt; no worker receipt yet",
    STAGE_ACKNOWLEDGED: "a worker receipted it on the attempt that carried it",
    STAGE_REJECTED: "settled without being acted on",
    STAGE_BLOCKED: "the urgent channel could not carry it; durably settled",
    STAGE_UNCERTAIN: "delivered to an attempt that never receipted it",
}

#: Settlement reasons that mean "not here, not now" rather than "never". A
#: correction settled for one of these is reported ``blocked``, which is the
#: stage that pairs with exit code 2.
BLOCKED_SETTLEMENT_REASONS: tuple[str, ...] = (
    redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE,
)

RECONCILE_HINT = (
    "reconcile it by id with `asl control redirect-status` before resubmitting; "
    "a resubmission carrying the same --operation-id is the same request, a new "
    "one is a second correction"
)

FRESH_CONVERSATION_NOTE = (
    "the authorization is one-use: the first turn that needs it consumes it, and "
    "it is never persisted, rehydrated by --resume, or replayed by a retry"
)


# ---------------------------------------------------------------------------
# Shared projection
# ---------------------------------------------------------------------------
