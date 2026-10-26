"""The protocol session: frame dispatch and the method registry.

Composes the handler layers into :class:`BridgeSession`, the unit the stdio
server and the tests drive.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from . import __version__, intervention
from .agent_mux_projection import CONTRACT_SET_ID
from .bridge_protocol import (
    _METHOD_ORDER,
    CAPABILITY_METHODS,
    ERROR_INVALID_REQUEST,
    ERROR_MALFORMED_JSON,
    ERROR_UNKNOWN_METHOD,
    ERROR_UNSUPPORTED_PROTOCOL,
    EVENT_NAMES,
    MAX_FRAME_BYTES,
    METHOD_HANDSHAKE,
    METHOD_RECOVER_CONVERSATION,
    METHOD_REDIRECT_STATUS,
    MUTATION_METHODS,
    POLL_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    BridgeError,
    _bounded_text,
    _error_response,
    _response,
)
from .bridge_redirect import RedirectHandlers


def hello_payload() -> dict[str, Any]:
    """The server-first frame, carrying the identity both ends must already share.

    ``contract_set_id`` is the whole compatibility story: it is the digest of
    the canonical corpus that defines these frames, the lifecycle hook payload,
    and the projection, so one string pins every component version at once.
    Listing the component schema versions separately would let the two drift
    apart, and the frame is closed against unknown properties precisely so that
    cannot happen.
    """
    return {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "asl_version": __version__,
        "contract_set_id": CONTRACT_SET_ID,
        "capabilities": list(CAPABILITIES),
        "events": list(EVENT_NAMES),
        "mutation_methods": list(MUTATION_METHODS),
        "handshake_required": True,
        "max_frame_bytes": MAX_FRAME_BYTES,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
    }


class BridgeSession(RedirectHandlers):
    """Protocol state for one connected client.

    Split from the transport so the projection, the framing, and the error
    vocabulary are all testable without a subprocess or a real pipe.
    """

    def hello(self) -> dict[str, Any]:
        return hello_payload()

    def handle_frame(self, raw: bytes) -> dict[str, Any] | None:
        """Answer one request frame; never raise for client-caused failures."""
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return _error_response(
                None,
                BridgeError(ERROR_MALFORMED_JSON, f"frame is not valid JSON: {error}"),
            )
        if not isinstance(decoded, Mapping):
            return _error_response(
                None,
                BridgeError(ERROR_INVALID_REQUEST, "frame is not a JSON object"),
            )

        raw_id = decoded.get("id")
        request_id = raw_id if isinstance(raw_id, str) and raw_id else None
        try:
            return _response(request_id, self._dispatch(decoded, request_id))
        except BridgeError as error:
            return _error_response(request_id, error)

    def _dispatch(
        self, request: Mapping[str, Any], request_id: str | None
    ) -> dict[str, Any]:
        if request.get("type") != "request":
            raise BridgeError(
                ERROR_INVALID_REQUEST,
                "frame type must be 'request'",
                details={"type": _bounded_text(request.get("type"))},
            )
        protocol = request.get("protocol")
        if protocol != PROTOCOL_VERSION:
            raise BridgeError(
                ERROR_UNSUPPORTED_PROTOCOL,
                f"protocol {protocol!r} is not supported",
                details={"supported": str(PROTOCOL_VERSION)},
            )
        if request_id is None:
            raise BridgeError(
                ERROR_INVALID_REQUEST, "request id must be a nonempty string"
            )
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise BridgeError(
                ERROR_INVALID_REQUEST, "request method must be a nonempty string"
            )
        params = request.get("params", {})
        if not isinstance(params, Mapping):
            raise BridgeError(ERROR_INVALID_REQUEST, "request params must be an object")

        handler = METHOD_HANDLERS.get(method)
        if handler is None:
            raise BridgeError(
                ERROR_UNKNOWN_METHOD,
                f"unknown method {method!r}",
                details={"method": _bounded_text(method) or ""},
            )
        # Enforced here rather than in each handler so a method added to
        # MUTATION_METHODS is gated by construction instead of by remembering.
        if method in MUTATION_METHODS:
            self._require_mutations_enabled(method)
        elif method in CAPABILITY_METHODS:
            self._require_contract_handshake(method)
        return handler(self, params)


# Registered last so the derived capability list below can only advertise names
# that already have a handler behind them.
METHOD_HANDLERS: dict[
    str, Callable[[BridgeSession, Mapping[str, Any]], dict[str, Any]]
] = {
    METHOD_HANDSHAKE: BridgeSession.session_handshake,
    "runs.snapshot": lambda session, params: session.runs_snapshot(),
    "question.get": BridgeSession.question_get,
    intervention.ACTION_REPLY: BridgeSession.question_reply,
    intervention.ACTION_RESOLVE: BridgeSession.question_resolve,
    intervention.ACTION_RESUME: BridgeSession.run_resume,
    intervention.ACTION_STOP_AFTER_CURRENT: BridgeSession.run_stop_after_current,
    intervention.ACTION_ABORT: BridgeSession.run_abort,
    intervention.ACTION_WORKBENCH: BridgeSession.workbench_ensure,
    intervention.ACTION_REDIRECT: BridgeSession.run_redirect,
    METHOD_REDIRECT_STATUS: BridgeSession.run_redirect_status,
    METHOD_RECOVER_CONVERSATION: BridgeSession.run_recover_conversation,
}


CAPABILITIES: tuple[str, ...] = tuple(
    name for name in _METHOD_ORDER if name in METHOD_HANDLERS
)
