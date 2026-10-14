"""Envelope validation, frame encoding and the envelope builders.

Part of :mod:`asl_workbench.transport`, which re-exports everything here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .transport_checks import (
    _fail,
    _identifier,
    _mapping,
    _number,
    _word,
    assert_no_body,
    check_shape,
    refuse_non_finite,
)
from .transport_payloads import (
    _validate_detail,
    _validate_observation,
    _validate_operation,
    _validate_receipt,
    _validate_request,
)
from .transport_shapes import (
    DEADLINE_KINDS,
    ENVELOPE_SHAPE,
    REQUEST_ID_OPTIONAL,
    REQUEST_ID_REQUIRED,
)
from .transport_vocabulary import (
    CAPABILITIES_BY_KIND,
    CAPABILITY_BY_REQUEST,
    CAPABILITY_BY_VIEW,
    CAPABILITY_DETAIL,
    CAPABILITY_MUTATE,
    ENVELOPE_KINDS,
    ERROR_DEADLINE,
    ERROR_FRAME_TOO_LARGE,
    ERROR_INCOMPATIBLE_PROTOCOL,
    ERROR_INVALID_FIELD,
    ERROR_MALFORMED,
    ERROR_MISSING_FIELD,
    ERROR_UNKNOWN_FIELD,
    ERROR_UNKNOWN_KIND,
    ERROR_UNSUPPORTED_CAPABILITY,
    ERROR_WRONG_TARGET,
    KIND_DETAIL,
    KIND_OBSERVATION,
    KIND_OPERATION,
    KIND_RECEIPT,
    KIND_REQUEST,
    KINDS_BY_DIRECTION,
    MAX_FRAME_BYTES,
    MAX_REQUEST_LIFETIME_SECONDS,
    SUPPORTED_TRANSPORT_PROTOCOLS,
    TRANSPORT_CAPABILITIES,
    TRANSPORT_PROTOCOL_VERSION,
    TransportError,
)

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


__all__ = [
    "detail_envelope",
    "encode_frame",
    "envelope",
    "observation_envelope",
    "operation_envelope",
    "receipt_envelope",
    "request_envelope",
    "validate_envelope",
    "validate_frame",
]
