"""The Workbench's own wire format, versioned apart from everything it carries.

Five closed envelopes move between a Mac connector and the Pi service:

* a **request** the service hands down -- publish a catalog page, publish
  history, refresh observations, read one detail, submit one mutation;
* an **observation** the connector uploads, which is the cache-safe material
  :mod:`asl_workbench.store` retains;
* a **detail**, the one answer that may carry prose, read online and never
  written down;
* an **operation** update, which is a mutation's state moving through
  :data:`asl_workbench.operations.OPERATION_TRANSITIONS`;
* a **receipt**, which says what became of one correlated request.

Three properties are enforced here rather than left to each caller.

* **Closed.** Every envelope, payload and nested entry is checked against a
  :class:`_Shape`: a name that is not required and not optional is an error,
  not data to ignore. A field that carries a vocabulary carries a word from a
  tuple this package already owns -- methods and detail codes from
  :mod:`asl_workbench.operations`, artifacts and detail kinds from
  :mod:`asl_workbench.details`, outcomes and pause reasons from
  :mod:`asl_workbench.history`. The transport invents no vocabulary it could
  borrow, so a word cannot mean one thing on the wire and another in storage.

* **Bounded before parsed.** :func:`validate_frame` measures the raw bytes
  against :data:`MAX_FRAME_BYTES` before handing them to :mod:`json`, so an
  oversized frame is refused without being decoded, and ``NaN``/``Infinity``
  are refused by :func:`refuse_non_finite` rather than becoming floats no
  database column can hold.

* **Separately versioned.** :data:`TRANSPORT_PROTOCOL_VERSION` is this
  transport's own number. It is not the Companion's protocol version and it is
  not AgentMux's bridge protocol: the contract set the connector reports
  travels *inside* the envelope as an opaque identifier, checked against
  :data:`asl_workbench.observation.SUPPORTED_CONTRACT_SET_IDS`. Nothing in
  ``tests/fixtures/agent_mux_contract/`` describes these envelopes, and the
  corpus that does lives at ``tests/fixtures/workbench_transport/`` precisely
  so the AgentMux contract-set identity cannot move when this format changes.

:func:`evaluate_hello` keeps two failures apart, because they call for
different behaviour:

* **An incompatible version** is fatal in both directions. If the two ends
  share no transport protocol number, neither can read the other's frames, so
  reads and mutations are both refused and the only honest report is that the
  deployments do not speak.
* **An unsupported capability** is a narrowing. A peer that never offers
  ``mutate``, or offers a capability this build has never heard of, is still a
  peer whose observations are worth accepting: reads stay available and only
  the capability in question is withheld. The same holds for a contract set
  this build does not understand -- the connector's observations are recorded,
  and mutations are refused, because acting on a projection whose shape is
  unknown is the one thing that cannot be undone.

:func:`authorize` turns that verdict into a refusal on a specific envelope, so
"an incompatible deployment cannot execute mutations" is a check on the path a
mutation actually takes rather than a property somebody remembers to preserve.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .catalog import (
    ACTIVITY_LABELS,
    COUNT_KEYS,
    DISCOVERY_SOURCES,
    KEY_CHARS,
    LAST_ACTIVITY_SOURCES,
    PRIMARY_STATES,
    RECOMMENDED_ACTIONS,
    URGENCY_GROUPS,
)
from .details import (
    ARTIFACT_NAMES,
    DETAIL_KINDS,
    MAX_EVENT_LIMIT,
    REASON_INVALID_PLAN,
    REASON_INVALID_REQUEST,
    REASON_MALFORMED_PLAN,
    REASON_NOT_FOUND,
    REASON_OS_ERROR,
    STREAM_ARTIFACTS,
)
from .history import (
    HISTORY_OUTCOMES,
    PAUSE_REASON_OTHER,
    PAUSE_REASONS,
    REVIEW_OUTCOMES,
)
from .observation import (
    COMPLETENESS_LABELS,
    MAX_LABEL_CHARS,
    MAX_WARNINGS,
    SUPPORTED_CONTRACT_SET_IDS,
    ObservationPolicy,
    require_policy,
)
from .operations import (
    FORBIDDEN_PERSISTED_KEYS,
    MAX_OPERATION_ID_CHARS,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DETAIL_CODES,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_METHODS,
    OPERATION_REJECTED,
    OPERATION_SUPERSEDED,
    OPERATION_UNCONFIRMED,
)

# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

#: This transport's version. Incremented when an envelope changes shape in a
#: way an older peer cannot read. Deliberately *not* derived from the
#: Companion's ``PROTOCOL_VERSION`` or from AgentMux's bridge protocol: the
#: Workbench is a second application over the same bridge, and tying its wire
#: format to the bridge's number is what would force one to move when only the
#: other changed.
TRANSPORT_PROTOCOL_VERSION = 1

#: Every version this build can still speak. A peer announcing a newer number
#: alongside an older one it also supports is negotiated down rather than
#: refused, which is what makes a rolling upgrade possible at all.
SUPPORTED_TRANSPORT_PROTOCOLS: tuple[int, ...] = (TRANSPORT_PROTOCOL_VERSION,)

# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

#: Heartbeats and run snapshots: is the bridge alive, and what does it show.
CAPABILITY_OBSERVE = "observe"
#: Catalog pages -- the published list of plans.
CAPABILITY_CATALOG = "catalog"
#: Completed-run history batches.
CAPABILITY_HISTORY = "history"
#: Online detail reads. The only capability whose answers carry prose.
CAPABILITY_DETAIL = "detail"
#: Mutations. The only capability that changes anything on the Mac.
CAPABILITY_MUTATE = "mutate"

TRANSPORT_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_OBSERVE,
    CAPABILITY_CATALOG,
    CAPABILITY_HISTORY,
    CAPABILITY_DETAIL,
    CAPABILITY_MUTATE,
)

#: Without this one there is nothing to be a connector *for*, so its absence is
#: an incompatibility rather than a narrowing. Everything else is optional and
#: its absence costs only that capability.
REQUIRED_CAPABILITIES: frozenset[str] = frozenset({CAPABILITY_OBSERVE})

#: The capabilities that change state on the Mac. An envelope carrying one of
#: these is refused unless :attr:`TransportCompatibility.mutations_allowed`.
MUTATING_CAPABILITIES: frozenset[str] = frozenset({CAPABILITY_MUTATE})

# --------------------------------------------------------------------------
# Envelope kinds
# --------------------------------------------------------------------------

#: Service to connector: the one envelope that travels downwards.
KIND_REQUEST = "request"
#: Connector to service: cache-safe material for the store.
KIND_OBSERVATION = "observation"
#: Connector to service: one online read, never retained.
KIND_DETAIL = "detail"
#: Connector to service: one mutation's state.
KIND_OPERATION = "operation"
#: Connector to service: what became of one correlated request.
KIND_RECEIPT = "receipt"

ENVELOPE_KINDS: tuple[str, ...] = (
    KIND_REQUEST,
    KIND_OBSERVATION,
    KIND_DETAIL,
    KIND_OPERATION,
    KIND_RECEIPT,
)

#: Direction names, used to refuse a frame that is well formed but arriving at
#: the wrong end -- a connector replaying a request back at the service, say.
DIRECTION_DOWN = "down"
DIRECTION_UP = "up"

KINDS_BY_DIRECTION: Mapping[str, frozenset[str]] = {
    DIRECTION_DOWN: frozenset({KIND_REQUEST}),
    DIRECTION_UP: frozenset(
        {KIND_OBSERVATION, KIND_DETAIL, KIND_OPERATION, KIND_RECEIPT}
    ),
}

DIRECTION_BY_KIND: Mapping[str, str] = {
    kind: direction for direction, kinds in KINDS_BY_DIRECTION.items() for kind in kinds
}

#: Which capability each kind may claim. A detail envelope claiming ``mutate``
#: is refused before anything reads its payload, so the capability field can be
#: trusted as the thing :func:`authorize` gates on.
CAPABILITIES_BY_KIND: Mapping[str, frozenset[str]] = {
    KIND_REQUEST: frozenset(TRANSPORT_CAPABILITIES),
    KIND_OBSERVATION: frozenset(
        {CAPABILITY_OBSERVE, CAPABILITY_CATALOG, CAPABILITY_HISTORY}
    ),
    KIND_DETAIL: frozenset({CAPABILITY_DETAIL}),
    KIND_OPERATION: frozenset({CAPABILITY_MUTATE}),
    KIND_RECEIPT: frozenset(TRANSPORT_CAPABILITIES),
}

# --------------------------------------------------------------------------
# Observation views and request methods
# --------------------------------------------------------------------------

#: The four uploads a connector makes. The names match
#: :mod:`asl_workbench.store`'s ``KIND_*`` constants exactly, so a validated
#: observation envelope is ingestible without translation; the test module
#: asserts that equality rather than leaving it to a comment.
VIEW_HEARTBEAT = "heartbeat"
VIEW_SNAPSHOT = "snapshot"
VIEW_HISTORY_BATCH = "history_batch"
VIEW_CATALOG_PAGE = "catalog_page"

OBSERVATION_VIEWS: tuple[str, ...] = (
    VIEW_HEARTBEAT,
    VIEW_SNAPSHOT,
    VIEW_HISTORY_BATCH,
    VIEW_CATALOG_PAGE,
)

CAPABILITY_BY_VIEW: Mapping[str, str] = {
    VIEW_HEARTBEAT: CAPABILITY_OBSERVE,
    VIEW_SNAPSHOT: CAPABILITY_OBSERVE,
    VIEW_HISTORY_BATCH: CAPABILITY_HISTORY,
    VIEW_CATALOG_PAGE: CAPABILITY_CATALOG,
}

REQUEST_OBSERVE = "observation.refresh"
REQUEST_CATALOG = "catalog.publish"
REQUEST_HISTORY = "history.publish"
REQUEST_DETAIL = "detail.read"
REQUEST_MUTATE = "operation.submit"

REQUEST_METHODS: tuple[str, ...] = (
    REQUEST_OBSERVE,
    REQUEST_CATALOG,
    REQUEST_HISTORY,
    REQUEST_DETAIL,
    REQUEST_MUTATE,
)

CAPABILITY_BY_REQUEST: Mapping[str, str] = {
    REQUEST_OBSERVE: CAPABILITY_OBSERVE,
    REQUEST_CATALOG: CAPABILITY_CATALOG,
    REQUEST_HISTORY: CAPABILITY_HISTORY,
    REQUEST_DETAIL: CAPABILITY_DETAIL,
    REQUEST_MUTATE: CAPABILITY_MUTATE,
}

# --------------------------------------------------------------------------
# Receipts and operation reports
# --------------------------------------------------------------------------

#: The request reached the connector and was carried out.
RECEIPT_DELIVERED = "delivered"
#: The connector declined it, and ``detail_code`` says why.
RECEIPT_REFUSED = "refused"
#: The deadline passed before the connector attempted it. Distinct from
#: ``unconfirmed`` in exactly the way :data:`OPERATION_EXPIRED` is: expired
#: means the work certainly did not happen.
RECEIPT_EXPIRED = "expired"
#: The connector attempted it and cannot say what happened. Never replayed.
RECEIPT_UNCONFIRMED = "unconfirmed"

RECEIPT_OUTCOMES: tuple[str, ...] = (
    RECEIPT_DELIVERED,
    RECEIPT_REFUSED,
    RECEIPT_EXPIRED,
    RECEIPT_UNCONFIRMED,
)

#: The statuses a connector is entitled to report. ``pending`` belongs to the
#: service, which is the only end that can admit an operation, and
#: ``cancelled`` belongs to the owner, who is the only one who may withdraw
#: one; a connector claiming either would be asserting a state it does not own.
CONNECTOR_REPORTED_STATUSES: tuple[str, ...] = (
    OPERATION_DISPATCHED,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_REJECTED,
    OPERATION_EXPIRED,
    OPERATION_SUPERSEDED,
    OPERATION_UNCONFIRMED,
)

# --------------------------------------------------------------------------
# Vocabularies borrowed rather than invented
# --------------------------------------------------------------------------

#: ASL's run phases, in ASL's order. The portable package has no phase
#: vocabulary of its own -- ``phase`` reaches the store from the projection --
#: so this mirrors the Companion's ``ASL_PHASES`` byte for byte. It is carried
#: because :data:`asl_workbench.observation.RUN_SUMMARY_PERSIST_FIELDS` has a
#: ``phase`` column that an installed database already populates: dropping the
#: field from the wire would blank a rendered field on the first deploy.
PHASES: tuple[str, ...] = (
    "awaiting-reply",
    "failed",
    "stale",
    "invalid",
    "verifying",
    "running",
)

#: Every pause reason that may travel, closed the way the Companion closes it:
#: a reason absent from the list is mapped onto ``other`` by the connector
#: rather than forwarded, because the field is declared as a code.
PAUSE_REASON_CODES: tuple[str, ...] = (*PAUSE_REASONS, PAUSE_REASON_OTHER)

#: Outcomes a run summary may carry. A summary describes a run that may still
#: be going, so it borrows history's endings and adds nothing.
SUMMARY_OUTCOMES: tuple[str, ...] = HISTORY_OUTCOMES

#: Why a detail read produced no answer. :mod:`asl_workbench.details` keeps
#: these as the reasons behind an HTTP status; a remote read has no status
#: line, so the reason itself travels and the caller renders it.
DETAIL_REFUSAL_REASONS: tuple[str, ...] = (
    REASON_INVALID_REQUEST,
    REASON_INVALID_PLAN,
    REASON_NOT_FOUND,
    REASON_MALFORMED_PLAN,
    REASON_OS_ERROR,
)

#: The stream names a log read may select, as a closed tuple.
STREAM_NAMES: tuple[str, ...] = tuple(STREAM_ARTIFACTS)

# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

#: The largest frame this transport will look at. Checked against the raw
#: bytes before parsing, so an oversized frame costs a length comparison.
MAX_FRAME_BYTES = 2 * 1024 * 1024

#: The largest ``content`` a detail answer may carry. Held well under
#: :data:`MAX_FRAME_BYTES` on purpose: JSON string escaping can expand a
#: control-character-dense window several times over, and a frame ceiling that
#: a legal answer could exceed would refuse a read the transport promises. A
#: window larger than this is paged with ``offset``/``next_offset`` rather than
#: sent whole, which is what those fields are for.
MAX_DETAIL_BYTES = 256 * 1024

#: The headroom the two ceilings must keep. Six is JSON's worst case: one
#: source byte becoming a six-character escape.
DETAIL_ESCAPE_FACTOR = 6

MAX_WARNING_CHARS = 240
MAX_RUNS_PER_SNAPSHOT = 500
MAX_ENTRIES_PER_BATCH = 100
MAX_CATALOG_ENTRIES_PER_PAGE = 100
MAX_DETAIL_ENTRIES = MAX_EVENT_LIMIT
MAX_COMPLETED_SESSION_IDS = 64
MAX_REQUEST_LIFETIME_SECONDS = 3600.0
MAX_IDENTIFIER_CHARS = 128
MAX_CAPABILITIES = 16
MAX_PROTOCOLS = 8

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

#: Not JSON, or not a JSON object.
ERROR_MALFORMED = "malformed"
#: A name no shape declares. Unknown fields are errors here, never ignored.
ERROR_UNKNOWN_FIELD = "unknown-field"
ERROR_MISSING_FIELD = "missing-field"
#: Present, declared, and not a value the field may hold.
ERROR_INVALID_FIELD = "invalid-field"
ERROR_UNKNOWN_KIND = "unknown-kind"
#: The frame exceeded :data:`MAX_FRAME_BYTES`, or a declared window exceeded
#: its own ceiling.
ERROR_FRAME_TOO_LARGE = "frame-too-large"
#: Prose reached an envelope that is retained. See :func:`assert_no_body`.
ERROR_BODY_LEAK = "body-leak"
#: A method outside :data:`asl_workbench.operations.OPERATION_METHODS`.
ERROR_FORBIDDEN_METHOD = "forbidden-method"
#: No transport protocol in common. Fatal in both directions.
ERROR_INCOMPATIBLE_PROTOCOL = "incompatible-protocol"
#: A contract set this build does not understand. Reads survive it.
ERROR_INCOMPATIBLE_CONTRACT = "incompatible-contract"
#: A capability that was never granted, or that this build does not know.
ERROR_UNSUPPORTED_CAPABILITY = "unsupported-capability"
#: Addressed to some other deployment.
ERROR_WRONG_TARGET = "wrong-target"
#: The deployment is observing only, by its own policy or the peer's.
ERROR_OBSERVATION_ONLY = "observation-only"
#: No credential proved the sender, or the one that did may not send this.
#: Distinct from every code above it: those describe two deployments that
#: cannot work together, and this describes one that has not identified itself.
ERROR_UNAUTHORIZED = "unauthorized"
#: The deadline had already passed when the envelope was composed.
ERROR_DEADLINE = "deadline"

TRANSPORT_ERROR_CODES: tuple[str, ...] = (
    ERROR_MALFORMED,
    ERROR_UNKNOWN_FIELD,
    ERROR_MISSING_FIELD,
    ERROR_INVALID_FIELD,
    ERROR_UNKNOWN_KIND,
    ERROR_FRAME_TOO_LARGE,
    ERROR_BODY_LEAK,
    ERROR_FORBIDDEN_METHOD,
    ERROR_INCOMPATIBLE_PROTOCOL,
    ERROR_INCOMPATIBLE_CONTRACT,
    ERROR_UNSUPPORTED_CAPABILITY,
    ERROR_WRONG_TARGET,
    ERROR_OBSERVATION_ONLY,
    ERROR_UNAUTHORIZED,
    ERROR_DEADLINE,
)

#: The subset a receipt may cite: the transport's own reasons for declining to
#: carry something, as opposed to ASL's reasons for declining to do it.
TRANSPORT_REFUSAL_CODES: tuple[str, ...] = (
    ERROR_INCOMPATIBLE_PROTOCOL,
    ERROR_INCOMPATIBLE_CONTRACT,
    ERROR_UNSUPPORTED_CAPABILITY,
    ERROR_WRONG_TARGET,
    ERROR_OBSERVATION_ONLY,
    ERROR_UNAUTHORIZED,
    ERROR_FORBIDDEN_METHOD,
    ERROR_DEADLINE,
)

#: What a receipt may give as its reason: ASL's operation detail codes, plus
#: the transport's own. Both vocabularies are closed, and their union is too.
RECEIPT_DETAIL_CODES: tuple[str, ...] = (
    *OPERATION_DETAIL_CODES,
    *TRANSPORT_REFUSAL_CODES,
)


class TransportError(Exception):
    """A frame this transport refuses, with a code from a closed set.

    The code is what a receipt cites and what a log line is grouped by, so it
    is asserted rather than trusted: a typo would produce a refusal nobody can
    match against anything.
    """

    def __init__(self, code: str, detail: str) -> None:
        if code not in TRANSPORT_ERROR_CODES:
            raise ValueError(f"undeclared transport error code: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shape:
    """A closed set of names: required, optional, and nothing else."""

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


#: The envelope itself. ``capability`` rides beside ``kind`` so authorization
#: reads one field rather than inferring one from a payload it has not
#: validated yet; ``connector_id`` and ``target_id`` name the two ends;
#: ``request_id`` correlates an answer with the request that asked for it, and
#: ``deadline`` says when a request stops being worth attempting.
ENVELOPE_SHAPE = _Shape(
    required=frozenset(
        {
            "protocol",
            "kind",
            "capability",
            "connector_id",
            "target_id",
            "sent_at",
            "payload",
        }
    ),
    optional=frozenset({"request_id", "deadline"}),
)

#: Where ``request_id`` is required, where it is merely allowed, and where it
#: is forbidden. An observation is unsolicited by nature -- a connector uploads
#: what it saw, not what it was asked for -- so a correlated observation would
#: be claiming a relationship that does not exist.
REQUEST_ID_REQUIRED: frozenset[str] = frozenset(
    {KIND_REQUEST, KIND_DETAIL, KIND_RECEIPT}
)
REQUEST_ID_OPTIONAL: frozenset[str] = frozenset({KIND_OPERATION})

#: Only a request carries a deadline: it is the service saying how long the
#: instruction stays live. An answer is about something that already happened.
DEADLINE_KINDS: frozenset[str] = frozenset({KIND_REQUEST})

#: The plan or run an envelope is about, kept in one nested object so no
#: top-level payload name collides with a storage column.
TARGET_SHAPE = _Shape(
    required=frozenset({"plan_key"}),
    optional=frozenset({"run_key", "run_id", "question_id", "session_id"}),
)

_SHARED_REQUEST_OPTIONAL = frozenset({"reason"})

#: One shape per request method, so a catalog publish carrying an
#: ``operation_id`` is refused rather than silently ignored.
REQUEST_SHAPES: Mapping[str, _Shape] = {
    REQUEST_OBSERVE: _Shape(
        required=frozenset({"request"}),
        optional=_SHARED_REQUEST_OPTIONAL | frozenset({"target"}),
    ),
    REQUEST_CATALOG: _Shape(
        required=frozenset({"request"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset({"generation", "offset", "limit"}),
    ),
    REQUEST_HISTORY: _Shape(
        required=frozenset({"request"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset({"window_start", "window_end", "limit"}),
    ),
    REQUEST_DETAIL: _Shape(
        required=frozenset({"request", "detail", "target"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset({"artifact", "stream", "offset", "limit", "max_bytes"}),
    ),
    # ``reply_ref`` and not ``reply``: reply prose is fetched by reference at
    # the moment of use and never travels in an envelope the service retains.
    REQUEST_MUTATE: _Shape(
        required=frozenset({"request", "operation_id", "method", "target"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset({"action_token", "reply_ref", "bridge_token", "harness"}),
    ),
}

#: One projected run inside a snapshot. Question *identity* travels; question
#: prose does not, exactly as in the projection this is derived from.
RUN_SHAPE = _Shape(
    required=frozenset(
        {
            "run_key",
            "project",
            "plan_key",
            "run_id",
            "primary_state",
            "phase",
            "urgency_group",
            "actions",
            "last_activity_at",
        }
    ),
    optional=frozenset(
        {
            "action_token",
            "session_id",
            "harness",
            "question_id",
            "question_asked_at",
            "unanswered_count",
            "run_started_at",
            "run_finished_at",
            "pause_reason",
            "outcome",
            "warnings",
        }
    ),
)

#: One completed run inside a history batch. Every name is one
#: :data:`asl_workbench.observation.HISTORY_PERSIST_FIELDS` keeps, minus the
#: ingestion bookkeeping the service assigns itself.
HISTORY_ENTRY_SHAPE = _Shape(
    required=frozenset(
        {
            "entry_id",
            "source_key",
            "plan_key",
            "run_key",
            "run_id",
            "outcome",
            "recorded_at",
        }
    ),
    optional=frozenset(
        {
            "project",
            "projects",
            "harness",
            "model",
            "pause_reason",
            "run_started_at",
            "run_finished_at",
            "duration_seconds",
            "turn_count",
            "completed_session_count",
            "pending_session_count",
            "completed_session_ids",
            "review_completed",
            "review_outcome",
            "reconstructed",
            "event_schema_version",
        }
    ),
)

#: One catalog entry. The required half is what identifies and ranks a plan;
#: everything else is absent when the Mac could not determine it, which is a
#: different statement from "the value is zero".
CATALOG_ENTRY_SHAPE = _Shape(
    required=frozenset(
        {
            "plan_key",
            "title",
            "display_label",
            "primary_state",
            "recommended_action",
            "urgency_rank",
            "urgency_group",
            "activity_label",
            "counts",
            "sources",
            "exists",
        }
    ),
    optional=frozenset(
        {
            "run_key",
            "projects",
            "project_group",
            "pinned",
            "next_pending_session_id",
            "last_activity_at",
            "last_activity_source",
            "completed_at",
            "pause_reason",
            "harness",
            "registry_digest",
            "repository_count",
            "multi_repository",
            "warning_count",
            "search_text",
        }
    ),
)

#: One shape per observation view. The payload bodies are the ones
#: :mod:`asl_workbench.store` already reads, so a validated envelope goes
#: straight to ``ingest_*`` with no field renamed in between.
OBSERVATION_SHAPES: Mapping[str, _Shape] = {
    VIEW_HEARTBEAT: _Shape(
        required=frozenset({"view", "observed_at", "bridge_alive"}),
        optional=frozenset(
            {
                "bridge_protocol",
                "contract_set_id",
                "asl_version",
                "degraded_reason",
                "source_generation",
                "projection_age_seconds",
            }
        ),
    ),
    VIEW_SNAPSHOT: _Shape(
        required=frozenset(
            {
                "view",
                "observed_at",
                "digest",
                "row_count",
                "truncated",
                "runs",
            }
        ),
        optional=frozenset(
            {
                "contract_set_id",
                "source_digest",
                "source_generation",
                "failed_projections",
                "projection_age_seconds",
                "staleness_bound_seconds",
                "warnings",
            }
        ),
    ),
    VIEW_HISTORY_BATCH: _Shape(
        required=frozenset(
            {"view", "batch_id", "generated_at", "entry_count", "complete", "entries"}
        ),
        optional=frozenset({"window_start", "window_end", "more", "warnings"}),
    ),
    VIEW_CATALOG_PAGE: _Shape(
        required=frozenset(
            {
                "view",
                "observed_at",
                "generation",
                "complete",
                "final",
                "offset",
                "entry_count",
                "entries",
            }
        ),
        optional=frozenset({"source_generation", "total", "warnings"}),
    ),
}

#: The detail payload. ``complete`` is the completeness this transport carries
#: for a read: a window that was cut is ``partial``, and a reader that renders
#: it as the whole artifact is the failure the field exists to prevent.
DETAIL_SHAPE = _Shape(
    required=frozenset({"detail", "target", "observed_at", "complete"}),
    optional=frozenset(
        {
            "artifact",
            "stream",
            "content",
            "content_bytes",
            "encoding",
            "truncated",
            "offset",
            "next_offset",
            "total",
            "entries",
            "entry_count",
            "reason",
            "warnings",
        }
    ),
)

#: The fields of a detail answer that may hold prose, named explicitly. This is
#: the transport's single exemption from :func:`assert_no_body`, and it is
#: narrow on purpose: a detail envelope is answered online and dropped, so
#: nothing here reaches a database. The test module pins these names against
#: every persist allowlist in :mod:`asl_workbench.observation` -- a field that
#: may carry prose must not share a name with a column.
DETAIL_BODY_FIELDS: frozenset[str] = frozenset({"content", "entries"})

OPERATION_SHAPE = _Shape(
    required=frozenset({"operation_id", "status", "observed_at"}),
    optional=frozenset(
        {
            "method",
            "target",
            "detail_code",
            "dispatched_at",
            "settled_at",
            "deliver_by",
        }
    ),
)

RECEIPT_SHAPE = _Shape(
    required=frozenset({"outcome", "observed_at"}),
    optional=frozenset({"operation_id", "detail_code", "complete"}),
)

#: What each end announces before anything else is exchanged.
#: ``protocols`` lists every transport version the peer can speak, so the two
#: ends can settle on a shared one instead of comparing a single number.
HELLO_SHAPE = _Shape(
    required=frozenset({"protocol", "connector_id", "target_id", "capabilities"}),
    optional=frozenset(
        {"protocols", "contract_set_id", "asl_version", "observation_only"}
    ),
)

PAYLOAD_SHAPES: Mapping[str, _Shape] = {
    KIND_DETAIL: DETAIL_SHAPE,
    KIND_OPERATION: OPERATION_SHAPE,
    KIND_RECEIPT: RECEIPT_SHAPE,
}

# --------------------------------------------------------------------------
# Field validation
# --------------------------------------------------------------------------

#: An identifier: something that may be logged, compared and used as a key.
#: No whitespace, no punctuation a path separator could hide in.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: The digests :mod:`asl_workbench.catalog` produces.
_KEY = re.compile(rf"^[0-9a-f]{{{KEY_CHARS}}}$")

#: ASL's dotted session numbering.
_SESSION_ID = re.compile(r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")


def refuse_non_finite(value: str) -> float:
    """Refuse ``NaN`` and the infinities rather than decoding them.

    :mod:`json` accepts all three by default. None survives a round trip
    through storage or a browser, so a frame containing one is malformed here
    rather than a row that fails later.
    """
    raise TransportError(ERROR_INVALID_FIELD, f"non-finite number: {value}")


def _fail(code: str, where: str, detail: str) -> TransportError:
    return TransportError(code, f"{where}: {detail}")


def check_shape(
    document: Mapping[str, Any], shape: _Shape, where: str
) -> Mapping[str, Any]:
    """Refuse any name the shape does not declare, and any it requires."""
    names = set(document)
    unknown = sorted(names - shape.allowed)
    if unknown:
        raise _fail(ERROR_UNKNOWN_FIELD, where, ", ".join(unknown))
    missing = sorted(shape.required - names)
    if missing:
        raise _fail(ERROR_MISSING_FIELD, where, ", ".join(missing))
    return document


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(ERROR_INVALID_FIELD, where, "expected an object")
    for name in value:
        if not isinstance(name, str):
            raise _fail(ERROR_INVALID_FIELD, where, "expected string keys")
    return value


def _sequence(value: Any, where: str, *, limit: int) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a list")
    if len(value) > limit:
        raise _fail(ERROR_FRAME_TOO_LARGE, where, f"more than {limit} entries")
    return value


def _text(value: Any, where: str, *, limit: int = MAX_LABEL_CHARS) -> str:
    if not isinstance(value, str):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a string")
    if len(value) > limit:
        raise _fail(ERROR_INVALID_FIELD, where, f"longer than {limit} characters")
    return value


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise _fail(ERROR_INVALID_FIELD, where, "expected an identifier")
    return value


def _key(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _KEY.match(value):
        raise _fail(ERROR_INVALID_FIELD, where, f"expected {KEY_CHARS} hex characters")
    return value


def _word(value: Any, where: str, vocabulary: Iterable[str]) -> str:
    allowed = tuple(vocabulary)
    if value not in allowed:
        raise _fail(
            ERROR_INVALID_FIELD, where, f"not one of {len(allowed)} known words"
        )
    assert isinstance(value, str)
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(ERROR_INVALID_FIELD, where, "expected true or false")
    return value


def _count(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(ERROR_INVALID_FIELD, where, "expected an integer")
    if value < minimum:
        raise _fail(ERROR_INVALID_FIELD, where, f"below {minimum}")
    return value


def _number(value: Any, where: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a finite number")
    if minimum is not None and number < minimum:
        raise _fail(ERROR_INVALID_FIELD, where, f"below {minimum}")
    return number


def _optional(document: Mapping[str, Any], name: str, where: str, check: Any) -> None:
    """Apply ``check`` to ``document[name]`` when it is present and not null.

    ``null`` is accepted for every optional field, and means "the Mac could not
    determine this" -- which is why the store's columns are nullable.
    """
    if name not in document:
        return
    value = document[name]
    if value is None:
        return
    check(value, f"{where}.{name}")


def _warnings(value: Any, where: str) -> None:
    for index, warning in enumerate(_sequence(value, where, limit=MAX_WARNINGS)):
        _text(warning, f"{where}[{index}]", limit=MAX_WARNING_CHARS)


def assert_no_body(document: Any, *, where: str) -> None:
    """Refuse any key that names prose, at any depth.

    :data:`asl_workbench.operations.FORBIDDEN_PERSISTED_KEYS` is the list the
    operations module already keeps for records on disk. The same walk runs on
    the wire so a leak is a refused frame rather than a refused write: by the
    time a write is attempted the prose has already crossed the network.
    """
    if isinstance(document, Mapping):
        for name, value in document.items():
            if isinstance(name, str) and name in FORBIDDEN_PERSISTED_KEYS:
                raise _fail(ERROR_BODY_LEAK, where, name)
            assert_no_body(value, where=f"{where}.{name}")
        return
    if isinstance(document, (str, bytes)) or not isinstance(document, Sequence):
        return
    for index, value in enumerate(document):
        assert_no_body(value, where=f"{where}[{index}]")


# --------------------------------------------------------------------------
# Payload validation
# --------------------------------------------------------------------------


def _validate_target(value: Any, where: str) -> None:
    target = _mapping(value, where)
    check_shape(target, TARGET_SHAPE, where)
    _key(target["plan_key"], f"{where}.plan_key")
    _optional(target, "run_key", where, _key)
    _optional(target, "run_id", where, _identifier)
    _optional(target, "question_id", where, _identifier)
    _optional(
        target,
        "session_id",
        where,
        lambda value, at: _word_matching(value, at, _SESSION_ID),
    )


def _word_matching(value: Any, where: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise _fail(ERROR_INVALID_FIELD, where, "malformed")
    return value


def _validate_request(payload: Mapping[str, Any], where: str) -> str:
    request = _word(payload.get("request"), f"{where}.request", REQUEST_METHODS)
    check_shape(payload, REQUEST_SHAPES[request], where)
    _optional(payload, "target", where, _validate_target)
    _optional(payload, "reason", where, _text)
    _optional(payload, "generation", where, _identifier)
    _optional(payload, "offset", where, _count)
    _optional(payload, "limit", where, lambda v, at: _count(v, at, minimum=1))
    _optional(payload, "max_bytes", where, lambda v, at: _count(v, at, minimum=1))
    _optional(payload, "window_start", where, _number)
    _optional(payload, "window_end", where, _number)
    if request == REQUEST_HISTORY:
        start = payload.get("window_start")
        end = payload.get("window_end")
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and float(end) < float(start)
        ):
            raise _fail(ERROR_INVALID_FIELD, where, "window ends before it starts")
    if request == REQUEST_DETAIL:
        _word(payload["detail"], f"{where}.detail", DETAIL_KINDS)
        _optional(
            payload, "artifact", where, lambda v, at: _word(v, at, ARTIFACT_NAMES)
        )
        _optional(payload, "stream", where, lambda v, at: _word(v, at, STREAM_NAMES))
        max_bytes = payload.get("max_bytes")
        if isinstance(max_bytes, int) and max_bytes > MAX_DETAIL_BYTES:
            raise _fail(
                ERROR_FRAME_TOO_LARGE,
                f"{where}.max_bytes",
                f"above {MAX_DETAIL_BYTES}",
            )
    if request == REQUEST_MUTATE:
        _identifier(payload["operation_id"], f"{where}.operation_id")
        _text(
            payload["operation_id"],
            f"{where}.operation_id",
            limit=MAX_OPERATION_ID_CHARS,
        )
        method = payload["method"]
        if method not in OPERATION_METHODS:
            raise _fail(ERROR_FORBIDDEN_METHOD, f"{where}.method", str(method))
        _optional(payload, "action_token", where, _identifier)
        _optional(payload, "reply_ref", where, _identifier)
        _optional(payload, "bridge_token", where, _identifier)
        _optional(payload, "harness", where, _identifier)
    return request


def _validate_run(value: Any, where: str) -> None:
    run = _mapping(value, where)
    check_shape(run, RUN_SHAPE, where)
    _key(run["run_key"], f"{where}.run_key")
    _key(run["plan_key"], f"{where}.plan_key")
    _text(run["project"], f"{where}.project")
    _identifier(run["run_id"], f"{where}.run_id")
    _word(run["primary_state"], f"{where}.primary_state", PRIMARY_STATES)
    _number(run["last_activity_at"], f"{where}.last_activity_at")
    if run["phase"] is not None:
        _word(run["phase"], f"{where}.phase", PHASES)
    if run["urgency_group"] is not None:
        _word(run["urgency_group"], f"{where}.urgency_group", URGENCY_GROUPS)
    actions = _sequence(
        run["actions"], f"{where}.actions", limit=len(OPERATION_METHODS)
    )
    for index, action in enumerate(actions):
        if action not in OPERATION_METHODS:
            raise _fail(
                ERROR_FORBIDDEN_METHOD, f"{where}.actions[{index}]", str(action)
            )
    _optional(run, "action_token", where, _identifier)
    _optional(
        run, "session_id", where, lambda v, at: _word_matching(v, at, _SESSION_ID)
    )
    _optional(run, "harness", where, _identifier)
    _optional(run, "question_id", where, _identifier)
    _optional(run, "question_asked_at", where, _number)
    _optional(run, "unanswered_count", where, _count)
    _optional(run, "run_started_at", where, _number)
    _optional(run, "run_finished_at", where, _number)
    _optional(
        run, "pause_reason", where, lambda v, at: _word(v, at, PAUSE_REASON_CODES)
    )
    _optional(run, "outcome", where, lambda v, at: _word(v, at, SUMMARY_OUTCOMES))
    _optional(run, "warnings", where, _warnings)


def _validate_history_entry(value: Any, where: str) -> None:
    entry = _mapping(value, where)
    check_shape(entry, HISTORY_ENTRY_SHAPE, where)
    _identifier(entry["entry_id"], f"{where}.entry_id")
    _identifier(entry["source_key"], f"{where}.source_key")
    _key(entry["plan_key"], f"{where}.plan_key")
    _key(entry["run_key"], f"{where}.run_key")
    _identifier(entry["run_id"], f"{where}.run_id")
    _word(entry["outcome"], f"{where}.outcome", HISTORY_OUTCOMES)
    _number(entry["recorded_at"], f"{where}.recorded_at")
    _optional(entry, "project", where, _text)
    _optional(
        entry,
        "projects",
        where,
        lambda v, at: [
            _text(name, f"{at}[{index}]")
            for index, name in enumerate(_sequence(v, at, limit=MAX_WARNINGS))
        ],
    )
    _optional(entry, "harness", where, _identifier)
    _optional(entry, "model", where, _text)
    _optional(
        entry, "pause_reason", where, lambda v, at: _word(v, at, PAUSE_REASON_CODES)
    )
    _optional(entry, "run_started_at", where, _number)
    _optional(entry, "run_finished_at", where, _number)
    _optional(entry, "duration_seconds", where, lambda v, at: _number(v, at, minimum=0))
    _optional(entry, "turn_count", where, _count)
    _optional(entry, "completed_session_count", where, _count)
    _optional(entry, "pending_session_count", where, _count)
    _optional(
        entry,
        "completed_session_ids",
        where,
        lambda v, at: [
            _word_matching(value, f"{at}[{index}]", _SESSION_ID)
            for index, value in enumerate(
                _sequence(v, at, limit=MAX_COMPLETED_SESSION_IDS)
            )
        ],
    )
    _optional(entry, "review_completed", where, _flag)
    _optional(
        entry, "review_outcome", where, lambda v, at: _word(v, at, REVIEW_OUTCOMES)
    )
    _optional(entry, "reconstructed", where, _flag)
    _optional(entry, "event_schema_version", where, _count)


def _validate_catalog_entry(value: Any, where: str) -> None:
    entry = _mapping(value, where)
    check_shape(entry, CATALOG_ENTRY_SHAPE, where)
    _key(entry["plan_key"], f"{where}.plan_key")
    _text(entry["title"], f"{where}.title")
    _text(entry["display_label"], f"{where}.display_label")
    _word(entry["primary_state"], f"{where}.primary_state", PRIMARY_STATES)
    _word(
        entry["recommended_action"], f"{where}.recommended_action", RECOMMENDED_ACTIONS
    )
    _count(entry["urgency_rank"], f"{where}.urgency_rank")
    _word(entry["urgency_group"], f"{where}.urgency_group", URGENCY_GROUPS)
    _word(entry["activity_label"], f"{where}.activity_label", ACTIVITY_LABELS)
    _flag(entry["exists"], f"{where}.exists")
    counts = _mapping(entry["counts"], f"{where}.counts")
    check_shape(counts, _Shape(required=frozenset(COUNT_KEYS)), f"{where}.counts")
    for name, total in counts.items():
        _count(total, f"{where}.counts.{name}")
    sources = _sequence(
        entry["sources"], f"{where}.sources", limit=len(DISCOVERY_SOURCES)
    )
    for index, source in enumerate(sources):
        _word(source, f"{where}.sources[{index}]", DISCOVERY_SOURCES)
    _optional(entry, "run_key", where, _key)
    _optional(
        entry,
        "projects",
        where,
        lambda v, at: [
            _text(name, f"{at}[{index}]")
            for index, name in enumerate(_sequence(v, at, limit=MAX_WARNINGS))
        ],
    )
    _optional(entry, "project_group", where, _text)
    _optional(entry, "pinned", where, _flag)
    _optional(
        entry,
        "next_pending_session_id",
        where,
        lambda v, at: _word_matching(v, at, _SESSION_ID),
    )
    _optional(entry, "last_activity_at", where, _text)
    _optional(
        entry,
        "last_activity_source",
        where,
        lambda v, at: _word(v, at, LAST_ACTIVITY_SOURCES),
    )
    _optional(entry, "completed_at", where, _text)
    _optional(
        entry, "pause_reason", where, lambda v, at: _word(v, at, PAUSE_REASON_CODES)
    )
    _optional(entry, "harness", where, _identifier)
    _optional(entry, "registry_digest", where, _identifier)
    _optional(entry, "repository_count", where, _count)
    _optional(entry, "multi_repository", where, _flag)
    _optional(entry, "warning_count", where, _count)
    _optional(entry, "search_text", where, lambda v, at: _text(v, at, limit=4096))


def _validate_observation(payload: Mapping[str, Any], where: str) -> str:
    view = _word(payload.get("view"), f"{where}.view", OBSERVATION_VIEWS)
    check_shape(payload, OBSERVATION_SHAPES[view], where)
    _optional(payload, "warnings", where, _warnings)
    _optional(payload, "contract_set_id", where, _identifier)
    _optional(payload, "source_generation", where, _count)
    _optional(payload, "projection_age_seconds", where, lambda v, at: _number(v, at))
    if view == VIEW_HEARTBEAT:
        _number(payload["observed_at"], f"{where}.observed_at")
        _flag(payload["bridge_alive"], f"{where}.bridge_alive")
        _optional(payload, "bridge_protocol", where, _count)
        _optional(payload, "asl_version", where, _identifier)
        _optional(payload, "degraded_reason", where, _text)
    elif view == VIEW_SNAPSHOT:
        _number(payload["observed_at"], f"{where}.observed_at")
        _identifier(payload["digest"], f"{where}.digest")
        row_count = _count(payload["row_count"], f"{where}.row_count")
        _flag(payload["truncated"], f"{where}.truncated")
        runs = _sequence(payload["runs"], f"{where}.runs", limit=MAX_RUNS_PER_SNAPSHOT)
        for index, run in enumerate(runs):
            _validate_run(run, f"{where}.runs[{index}]")
        if row_count != len(runs):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.row_count", "disagrees with runs"
            )
        _optional(payload, "source_digest", where, _identifier)
        _optional(payload, "failed_projections", where, _count)
        _optional(
            payload,
            "staleness_bound_seconds",
            where,
            lambda v, at: _number(v, at, minimum=0),
        )
    elif view == VIEW_HISTORY_BATCH:
        _identifier(payload["batch_id"], f"{where}.batch_id")
        _number(payload["generated_at"], f"{where}.generated_at")
        _flag(payload["complete"], f"{where}.complete")
        entries = _sequence(
            payload["entries"], f"{where}.entries", limit=MAX_ENTRIES_PER_BATCH
        )
        for index, entry in enumerate(entries):
            _validate_history_entry(entry, f"{where}.entries[{index}]")
        if _count(payload["entry_count"], f"{where}.entry_count") != len(entries):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.entry_count", "disagrees with entries"
            )
        _optional(payload, "window_start", where, _number)
        _optional(payload, "window_end", where, _number)
        _optional(payload, "more", where, _flag)
    else:
        _number(payload["observed_at"], f"{where}.observed_at")
        _identifier(payload["generation"], f"{where}.generation")
        complete = _flag(payload["complete"], f"{where}.complete")
        final = _flag(payload["final"], f"{where}.final")
        _count(payload["offset"], f"{where}.offset")
        entries = _sequence(
            payload["entries"],
            f"{where}.entries",
            limit=MAX_CATALOG_ENTRIES_PER_PAGE,
        )
        for index, entry in enumerate(entries):
            _validate_catalog_entry(entry, f"{where}.entries[{index}]")
        if _count(payload["entry_count"], f"{where}.entry_count") != len(entries):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.entry_count", "disagrees with entries"
            )
        _optional(payload, "total", where, _count)
        # A page that claims to close a generation has to claim the generation
        # was whole: retirement is decided from those two together, and
        # ``final`` alone would retire rows a partial publication never saw.
        if final and not complete:
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.final", "final page is not complete"
            )
    return view


def _validate_detail(payload: Mapping[str, Any], where: str) -> str:
    check_shape(payload, DETAIL_SHAPE, where)
    detail = _word(payload["detail"], f"{where}.detail", DETAIL_KINDS)
    _validate_target(payload["target"], f"{where}.target")
    _number(payload["observed_at"], f"{where}.observed_at")
    _word(payload["complete"], f"{where}.complete", COMPLETENESS_LABELS)
    _optional(payload, "artifact", where, lambda v, at: _word(v, at, ARTIFACT_NAMES))
    _optional(payload, "stream", where, lambda v, at: _word(v, at, STREAM_NAMES))
    _optional(
        payload, "encoding", where, lambda v, at: _word(v, at, ("utf-8", "base64"))
    )
    _optional(payload, "truncated", where, _flag)
    _optional(payload, "offset", where, _count)
    _optional(payload, "next_offset", where, _count)
    _optional(payload, "total", where, _count)
    _optional(
        payload, "reason", where, lambda v, at: _word(v, at, DETAIL_REFUSAL_REASONS)
    )
    _optional(payload, "warnings", where, _warnings)
    content = payload.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise _fail(ERROR_INVALID_FIELD, f"{where}.content", "expected a string")
        measured = len(content.encode("utf-8", "surrogatepass"))
        if measured > MAX_DETAIL_BYTES:
            raise _fail(
                ERROR_FRAME_TOO_LARGE,
                f"{where}.content",
                f"{measured} bytes above {MAX_DETAIL_BYTES}",
            )
        declared = payload.get("content_bytes")
        if (
            declared is not None
            and _count(declared, f"{where}.content_bytes") != measured
        ):
            raise _fail(
                ERROR_INVALID_FIELD,
                f"{where}.content_bytes",
                "disagrees with content",
            )
    elif "content_bytes" in payload and payload["content_bytes"] is not None:
        raise _fail(ERROR_INVALID_FIELD, f"{where}.content_bytes", "no content")
    entries = payload.get("entries")
    if entries is not None:
        listed = _sequence(entries, f"{where}.entries", limit=MAX_DETAIL_ENTRIES)
        for index, entry in enumerate(listed):
            _mapping(entry, f"{where}.entries[{index}]")
        declared_count = payload.get("entry_count")
        if declared_count is not None and _count(
            declared_count, f"{where}.entry_count"
        ) != len(listed):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.entry_count", "disagrees with entries"
            )
    elif "entry_count" in payload and payload["entry_count"] is not None:
        raise _fail(ERROR_INVALID_FIELD, f"{where}.entry_count", "no entries")
    return detail


def _validate_operation(payload: Mapping[str, Any], where: str) -> None:
    check_shape(payload, OPERATION_SHAPE, where)
    _identifier(payload["operation_id"], f"{where}.operation_id")
    _text(
        payload["operation_id"], f"{where}.operation_id", limit=MAX_OPERATION_ID_CHARS
    )
    _word(payload["status"], f"{where}.status", CONNECTOR_REPORTED_STATUSES)
    _number(payload["observed_at"], f"{where}.observed_at")
    _optional(payload, "target", where, _validate_target)
    _optional(payload, "dispatched_at", where, _number)
    _optional(payload, "settled_at", where, _number)
    _optional(payload, "deliver_by", where, _number)
    _optional(
        payload,
        "detail_code",
        where,
        lambda v, at: _word(v, at, OPERATION_DETAIL_CODES),
    )
    method = payload.get("method")
    if method is not None and method not in OPERATION_METHODS:
        raise _fail(ERROR_FORBIDDEN_METHOD, f"{where}.method", str(method))


def _validate_receipt(payload: Mapping[str, Any], where: str) -> None:
    check_shape(payload, RECEIPT_SHAPE, where)
    outcome = _word(payload["outcome"], f"{where}.outcome", RECEIPT_OUTCOMES)
    _number(payload["observed_at"], f"{where}.observed_at")
    _optional(payload, "operation_id", where, _identifier)
    _optional(
        payload, "complete", where, lambda v, at: _word(v, at, COMPLETENESS_LABELS)
    )
    detail_code = payload.get("detail_code")
    if detail_code is not None:
        _word(detail_code, f"{where}.detail_code", RECEIPT_DETAIL_CODES)
    # Anything other than delivery has to say why. "It did not work" without a
    # reason is the receipt that leaves an owner unable to decide what to do.
    if outcome != RECEIPT_DELIVERED and detail_code is None:
        raise _fail(ERROR_MISSING_FIELD, f"{where}.detail_code", outcome)


# --------------------------------------------------------------------------
# Envelope validation
# --------------------------------------------------------------------------


def validate_envelope(
    message: Any,
    *,
    direction: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Check one decoded envelope and return it.

    ``direction`` refuses a kind travelling the wrong way. ``target_id`` is the
    receiver's own identity: an envelope addressed elsewhere is refused here
    rather than acted on, which is what stops a connector paired with one
    deployment from having its uploads accepted by another.
    """
    envelope = _mapping(message, "envelope")
    check_shape(envelope, ENVELOPE_SHAPE, "envelope")

    protocol = envelope["protocol"]
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise _fail(ERROR_INVALID_FIELD, "envelope.protocol", "expected an integer")
    if protocol not in SUPPORTED_TRANSPORT_PROTOCOLS:
        raise _fail(
            ERROR_INCOMPATIBLE_PROTOCOL, "envelope.protocol", f"version {protocol}"
        )

    kind = envelope["kind"]
    if kind not in ENVELOPE_KINDS:
        raise _fail(ERROR_UNKNOWN_KIND, "envelope.kind", str(kind))
    assert isinstance(kind, str)
    if direction is not None:
        if direction not in KINDS_BY_DIRECTION:
            raise ValueError(f"unknown direction: {direction!r}")
        if kind not in KINDS_BY_DIRECTION[direction]:
            raise _fail(
                ERROR_UNKNOWN_KIND, "envelope.kind", f"{kind} cannot travel {direction}"
            )

    capability = _word(
        envelope["capability"], "envelope.capability", TRANSPORT_CAPABILITIES
    )
    if capability not in CAPABILITIES_BY_KIND[kind]:
        raise _fail(
            ERROR_UNSUPPORTED_CAPABILITY,
            "envelope.capability",
            f"{capability} on a {kind}",
        )

    _identifier(envelope["connector_id"], "envelope.connector_id")
    addressed = _identifier(envelope["target_id"], "envelope.target_id")
    if target_id is not None and addressed != target_id:
        raise _fail(ERROR_WRONG_TARGET, "envelope.target_id", addressed)

    sent_at = _number(envelope["sent_at"], "envelope.sent_at", minimum=0)

    request_id = envelope.get("request_id")
    if request_id is None:
        if kind in REQUEST_ID_REQUIRED:
            raise _fail(ERROR_MISSING_FIELD, "envelope", "request_id")
    else:
        if kind not in REQUEST_ID_REQUIRED | REQUEST_ID_OPTIONAL:
            raise _fail(
                ERROR_UNKNOWN_FIELD, "envelope.request_id", f"not carried by a {kind}"
            )
        _identifier(request_id, "envelope.request_id")

    deadline = envelope.get("deadline")
    if deadline is None:
        if kind in DEADLINE_KINDS:
            raise _fail(ERROR_MISSING_FIELD, "envelope", "deadline")
    else:
        if kind not in DEADLINE_KINDS:
            raise _fail(
                ERROR_UNKNOWN_FIELD, "envelope.deadline", f"not carried by a {kind}"
            )
        # A deadline already behind the moment of sending describes work nobody
        # could ever have attempted, and a deadline hours out holds a mutation
        # scope open for as long. Both are refused at composition time.
        moment = _number(deadline, "envelope.deadline", minimum=0)
        if moment < sent_at:
            raise _fail(ERROR_DEADLINE, "envelope.deadline", "already passed")
        if moment - sent_at > MAX_REQUEST_LIFETIME_SECONDS:
            raise _fail(
                ERROR_DEADLINE,
                "envelope.deadline",
                f"further than {MAX_REQUEST_LIFETIME_SECONDS:.0f}s out",
            )

    payload = _mapping(envelope["payload"], "payload")

    # Walked before the payload is shaped, so a frame carrying question prose
    # is refused as a body leak rather than as an unknown field. The closed
    # shapes below would reject it either way; only this order says *why*, and
    # "you tried to send a body" is the refusal a connector author can act on.
    # The detail envelope is the one answer that may carry prose, and it is
    # never written down, so it is exempt by declaration.
    if kind != KIND_DETAIL:
        assert_no_body(payload, where="payload")

    if kind == KIND_REQUEST:
        request = _validate_request(payload, "payload")
        expected = CAPABILITY_BY_REQUEST[request]
    elif kind == KIND_OBSERVATION:
        view = _validate_observation(payload, "payload")
        expected = CAPABILITY_BY_VIEW[view]
    elif kind == KIND_DETAIL:
        _validate_detail(payload, "payload")
        expected = CAPABILITY_DETAIL
    elif kind == KIND_OPERATION:
        _validate_operation(payload, "payload")
        expected = CAPABILITY_MUTATE
    else:
        _validate_receipt(payload, "payload")
        expected = capability

    if capability != expected:
        raise _fail(
            ERROR_INVALID_FIELD,
            "envelope.capability",
            f"{capability} does not match the payload",
        )

    return dict(envelope)


def validate_frame(
    raw: bytes,
    *,
    direction: str | None = None,
    target_id: str | None = None,
    limit: int = MAX_FRAME_BYTES,
) -> dict[str, Any]:
    """Decode and check one frame, measuring it before parsing it."""
    if len(raw) > limit:
        raise _fail(ERROR_FRAME_TOO_LARGE, "frame", f"{len(raw)} bytes above {limit}")
    try:
        message = json.loads(raw, parse_constant=refuse_non_finite)
    except TransportError:
        raise
    except (UnicodeDecodeError, ValueError) as error:
        raise _fail(ERROR_MALFORMED, "frame", str(error)) from error
    return validate_envelope(message, direction=direction, target_id=target_id)


def encode_frame(envelope: Mapping[str, Any], *, limit: int = MAX_FRAME_BYTES) -> bytes:
    """Render one envelope, refusing a frame the far end would have to reject.

    Measuring on the way out means an oversized answer is a local error the
    connector can page around, rather than a refusal discovered after the bytes
    have already crossed the network.
    """
    try:
        raw = json.dumps(
            envelope, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _fail(ERROR_MALFORMED, "frame", str(error)) from error
    if len(raw) > limit:
        raise _fail(ERROR_FRAME_TOO_LARGE, "frame", f"{len(raw)} bytes above {limit}")
    return raw


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def envelope(
    *,
    kind: str,
    capability: str,
    connector_id: str,
    target_id: str,
    sent_at: float,
    payload: Mapping[str, Any],
    request_id: str | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Compose one envelope and validate it before anybody sees it.

    Composition goes through the same checks reception does, so a producer
    cannot emit a frame its own receiver would refuse.
    """
    document: dict[str, Any] = {
        "protocol": TRANSPORT_PROTOCOL_VERSION,
        "kind": kind,
        "capability": capability,
        "connector_id": connector_id,
        "target_id": target_id,
        "sent_at": sent_at,
        "payload": dict(payload),
    }
    if request_id is not None:
        document["request_id"] = request_id
    if deadline is not None:
        document["deadline"] = deadline
    return validate_envelope(document)


def request_envelope(
    *,
    payload: Mapping[str, Any],
    connector_id: str,
    target_id: str,
    sent_at: float,
    request_id: str,
    deadline: float,
) -> dict[str, Any]:
    """Compose a request, taking its capability from the method it names."""
    method = payload.get("request")
    if method not in CAPABILITY_BY_REQUEST:
        raise _fail(ERROR_INVALID_FIELD, "payload.request", str(method))
    assert isinstance(method, str)
    return envelope(
        kind=KIND_REQUEST,
        capability=CAPABILITY_BY_REQUEST[method],
        connector_id=connector_id,
        target_id=target_id,
        sent_at=sent_at,
        payload=payload,
        request_id=request_id,
        deadline=deadline,
    )


def observation_envelope(
    *,
    payload: Mapping[str, Any],
    connector_id: str,
    target_id: str,
    sent_at: float,
) -> dict[str, Any]:
    """Compose an observation, taking its capability from the view it carries."""
    view = payload.get("view")
    if view not in CAPABILITY_BY_VIEW:
        raise _fail(ERROR_INVALID_FIELD, "payload.view", str(view))
    assert isinstance(view, str)
    return envelope(
        kind=KIND_OBSERVATION,
        capability=CAPABILITY_BY_VIEW[view],
        connector_id=connector_id,
        target_id=target_id,
        sent_at=sent_at,
        payload=payload,
    )


def detail_envelope(
    *,
    payload: Mapping[str, Any],
    connector_id: str,
    target_id: str,
    sent_at: float,
    request_id: str,
) -> dict[str, Any]:
    """Compose a detail answer to one correlated read."""
    return envelope(
        kind=KIND_DETAIL,
        capability=CAPABILITY_DETAIL,
        connector_id=connector_id,
        target_id=target_id,
        sent_at=sent_at,
        payload=payload,
        request_id=request_id,
    )


def operation_envelope(
    *,
    payload: Mapping[str, Any],
    connector_id: str,
    target_id: str,
    sent_at: float,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Compose one operation state report."""
    return envelope(
        kind=KIND_OPERATION,
        capability=CAPABILITY_MUTATE,
        connector_id=connector_id,
        target_id=target_id,
        sent_at=sent_at,
        payload=payload,
        request_id=request_id,
    )


def receipt_envelope(
    *,
    payload: Mapping[str, Any],
    capability: str,
    connector_id: str,
    target_id: str,
    sent_at: float,
    request_id: str,
) -> dict[str, Any]:
    """Compose a receipt for one correlated request."""
    return envelope(
        kind=KIND_RECEIPT,
        capability=capability,
        connector_id=connector_id,
        target_id=target_id,
        sent_at=sent_at,
        payload=payload,
        request_id=request_id,
    )


# --------------------------------------------------------------------------
# Compatibility
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportCompatibility:
    """What two deployments may do with each other, and why.

    ``compatible`` is the summary; the two permissions are what the code acts
    on. They come apart on purpose: a peer on an understood transport version
    whose contract set this build has never seen still produces observations
    worth recording, so ``reads_allowed`` stays true while
    ``mutations_allowed`` does not. ``mutation_refusal`` carries the single
    code a receipt should cite, chosen by a fixed precedence so two ends of the
    same mismatch report it identically.
    """

    compatible: bool
    reads_allowed: bool
    mutations_allowed: bool
    protocol: int | None
    connector_id: str | None
    contract_set_id: str | None
    asl_version: str | None
    granted: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    missing: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()
    mutation_refusal: str | None = None
    notes: tuple[str, ...] = ()

    def permits(self, capability: str) -> bool:
        """Whether an envelope claiming ``capability`` may be acted on."""
        if not self.reads_allowed or capability not in self.granted:
            return False
        if capability in MUTATING_CAPABILITIES:
            return self.mutations_allowed
        return True


#: The order a mutation refusal is reported in. An incompatible version
#: explains everything downstream of it, so it is named first; "observation
#: only" is last because it is a deliberate setting rather than a mismatch, and
#: reporting it over a real incompatibility would hide the incompatibility.
_MUTATION_REFUSAL_PRECEDENCE: tuple[str, ...] = (
    ERROR_INCOMPATIBLE_PROTOCOL,
    ERROR_WRONG_TARGET,
    ERROR_INCOMPATIBLE_CONTRACT,
    ERROR_UNSUPPORTED_CAPABILITY,
    ERROR_OBSERVATION_ONLY,
)


def negotiate_protocol(offered: Iterable[Any]) -> int | None:
    """The highest transport version both ends can speak, or ``None``."""
    shared = {
        version
        for version in offered
        if isinstance(version, int)
        and not isinstance(version, bool)
        and version in SUPPORTED_TRANSPORT_PROTOCOLS
    }
    return max(shared) if shared else None


def validate_hello(message: Any) -> dict[str, Any]:
    """Check one announcement. Closed, like every other document here."""
    hello = _mapping(message, "hello")
    check_shape(hello, HELLO_SHAPE, "hello")
    protocol = hello["protocol"]
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise _fail(ERROR_INVALID_FIELD, "hello.protocol", "expected an integer")
    _identifier(hello["connector_id"], "hello.connector_id")
    _identifier(hello["target_id"], "hello.target_id")
    capabilities = _sequence(
        hello["capabilities"], "hello.capabilities", limit=MAX_CAPABILITIES
    )
    for index, capability in enumerate(capabilities):
        _identifier(capability, f"hello.capabilities[{index}]")
    protocols = hello.get("protocols")
    if protocols is not None:
        for index, version in enumerate(
            _sequence(protocols, "hello.protocols", limit=MAX_PROTOCOLS)
        ):
            if isinstance(version, bool) or not isinstance(version, int):
                raise _fail(
                    ERROR_INVALID_FIELD,
                    f"hello.protocols[{index}]",
                    "expected an integer",
                )
    _optional(hello, "contract_set_id", "hello", _identifier)
    _optional(hello, "asl_version", "hello", _identifier)
    _optional(hello, "observation_only", "hello", _flag)
    return dict(hello)


def evaluate_hello(
    message: Any,
    *,
    policy: ObservationPolicy,
    target_id: str | None = None,
    supported_contract_set_ids: Sequence[str] | None = None,
) -> TransportCompatibility:
    """Decide what a peer may do, keeping versions and capabilities apart.

    An unusable *version* is fatal: without a shared transport number neither
    end can read the other's frames, so nothing is permitted and the verdict
    says so once. An unusable *capability* -- one this build does not know, one
    the peer never offered, or a contract set outside
    ``supported_contract_set_ids`` -- narrows what is permitted and leaves the
    rest working.
    """
    hello = validate_hello(message)
    policy = require_policy(policy)
    supported = tuple(
        supported_contract_set_ids
        if supported_contract_set_ids is not None
        else SUPPORTED_CONTRACT_SET_IDS
    )

    offered_protocols = hello.get("protocols") or [hello["protocol"]]
    protocol = negotiate_protocol(offered_protocols)
    offered = frozenset(hello["capabilities"])
    granted = offered & frozenset(TRANSPORT_CAPABILITIES)
    unknown = offered - frozenset(TRANSPORT_CAPABILITIES)
    missing = REQUIRED_CAPABILITIES - offered
    contract_set_id = hello.get("contract_set_id")

    reasons: list[str] = []
    notes: list[str] = []

    if protocol is None:
        reasons.append(ERROR_INCOMPATIBLE_PROTOCOL)
    if target_id is not None and hello["target_id"] != target_id:
        reasons.append(ERROR_WRONG_TARGET)
    if contract_set_id not in supported:
        reasons.append(ERROR_INCOMPATIBLE_CONTRACT)
    # What the peer cannot do, as opposed to what this deployment declines to
    # ask for. A connector that never offered ``mutate`` is the first; a
    # deployment set to observe only is the second, and they are reported
    # separately because only the second is somebody's decision to reverse.
    if missing or CAPABILITY_MUTATE not in granted:
        reasons.append(ERROR_UNSUPPORTED_CAPABILITY)
    if not policy.replies_permitted or bool(hello.get("observation_only")):
        reasons.append(ERROR_OBSERVATION_ONLY)
    if unknown:
        # Named, never fatal: a newer connector offering something this build
        # has not learned yet is still a connector whose observations count.
        notes.append(f"unknown capabilities: {', '.join(sorted(unknown))}")

    fatal = protocol is None or ERROR_WRONG_TARGET in reasons
    reads_allowed = not fatal
    compatible = reads_allowed and not missing and contract_set_id in supported
    mutation_refusal = next(
        (code for code in _MUTATION_REFUSAL_PRECEDENCE if code in reasons), None
    )
    mutations_allowed = compatible and mutation_refusal is None

    return TransportCompatibility(
        compatible=compatible,
        reads_allowed=reads_allowed,
        mutations_allowed=mutations_allowed,
        protocol=protocol,
        connector_id=hello["connector_id"],
        contract_set_id=contract_set_id,
        asl_version=hello.get("asl_version"),
        granted=frozenset(granted),
        unknown=frozenset(unknown),
        missing=frozenset(missing),
        reasons=tuple(reasons),
        mutation_refusal=mutation_refusal,
        notes=tuple(notes),
    )


def authorize(
    document: Mapping[str, Any], compatibility: TransportCompatibility
) -> Mapping[str, Any]:
    """Refuse an envelope the negotiated compatibility does not permit.

    This is where "an incompatible deployment cannot execute mutations" stops
    being a property somebody remembers and becomes a refusal on the path a
    mutation takes. A validated envelope is not yet an authorized one: shape
    says the frame is readable, and this says the pairing may act on it.
    """
    capability = document.get("capability")
    if not isinstance(capability, str) or capability not in TRANSPORT_CAPABILITIES:
        raise _fail(ERROR_INVALID_FIELD, "envelope.capability", str(capability))

    if not compatibility.reads_allowed:
        code = (
            ERROR_WRONG_TARGET
            if ERROR_WRONG_TARGET in compatibility.reasons
            else ERROR_INCOMPATIBLE_PROTOCOL
        )
        raise _fail(code, "envelope", "the deployments are not compatible")

    connector_id = document.get("connector_id")
    if (
        compatibility.connector_id is not None
        and connector_id != compatibility.connector_id
    ):
        raise _fail(ERROR_WRONG_TARGET, "envelope.connector_id", str(connector_id))

    if capability not in compatibility.granted:
        raise _fail(ERROR_UNSUPPORTED_CAPABILITY, "envelope.capability", capability)

    if capability in MUTATING_CAPABILITIES and not compatibility.mutations_allowed:
        raise _fail(
            compatibility.mutation_refusal or ERROR_UNSUPPORTED_CAPABILITY,
            "envelope.capability",
            capability,
        )

    return document


__all__ = [
    "CAPABILITIES_BY_KIND",
    "CAPABILITY_BY_REQUEST",
    "CAPABILITY_BY_VIEW",
    "CAPABILITY_CATALOG",
    "CAPABILITY_DETAIL",
    "CAPABILITY_HISTORY",
    "CAPABILITY_MUTATE",
    "CAPABILITY_OBSERVE",
    "CATALOG_ENTRY_SHAPE",
    "CONNECTOR_REPORTED_STATUSES",
    "DEADLINE_KINDS",
    "DETAIL_BODY_FIELDS",
    "DETAIL_ESCAPE_FACTOR",
    "DETAIL_REFUSAL_REASONS",
    "DETAIL_SHAPE",
    "DIRECTION_BY_KIND",
    "DIRECTION_DOWN",
    "DIRECTION_UP",
    "ENVELOPE_KINDS",
    "ENVELOPE_SHAPE",
    "ERROR_BODY_LEAK",
    "ERROR_DEADLINE",
    "ERROR_FORBIDDEN_METHOD",
    "ERROR_FRAME_TOO_LARGE",
    "ERROR_INCOMPATIBLE_CONTRACT",
    "ERROR_INCOMPATIBLE_PROTOCOL",
    "ERROR_INVALID_FIELD",
    "ERROR_MALFORMED",
    "ERROR_MISSING_FIELD",
    "ERROR_OBSERVATION_ONLY",
    "ERROR_UNKNOWN_FIELD",
    "ERROR_UNAUTHORIZED",
    "ERROR_UNKNOWN_KIND",
    "ERROR_UNSUPPORTED_CAPABILITY",
    "ERROR_WRONG_TARGET",
    "HELLO_SHAPE",
    "HISTORY_ENTRY_SHAPE",
    "KINDS_BY_DIRECTION",
    "KIND_DETAIL",
    "KIND_OBSERVATION",
    "KIND_OPERATION",
    "KIND_RECEIPT",
    "KIND_REQUEST",
    "MAX_CATALOG_ENTRIES_PER_PAGE",
    "MAX_DETAIL_BYTES",
    "MAX_DETAIL_ENTRIES",
    "MAX_ENTRIES_PER_BATCH",
    "MAX_FRAME_BYTES",
    "MAX_REQUEST_LIFETIME_SECONDS",
    "MAX_RUNS_PER_SNAPSHOT",
    "MAX_WARNING_CHARS",
    "MUTATING_CAPABILITIES",
    "OBSERVATION_SHAPES",
    "OBSERVATION_VIEWS",
    "OPERATION_SHAPE",
    "PAUSE_REASON_CODES",
    "PAYLOAD_SHAPES",
    "PHASES",
    "RECEIPT_DELIVERED",
    "RECEIPT_DETAIL_CODES",
    "RECEIPT_EXPIRED",
    "RECEIPT_OUTCOMES",
    "RECEIPT_REFUSED",
    "RECEIPT_SHAPE",
    "RECEIPT_UNCONFIRMED",
    "REQUEST_CATALOG",
    "REQUEST_DETAIL",
    "REQUEST_HISTORY",
    "REQUEST_ID_OPTIONAL",
    "REQUEST_ID_REQUIRED",
    "REQUEST_METHODS",
    "REQUEST_MUTATE",
    "REQUEST_OBSERVE",
    "REQUEST_SHAPES",
    "REQUIRED_CAPABILITIES",
    "RUN_SHAPE",
    "STREAM_NAMES",
    "SUMMARY_OUTCOMES",
    "SUPPORTED_TRANSPORT_PROTOCOLS",
    "TARGET_SHAPE",
    "TRANSPORT_CAPABILITIES",
    "TRANSPORT_ERROR_CODES",
    "TRANSPORT_PROTOCOL_VERSION",
    "TRANSPORT_REFUSAL_CODES",
    "TransportCompatibility",
    "TransportError",
    "VIEW_CATALOG_PAGE",
    "VIEW_HEARTBEAT",
    "VIEW_HISTORY_BATCH",
    "VIEW_SNAPSHOT",
    "assert_no_body",
    "authorize",
    "check_shape",
    "detail_envelope",
    "encode_frame",
    "envelope",
    "evaluate_hello",
    "negotiate_protocol",
    "observation_envelope",
    "operation_envelope",
    "receipt_envelope",
    "refuse_non_finite",
    "request_envelope",
    "validate_envelope",
    "validate_frame",
    "validate_hello",
]
