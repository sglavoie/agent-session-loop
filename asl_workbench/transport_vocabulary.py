"""The Workbench wire format's closed vocabularies, bounds and error codes.

Part of :mod:`asl_workbench.transport`, which re-exports everything here.
"""

from __future__ import annotations

from collections.abc import Mapping

from .details import (
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
)
from .operations import (
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DETAIL_CODES,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
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
#: :mod:`asl_workbench.store_types`'s ``KIND_*`` constants exactly, so a validated
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


__all__ = [
    "CAPABILITIES_BY_KIND",
    "CAPABILITY_BY_REQUEST",
    "CAPABILITY_BY_VIEW",
    "CAPABILITY_CATALOG",
    "CAPABILITY_DETAIL",
    "CAPABILITY_HISTORY",
    "CAPABILITY_MUTATE",
    "CAPABILITY_OBSERVE",
    "CONNECTOR_REPORTED_STATUSES",
    "DETAIL_ESCAPE_FACTOR",
    "DETAIL_REFUSAL_REASONS",
    "DIRECTION_BY_KIND",
    "DIRECTION_DOWN",
    "DIRECTION_UP",
    "ENVELOPE_KINDS",
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
    "ERROR_UNAUTHORIZED",
    "ERROR_UNKNOWN_FIELD",
    "ERROR_UNKNOWN_KIND",
    "ERROR_UNSUPPORTED_CAPABILITY",
    "ERROR_WRONG_TARGET",
    "KINDS_BY_DIRECTION",
    "KIND_DETAIL",
    "KIND_OBSERVATION",
    "KIND_OPERATION",
    "KIND_RECEIPT",
    "KIND_REQUEST",
    "MAX_CAPABILITIES",
    "MAX_CATALOG_ENTRIES_PER_PAGE",
    "MAX_COMPLETED_SESSION_IDS",
    "MAX_DETAIL_BYTES",
    "MAX_DETAIL_ENTRIES",
    "MAX_ENTRIES_PER_BATCH",
    "MAX_FRAME_BYTES",
    "MAX_IDENTIFIER_CHARS",
    "MAX_PROTOCOLS",
    "MAX_REQUEST_LIFETIME_SECONDS",
    "MAX_RUNS_PER_SNAPSHOT",
    "MAX_WARNING_CHARS",
    "MUTATING_CAPABILITIES",
    "OBSERVATION_VIEWS",
    "PAUSE_REASON_CODES",
    "PHASES",
    "RECEIPT_DELIVERED",
    "RECEIPT_DETAIL_CODES",
    "RECEIPT_EXPIRED",
    "RECEIPT_OUTCOMES",
    "RECEIPT_REFUSED",
    "RECEIPT_UNCONFIRMED",
    "REQUEST_CATALOG",
    "REQUEST_DETAIL",
    "REQUEST_HISTORY",
    "REQUEST_METHODS",
    "REQUEST_MUTATE",
    "REQUEST_OBSERVE",
    "REQUIRED_CAPABILITIES",
    "STREAM_NAMES",
    "SUMMARY_OUTCOMES",
    "SUPPORTED_TRANSPORT_PROTOCOLS",
    "TRANSPORT_CAPABILITIES",
    "TRANSPORT_ERROR_CODES",
    "TRANSPORT_PROTOCOL_VERSION",
    "TRANSPORT_REFUSAL_CODES",
    "TransportError",
    "VIEW_CATALOG_PAGE",
    "VIEW_HEARTBEAT",
    "VIEW_HISTORY_BATCH",
    "VIEW_SNAPSHOT",
]
