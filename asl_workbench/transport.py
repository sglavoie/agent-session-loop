"""The Workbench's own wire format, versioned apart from everything it carries.

Five closed envelopes move between a Mac connector and the Pi service:

* a **request** the service hands down -- publish a catalog page, publish
  history, refresh observations, read one detail, submit one mutation;
* an **observation** the connector uploads, which is the cache-safe material
  :mod:`asl_workbench.store_workbench` retains;
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

The implementation is split by concern into ``transport_vocabulary`` (closed
vocabularies, bounds, error codes), ``transport_shapes`` (field sets),
``transport_checks`` (field validators), ``transport_payloads`` (per-kind
payload checks), ``transport_envelopes`` (envelope validation, framing,
builders) and ``transport_compat`` (hello, negotiation, authorization). This
module re-exports their public names so ``asl_workbench.transport`` stays the
one import path.
"""

# ruff: noqa: F403, F405
from __future__ import annotations

from .catalog import (
    ACTIVITY_LABELS as ACTIVITY_LABELS,
)
from .catalog import (
    COUNT_KEYS as COUNT_KEYS,
)
from .catalog import (
    DISCOVERY_SOURCES as DISCOVERY_SOURCES,
)
from .catalog import (
    KEY_CHARS as KEY_CHARS,
)
from .catalog import (
    LAST_ACTIVITY_SOURCES as LAST_ACTIVITY_SOURCES,
)
from .catalog import (
    PRIMARY_STATES as PRIMARY_STATES,
)
from .catalog import (
    RECOMMENDED_ACTIONS as RECOMMENDED_ACTIONS,
)
from .catalog import (
    URGENCY_GROUPS as URGENCY_GROUPS,
)
from .details import (
    ARTIFACT_NAMES as ARTIFACT_NAMES,
)
from .details import (
    DETAIL_KINDS as DETAIL_KINDS,
)
from .details import (
    MAX_EVENT_LIMIT as MAX_EVENT_LIMIT,
)
from .details import (
    REASON_INVALID_PLAN as REASON_INVALID_PLAN,
)
from .details import (
    REASON_INVALID_REQUEST as REASON_INVALID_REQUEST,
)
from .details import (
    REASON_MALFORMED_PLAN as REASON_MALFORMED_PLAN,
)
from .details import (
    REASON_NOT_FOUND as REASON_NOT_FOUND,
)
from .details import (
    REASON_OS_ERROR as REASON_OS_ERROR,
)
from .details import (
    STREAM_ARTIFACTS as STREAM_ARTIFACTS,
)
from .history import (
    HISTORY_OUTCOMES as HISTORY_OUTCOMES,
)
from .history import (
    PAUSE_REASON_OTHER as PAUSE_REASON_OTHER,
)
from .history import (
    PAUSE_REASONS as PAUSE_REASONS,
)
from .history import (
    REVIEW_OUTCOMES as REVIEW_OUTCOMES,
)
from .observation import (
    COMPLETENESS_LABELS as COMPLETENESS_LABELS,
)
from .observation import (
    MAX_LABEL_CHARS as MAX_LABEL_CHARS,
)
from .observation import (
    MAX_WARNINGS as MAX_WARNINGS,
)
from .observation import (
    SUPPORTED_CONTRACT_SET_IDS as SUPPORTED_CONTRACT_SET_IDS,
)
from .observation import (
    ObservationPolicy as ObservationPolicy,
)
from .observation import (
    require_policy as require_policy,
)
from .operations import (
    FORBIDDEN_PERSISTED_KEYS as FORBIDDEN_PERSISTED_KEYS,
)
from .operations import (
    MAX_OPERATION_ID_CHARS as MAX_OPERATION_ID_CHARS,
)
from .operations import (
    METHOD_ARCHIVE_DELETE as METHOD_ARCHIVE_DELETE,
)
from .operations import (
    METHOD_ARCHIVE_RESTORE as METHOD_ARCHIVE_RESTORE,
)
from .operations import (
    METHOD_ARCHIVE_SETTINGS as METHOD_ARCHIVE_SETTINGS,
)
from .operations import (
    OPERATION_ACCEPTED as OPERATION_ACCEPTED,
)
from .operations import (
    OPERATION_CONSUMED as OPERATION_CONSUMED,
)
from .operations import (
    OPERATION_DETAIL_CODES as OPERATION_DETAIL_CODES,
)
from .operations import (
    OPERATION_DISPATCHED as OPERATION_DISPATCHED,
)
from .operations import (
    OPERATION_EXPIRED as OPERATION_EXPIRED,
)
from .operations import (
    OPERATION_METHODS as OPERATION_METHODS,
)
from .operations import (
    OPERATION_REJECTED as OPERATION_REJECTED,
)
from .operations import (
    OPERATION_SUPERSEDED as OPERATION_SUPERSEDED,
)
from .operations import (
    OPERATION_UNCONFIRMED as OPERATION_UNCONFIRMED,
)
from .transport_checks import *
from .transport_compat import *
from .transport_envelopes import *
from .transport_payloads import *
from .transport_shapes import *
from .transport_vocabulary import *

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
