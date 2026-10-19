"""Remote Workbench service provider.

Query handlers, mutation admission, and lane contracts live in the sibling
``service_*`` modules; this module owns dispatch and connector upload routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .http import (
    OP_ARCHIVE_LIST,
    OP_COMMAND_REDEEM,
    OP_HEALTH,
    OP_HISTORY,
    OP_PLAN,
    OP_QUEUE,
    OP_SUMMARY,
    SECURITY_HEADERS,
    Call,
    HttpRefusal,
    Response,
    unavailable_from,
)
from .service_models import (
    _DEFAULT_UNAVAILABLE,
    _NO_BODY_LANE,
    _NO_READ_LANE,
    _NO_SUBMISSION_LANE,
    _NO_UPLOAD_LANE,
    DETAIL_OPERATIONS,
    MUTATION_METHODS,
    UNAVAILABLE_REASONS,
    UPLOAD_OPERATIONS,
)
from .service_models import READ_STATUSES as READ_STATUSES
from .service_models import DetailLane as DetailLane
from .service_models import MutationBodyLane as MutationBodyLane
from .service_models import SubmissionLane as SubmissionLane
from .service_models import UploadLane as UploadLane
from .service_mutations import ServiceMutations
from .service_queries import ServiceQueries

__all__ = [
    "DETAIL_OPERATIONS",
    "MUTATION_METHODS",
    "READ_STATUSES",
    "UNAVAILABLE_REASONS",
    "UPLOAD_OPERATIONS",
    "DetailLane",
    "RemoteProvider",
    "SubmissionLane",
    "UploadLane",
]


class RemoteProvider(ServiceQueries, ServiceMutations):
    """Remote Workbench dispatch using a retained store and optional connector lanes."""

    def unavailable(self, operation: str) -> str | None:
        supported = set(_STORE_OPERATIONS)
        if self._reads is not None:
            supported.update(DETAIL_OPERATIONS)
            supported.add(OP_ARCHIVE_LIST)
        if self._mutations is not None:
            supported.update(MUTATION_METHODS)
        if self._uploads is not None:
            supported.update(UPLOAD_OPERATIONS)
        if self._bodies is not None:
            supported.add(OP_COMMAND_REDEEM)
        reasons = dict(UNAVAILABLE_REASONS)
        if self._reads is None:
            for operation_name in DETAIL_OPERATIONS:
                reasons.setdefault(operation_name, _NO_READ_LANE)
            reasons.setdefault(OP_ARCHIVE_LIST, _NO_READ_LANE)
        if self._mutations is None:
            for operation_name in MUTATION_METHODS:
                reasons.setdefault(operation_name, _NO_SUBMISSION_LANE)
        if self._uploads is None:
            for operation_name in UPLOAD_OPERATIONS:
                reasons.setdefault(operation_name, _NO_UPLOAD_LANE)
        if self._bodies is None:
            reasons.setdefault(OP_COMMAND_REDEEM, _NO_BODY_LANE)
        return unavailable_from(
            supported, reasons=reasons, default=_DEFAULT_UNAVAILABLE
        )(operation)

    def invoke(self, call: Call) -> Response:
        operation = call.operation
        if operation in UPLOAD_OPERATIONS:
            return self._ingest(call)
        if operation == OP_COMMAND_REDEEM:
            return self._redeem(call)
        if operation in DETAIL_OPERATIONS:
            return self._detail(call, DETAIL_OPERATIONS[operation])
        if operation == OP_ARCHIVE_LIST:
            return self._archive_list(call)
        if operation in MUTATION_METHODS:
            return self._submit(call, MUTATION_METHODS[operation])
        handler = _STORE_OPERATIONS.get(operation)
        if handler is None:  # pragma: no cover - `unavailable` refused it first
            raise HttpRefusal(
                503,
                "operation-unavailable",
                _DEFAULT_UNAVAILABLE,
                details={"operation": operation, "mode": self.mode},
            )
        return handler(self, call)

    def _ingest(self, call: Call) -> Response:
        """Take one uploaded frame in and answer with whatever goes back down.

        The answer is the *bare envelope*, not this module's ``ok`` wrapper.
        That is the transport's shape rather than the API's, and it has to be:
        the connector hands the response body straight to
        :func:`~asl_workbench.transport.validate_frame`, so anything wrapped
        around it would be a frame the far end refuses as undecodable. An
        acknowledgement with nothing to carry is a body-less ``202``, which is
        the same statement with no frame in it.
        """
        lane = self._uploads
        if lane is None:  # pragma: no cover - `unavailable` refused it first
            raise HttpRefusal(503, "operation-unavailable", _NO_UPLOAD_LANE)
        carried = lane.ingest(
            call.body, now=self._clock(), subject=call.principal.subject
        )
        if carried is None:
            return Response(status=202, headers=dict(SECURITY_HEADERS))
        return Response.json(200, carried)

    def _redeem(self, call: Call) -> Response:
        """Exchange a dispatched command's capability for its volatile body."""
        lane = self._bodies
        if lane is None:  # pragma: no cover - `unavailable` refused it first
            raise HttpRefusal(503, "operation-unavailable", _NO_BODY_LANE)
        subject = call.principal.subject
        if not subject:
            raise HttpRefusal(403, "wrong-connector", "connector identity is required")
        text = lane.redeem_body(
            call.require_string("operation_id"),
            call.require_string("reply_ref"),
            call.require_string("action_token"),
            connector_id=subject,
            now=self._clock(),
        )
        return Response.success(reason="ok", reply_text=text)


#: The operations answered out of the retained store alone. Held as a table for
#: the same reason the local provider holds one: a route with no entry here is
#: unavailable rather than a dispatch that fails at request time.
_STORE_OPERATIONS: Mapping[str, Callable[[RemoteProvider, Call], Response]] = {
    OP_HEALTH: RemoteProvider._handle_health,
    OP_SUMMARY: RemoteProvider._handle_summary,
    OP_QUEUE: RemoteProvider._handle_queue,
    OP_HISTORY: RemoteProvider._handle_history,
    OP_PLAN: RemoteProvider._handle_plan,
}
