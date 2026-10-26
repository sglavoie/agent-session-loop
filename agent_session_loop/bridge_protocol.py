"""NDJSON stdio bridge over ASL's active/actionable run projection.

The bridge is the authoritative boundary between ASL and a standalone
supervision client. Protocol 2 is UTF-8 NDJSON on stdio: the server writes a
``hello`` frame before reading anything, every request carries a correlation
``id`` answered by exactly one response, and unsolicited events carry a
monotonic ``seq``. Stdout is protocol-only; diagnostics go to stderr.

Protocol 2 is not a negotiation. The hello names the contract set both ends
must already agree on, and a client that does not recognize it has no
compatible way to continue: there is no protocol-1 parser left to fall back to.
Reads stay available across a mismatch so a stale cockpit can still explain
itself, but every mutation is gated behind ``session.handshake``, which accepts
only an exact contract-set match against a snapshot digest the bridge still
holds.

Five properties are structural rather than incidental:

- **Observation never writes.** Every state read goes through the explicitly
  non-reclaiming paths (``reclaim=False``), so a snapshot is a function of the
  corpus and not of the order in which the bridge happened to poll it.
- **Question bodies never enter a snapshot.** Rows carry question identity,
  counts, and timing only. The prose an operator answers is fetched deliberately
  through ``question.get`` and never rides along with a projection.
- **Every mutation goes through the shared service layer.** The handlers below
  own request parsing and the operation model; :mod:`.intervention` owns what is
  legal, so the bridge cannot drift from the CLI or the Workbench HTTP API.
- **Reply text never becomes protocol or diagnostic state.** It is passed
  straight to ASL's own durable records and is absent from every response,
  event, operation receipt, and stderr line the bridge can emit.
- **A mutation is authorized against state the client has actually seen.** The
  handshake pins a snapshot digest; the action token pins the plan. Both must
  still hold when the request lands, so an operator can never act on a row that
  moved between the projection and the click.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from asl_workbench import controls as control_rules

from . import (
    clock,
    intervention,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store_vocab,
    workbench_summary_model,
)

PROTOCOL_VERSION = 2


# A frame ceiling only helps if both ends agree on it before parsing, so it is
# advertised in the hello and enforced on read and on write.
MAX_FRAME_BYTES = 1024 * 1024


POLL_INTERVAL_SECONDS = 1.0


# How long a failed projection is owed before it is attempted again. A failure
# leaves the last successful projection's deadline in the past, so without a
# deadline of its own the loop selects with timeout zero and retries the same
# failing walk immediately, burning a core instead of waiting.
PROJECTION_RETRY_INTERVAL_SECONDS = 1.0


# How often the Workbench listener checks whether it has been asked to stop.
# It is well below the client's shutdown grace because the client terminates a
# bridge that has not exited by then, and the default half second is long
# enough to turn every clean exit into a kill.
WORKBENCH_POLL_INTERVAL_SECONDS = 0.02


# How long a stopping bridge waits for an in-flight projection to notice. A
# build cannot be interrupted mid-walk, so this is bounded by the corpus cost
# rather than by the client's shutdown grace; the thread is a daemon thread, so
# exceeding it costs an unjoined thread rather than a hung exit.
WORKER_JOIN_TIMEOUT_SECONDS = 0.1


# Ordered so a client can compare capability lists byte for byte across
# restarts. Later protocol work appends; it never reorders or removes.
#
# This is the *candidate* order. The advertised ``CAPABILITIES`` is derived at
# the bottom of this module by intersecting it with the methods that actually
# got registered, so a name can never reach a hello without a handler behind it.
#
# Protocol 2 splits the single protocol-1 capability list into three closed
# vocabularies. Mixing methods and events in one array made "can I call this?"
# and "will I be sent this?" indistinguishable to a client, and left the
# mutation subset — the one that needs a handshake — implicit.
METHOD_HANDSHAKE = "session.handshake"


METHOD_REDIRECT_STATUS = "run.redirect_status"


METHOD_RECOVER_CONVERSATION = "run.recover_conversation"


_METHOD_ORDER: tuple[str, ...] = (
    METHOD_HANDSHAKE,
    "runs.snapshot",
    "question.get",
    "question.reply",
    "question.resolve",
    "run.resume",
    "run.stop_after_current",
    "run.abort",
    "workbench.ensure",
    "run.redirect",
    METHOD_REDIRECT_STATUS,
    METHOD_RECOVER_CONVERSATION,
)


# The methods that change ASL state. Each one is refused until a handshake has
# matched the contract set against a snapshot digest the bridge still holds.
#
# ``run.redirect`` is here and its two companions are not, and the split is the
# point: submitting a correction appends to the redirect journal, while reading
# where a correction got to and reading which recovery a pause is waiting on
# both write nothing. A read that a stale client can still make is what lets an
# incompatible cockpit explain a stuck run instead of guessing at it.
MUTATION_METHODS: tuple[str, ...] = (
    "question.reply",
    "question.resolve",
    "run.resume",
    "run.stop_after_current",
    "run.abort",
    "run.redirect",
)


# Reads that hand out capability rather than state. ``workbench.ensure`` writes
# nothing, so it stays out of ``MUTATION_METHODS`` and needs no current
# snapshot, but its answer is a tokenized owner URL whose token passes the
# Workbench's own mutating routes. Serving that before a handshake has matched
# the contract set would let any client skip the gate by asking for the link,
# so these methods require a contract-matched handshake and are refused the way
# an unhandshaken mutation is.
CAPABILITY_METHODS: tuple[str, ...] = ("workbench.ensure",)


EVENT_RUNS_CHANGED = "runs.changed"


EVENT_OPERATION_UPDATED = "operation.updated"


EVENT_NAMES: tuple[str, ...] = (EVENT_RUNS_CHANGED, EVENT_OPERATION_UPDATED)


# Rich discovery is the intervention queue, not the plan list: healthy
# ``ready``, ``recent``, and ``complete`` plans stay in Plans and Workbench.
ACTIVE_PRIMARY_STATES: tuple[workbench_summary_model.PrimaryState, ...] = (
    "paused-for-reply",
    "failed",
    "stale",
    "invalid",
    "verifying",
    "running",
)


Phase = str


PHASE_AWAITING_REPLY = "awaiting-reply"


PHASE_BY_STATE: dict[workbench_summary_model.PrimaryState, Phase] = {
    "paused-for-reply": PHASE_AWAITING_REPLY,
    "failed": "failed",
    "stale": "stale",
    "invalid": "invalid",
    "verifying": "verifying",
    "running": "running",
}


PHASES: tuple[Phase, ...] = (
    PHASE_AWAITING_REPLY,
    "failed",
    "stale",
    "invalid",
    "verifying",
    "running",
)


CONTEXT_KIND_PAUSE = "pause"


CONTEXT_KIND_WAITING = "waiting"


CONTEXT_KINDS: tuple[str, ...] = (CONTEXT_KIND_PAUSE, CONTEXT_KIND_WAITING)


ERROR_MALFORMED_JSON = "malformed-json"


ERROR_INVALID_REQUEST = "invalid-request"


ERROR_UNSUPPORTED_PROTOCOL = "unsupported-protocol"


ERROR_UNKNOWN_METHOD = "unknown-method"


ERROR_FRAME_TOO_LARGE = "frame-too-large"


ERROR_INTERNAL = "internal-error"


ERROR_MUTATION_FAILED = "mutation-failed"


ERROR_WORKBENCH_UNAVAILABLE = "workbench-unavailable"


# Protocol 2 appends exactly two codes and removes none, so a client that
# understands the protocol-1 vocabulary still understands every refusal it
# shares with protocol 2.
#
# The two are deliberately different kinds of failure. A contract-set mismatch
# is a build-time disagreement: retrying cannot fix it, so it is reported
# non-retryable and the client is expected to degrade to read-only rather than
# loop. A stale snapshot is a race the client can and should resolve by
# refreshing and handshaking again.
ERROR_CONTRACT_SET_MISMATCH = "contract-set-mismatch"


ERROR_STALE_SNAPSHOT = "stale-snapshot"


ERROR_CODES: tuple[str, ...] = (
    ERROR_MALFORMED_JSON,
    ERROR_INVALID_REQUEST,
    ERROR_UNSUPPORTED_PROTOCOL,
    ERROR_UNKNOWN_METHOD,
    ERROR_FRAME_TOO_LARGE,
    ERROR_INTERNAL,
    ERROR_MUTATION_FAILED,
    ERROR_WORKBENCH_UNAVAILABLE,
    # Refusal reasons the service layer owns. They are listed here so a client
    # sees one error vocabulary rather than two.
    intervention.REASON_INVALID_PLAN,
    intervention.REASON_NO_ACTIVE_RUN,
    intervention.REASON_RUN_MISMATCH,
    intervention.REASON_NOT_PAUSED,
    intervention.REASON_NO_OPEN_QUESTION,
    intervention.REASON_QUESTION_MISMATCH,
    intervention.REASON_STALE_ACTION_TOKEN,
    intervention.REASON_ACTION_NOT_PERMITTED,
    intervention.REASON_INVALID_ACTION,
    intervention.REASON_EMPTY_TEXT,
    intervention.REASON_BODY_TOO_LARGE,
    intervention.REASON_MISSING_QUESTION_BODY,
    ERROR_CONTRACT_SET_MISMATCH,
    ERROR_STALE_SNAPSHOT,
)


OPERATION_ACCEPTED = "accepted"


OPERATION_CONSUMED = "consumed"


OPERATION_REJECTED = "rejected"


OPERATION_EXPIRED = "expired"


OPERATION_SUPERSEDED = "superseded"


OPERATION_STATUSES: tuple[str, ...] = (
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_REJECTED,
    OPERATION_EXPIRED,
    OPERATION_SUPERSEDED,
)


OPERATION_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        OPERATION_CONSUMED,
        OPERATION_REJECTED,
        OPERATION_EXPIRED,
        OPERATION_SUPERSEDED,
    }
)


# A receipt window, not a journal: it exists so a client that reconnects within
# a few minutes learns the fate of what it submitted, and it is deliberately
# small enough that a long-lived bridge cannot accumulate operator history.
#
# Redirect operations deliberately do not live here. Their receipt is the
# redirect journal, which survives a bridge restart, an eviction, and the TTL
# below; projecting them out of this cache instead would make "where did my
# correction go?" answerable only by the process that happened to take it.
MAX_OPERATIONS = 64


OPERATION_TTL_SECONDS = 900.0


# Redirect vocabularies, aliased rather than restated. Each one is owned by the
# module that produces it, so a value this protocol can carry is exactly a value
# ASL can produce — the alternative is a fourth list that drifts.
REDIRECT_STATUSES: tuple[str, ...] = redirect_recovery.OPERATION_STATUSES


REDIRECT_STAGES: tuple[str, ...] = redirect_cli.STAGES


REDIRECT_OUTCOMES: tuple[str, ...] = redirect_admission.OUTCOMES


OWNER_LIVENESS_VALUES: tuple[str, ...] = (
    redirect_recovery.OWNER_LIVE,
    redirect_recovery.OWNER_GONE,
    redirect_recovery.OWNER_UNKNOWN,
)


# Why a correction was refused or settled, from both sides of the same boundary:
# admission refuses before anything is persisted, the journal settles after, and
# `submitted` is the third answer — persisted, unsettled, and inside its TTL,
# which is neither. A client renders one vocabulary; ordered admission-first and
# deduplicated so the union is stable under any of the three lists growing.
REDIRECT_REASONS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *redirect_admission.REFUSAL_REASONS,
            *redirect_store_vocab.SETTLEMENT_REASONS,
            redirect_store_vocab.STATE_SUBMITTED,
        )
    )
)


# How many durable redirect operations one projection may carry. The journal is
# append-only and unbounded, so the projection needs a ceiling or a long-lived
# plan eventually produces a frame no client can read. Aliased rather than
# restated: two surfaces with two windows would disagree about which
# corrections exist.
MAX_REDIRECT_OPERATIONS = control_rules.MAX_REDIRECT_OPERATIONS


# Deep-link focus keys the Workbench front end recognizes. A request may name
# any of them; the default lands on the panel the operator is about to act in.
WORKBENCH_FOCUS_REPLY = "reply"


WORKBENCH_FOCUS_VERIFICATION = "verification"


WORKBENCH_FOCUS_ACTIVE = "active"


WORKBENCH_FOCUS_EVENTS = "events"


WORKBENCH_FOCUS_VALUES: tuple[str, ...] = (
    WORKBENCH_FOCUS_REPLY,
    WORKBENCH_FOCUS_VERIFICATION,
    WORKBENCH_FOCUS_ACTIVE,
    WORKBENCH_FOCUS_EVENTS,
)


# Per-field bounds run first so a single corrupt plan cannot consume the whole
# frame budget, and the aggregate row bound runs second so a corrupt *corpus*
# cannot either. Both are needed: neither one implies the other.
MAX_TEXT_CHARS = 400


MAX_PATH_CHARS = 1024


MAX_ID_CHARS = 128


# The request id is echoed verbatim on the response, so it is bounded on the
# way in: an id the size of a frame would push its own answer past the ceiling,
# and a mutation could land with its response dropped. Mirrors the corpus
# ``RequestId`` bound, so every id this bridge echoes is one the schema accepts.
MAX_REQUEST_ID_CHARS = 64


MAX_WARNINGS = 8


MAX_QUESTION_IDS = 16


MAX_ROWS = 200


class BridgeError(Exception):
    """A protocol-level failure carrying a stable, client-visible code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": _bounded_text(self.message),
            "retryable": self.retryable,
            "details": self.details,
        }


def _bounded_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _bounded_path(value: Any) -> str | None:
    if isinstance(value, Path):
        value = str(value)
    return _bounded_text(value, limit=MAX_PATH_CHARS)


def _bounded_id(value: Any) -> str | None:
    return _bounded_text(value, limit=MAX_ID_CHARS)


def _bounded_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bounded_strings(values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    bounded = []
    for value in values:
        text = _bounded_text(value)
        if text is not None:
            bounded.append(text)
        if len(bounded) >= limit:
            break
    return bounded


def _epoch(value: Any) -> float | None:
    # Naive is UTC, as everywhere ASL reads its own stamps back; `.timestamp()`
    # on a naive datetime would read it as this host's local time instead.
    parsed = clock.parse_datetime(value)
    return parsed.timestamp() if parsed is not None else None


def encode_frame(payload: Mapping[str, Any]) -> bytes:
    """Encode one NDJSON frame, newline included.

    ``sort_keys`` makes the bytes a function of the payload alone, which is
    what lets a client compare frames and what makes the tests deterministic.
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _response(request_id: str | None, result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "response",
        "protocol": PROTOCOL_VERSION,
        "id": request_id,
        "ok": True,
        "result": dict(result),
    }


def _error_response(request_id: str | None, error: BridgeError) -> dict[str, Any]:
    return {
        "type": "response",
        "protocol": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": error.payload(),
    }
