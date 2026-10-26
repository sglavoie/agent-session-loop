"""Http application."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from . import (
    http_auth as auth,
)
from . import (
    http_calls as calls,
)
from . import (
    http_constants as constants,
)
from . import (
    http_messages as messages,
)
from . import (
    http_payloads as payloads,
)
from . import (
    http_policy as policy_module,
)
from . import (
    http_routes as routes,
)
from . import (
    http_static as static_module,
)
from .credentials import (
    ROLE_CONNECTOR,
    ROLE_PUBLIC,
)
from .details import DetailError
from .history_primitives import refuse_non_finite
from .operations import OperationError
from .uplink import UplinkRefusal

#: The only spelling of a body length this application reads.
_CONTENT_LENGTH = re.compile(r"[0-9]+", re.ASCII)


class Application:
    """Route, police, authorise, and dispatch. The whole of both servers.

    :meth:`handle` is the only entry point, and it has one refusal exit. A
    transport adapter's job is reduced to turning bytes into a
    :class:`Request` and a :class:`Response` back into bytes -- which is what
    makes ``agent_session_loop.workbench_server`` an adapter rather than a
    second implementation.
    """

    def __init__(
        self,
        provider: calls.Provider,
        authenticator: calls.Authenticator,
        *,
        policy: policy_module.RequestPolicy,
        static: static_module.StaticBundle | None = None,
    ) -> None:
        self.provider = provider
        self.authenticator = authenticator
        self.policy = policy
        self.static = static

    # -- Entry point --------------------------------------------------------

    def handle(self, request: messages.Request) -> messages.Response:
        try:
            return self._handle(request)
        except auth.HttpRefusal as refusal:
            return messages.Response.json(
                refusal.status,
                refusal.to_payload(),
                headers=refusal.headers,
                close=refusal.status in messages._CLOSING,
            )
        except DetailError as error:
            # Refused in `asl_workbench.details`, so the local server and a
            # remote connector refuse the same read the same way.
            return messages.Response.json(
                error.status,
                payloads.error_payload(
                    constants.COMMAND_NAME,
                    reason=error.reason,
                    error=error.message,
                    paused=False,
                    details=dict(error.details),
                ),
            )
        except OperationError as error:
            return messages.Response.json(
                error.status,
                payloads.error_payload(
                    constants.COMMAND_NAME,
                    reason=error.reason,
                    error=error.message,
                    paused=False,
                    details=dict(error.details),
                ),
            )
        except UplinkRefusal as error:
            # Refused in `asl_workbench.uplink`, where the pairing's own rules
            # live. The status travels with the refusal because the connector's
            # retry decision depends on which one it was, and a message it had
            # to parse would not be a decision it could make.
            return messages.Response.json(
                error.status,
                payloads.error_payload(
                    constants.COMMAND_NAME,
                    reason=error.reason,
                    error=error.message,
                    paused=False,
                    details=dict(error.details),
                ),
            )
        except Exception as error:  # noqa: BLE001 - one safety net, by design
            return messages.Response.json(500, self._internal_error(error))

    def _internal_error(self, error: Exception) -> dict[str, Any]:
        """Report an unexpected failure at this mode's declared disclosure level."""
        message = (
            str(error)
            if self.policy.disclose_internal_errors
            else "the request could not be completed"
        )
        return payloads.error_payload(
            constants.COMMAND_NAME,
            reason="internal-error",
            error=message,
            paused=False,
        )

    # -- The pipeline -------------------------------------------------------

    def _handle(self, request: messages.Request) -> messages.Response:
        self._check_host(request)
        if request.method in constants.SAFE_METHODS and self.static is not None:
            if request.path == "/":
                return self.static.index(request)
            if request.path.startswith(static_module.STATIC_URL_PREFIX):
                return self.static.asset(
                    request, request.path[len(static_module.STATIC_URL_PREFIX) :]
                )
        route = self._route(request)
        principal = self.authenticator.authenticate(request, route)
        self._check_role(route, principal)
        self._check_origin(request, route)
        if route.operation == routes.CAPABILITY_OPERATION:
            # Answered here rather than by a provider: a provider that could
            # decline to describe itself would leave a frontend guessing.
            return messages.Response.success(reason="ok", **self.capabilities_payload())
        self._check_available(route)
        body = self._read_body(request, route)
        return self.provider.invoke(
            calls.Call(route=route, request=request, principal=principal, body=body)
        )

    def _check_host(self, request: messages.Request) -> None:
        """Refuse a request addressed to some other name that resolved here.

        Before routing, because the shell and its assets are reads too and a
        rebound page that could load them could read the API next.
        """
        if not self.policy.require_local_host:
            return
        if policy_module.is_local_host(request.host):
            return
        raise auth.HttpRefusal(
            403, "forbidden", "request Host does not name this machine"
        )

    def _route(self, request: messages.Request) -> routes.Route:
        # HEAD is GET without a body (RFC 9110 section 9.3.2), and the server
        # already drops the body, so it is answered by the GET route rather
        # than by a second table entry that could drift from it.
        method = "GET" if request.method == "HEAD" else request.method
        route = routes.ROUTES_BY_KEY.get((method, request.path))
        if route is not None:
            return route
        allowed = routes.methods_for_path(request.path)
        if allowed and self._conceals(request):
            raise auth.HttpRefusal(404, "not-found", f"unknown route {request.path}")
        if allowed:
            raise auth.HttpRefusal(
                405,
                "method-not-allowed",
                f"method {request.method} not supported on {request.path}",
                extra={"allow": list(allowed)},
                headers={"Allow": ", ".join(allowed)},
            )
        raise auth.HttpRefusal(404, "not-found", f"unknown route {request.path}")

    def _conceals(self, request: messages.Request) -> bool:
        """Whether a wrong method on this path must answer as an unknown path.

        Routing runs before authentication, so a 405 and its ``Allow`` header
        would confirm an owner-only path to the very caller :meth:`_check_role`
        answers 404 for. The path is concealed exactly when that caller could
        reach none of its routes. A credential the authenticator refuses is
        not concealed: that caller is told the path exists by the 401 a
        correct method gets, so hiding it here would only add a contradiction.
        """
        if not self.policy.conceal_forbidden:
            return False
        for route in routes.ROUTES:
            if route.path != request.path:
                continue
            try:
                principal = self.authenticator.authenticate(request, route)
            except auth.HttpRefusal:
                return False
            if principal.role != ROLE_PUBLIC or auth.role_satisfies(
                principal.role, route.role
            ):
                return False
        return True

    def _check_role(self, route: routes.Route, principal: auth.Principal) -> None:
        if auth.role_satisfies(principal.role, route.role):
            return
        if self.policy.conceal_forbidden and principal.role == ROLE_PUBLIC:
            # The same answer an unknown path gets. On a reachable surface the
            # existence of an owner-only route is itself worth not confirming
            # -- to a caller who proved nothing. Concealment stops there: a
            # caller who did prove a role and still cannot reach this route has
            # the wrong credential, not a wrong URL, and 404 would send them
            # looking for a typo. That is the split the Companion drew, and the
            # Homepage container's own access check reads it back from inside
            # the container on every verify.
            raise auth.HttpRefusal(404, "not-found", f"unknown route {route.path}")
        raise auth.HttpRefusal(
            403,
            "forbidden",
            f"{route.path} requires the {route.role} role",
            details={"required_role": route.role, "role": principal.role},
        )

    def _check_origin(self, request: messages.Request, route: routes.Route) -> None:
        """Prove a mutation came from this application's own page.

        Three checks, and each covers what the others cannot: ``Sec-Fetch-Site``
        names the browser's own verdict, a header no cross-origin form can set
        rules out the request shapes that carry no site at all, and the Origin
        comparison is what holds when a client sends neither.

        A connector route is exempt, and the exemption is the point rather than
        a hole in it: all three checks ask "did a page on this origin send
        this", and a connector is not a page. It sends no ``Origin`` and no
        ``Sec-Fetch-Site`` because nothing in a browser is involved, and what
        it does hold is an issued bearer credential that no page has -- so the
        cross-site reasoning these checks encode has nothing to decide here,
        and applying them anyway would refuse every upload.
        """
        policy = self.policy
        if not route.mutating or not policy.require_origin:
            return
        if route.role == ROLE_CONNECTOR:
            return
        site = (request.header("sec-fetch-site") or "").strip().lower()
        if site in policy.refused_fetch_sites:
            raise auth.HttpRefusal(403, "forbidden", "cross-site request refused")
        if (
            policy.request_header is not None
            and (request.header(policy.request_header) or "").strip() != "1"
        ):
            raise auth.HttpRefusal(403, "forbidden", "cross-site request refused")
        origin = (request.header("origin") or "").strip()
        if origin not in policy.origins(request.host):
            raise auth.HttpRefusal(403, "forbidden", "cross-site request refused")

    def _check_available(self, route: routes.Route) -> None:
        reason = self.provider.unavailable(route.operation)
        if reason is None:
            return
        raise auth.HttpRefusal(
            503,
            "operation-unavailable",
            reason,
            details={"operation": route.operation, "mode": self.provider.mode},
        )

    # -- Framing and body ---------------------------------------------------

    def _read_body(
        self, request: messages.Request, route: routes.Route
    ) -> Mapping[str, Any]:
        """Read at most the bytes the framing rules already admitted."""
        if not route.mutating:
            return {}
        policy = self.policy
        length = self._body_length(request, route)
        if length == 0:
            # 411 is for a request that declared nothing. An explicit
            # ``Content-Length: 0`` did declare its length -- an empty body --
            # and is refused, if at all, for what that empty body lacks.
            if (
                policy.require_content_length
                and request.header("content-length") is None
            ):
                raise auth.HttpRefusal(
                    411, "length-required", "request body must declare Content-Length"
                )
            return {}
        self._check_content_type(request)
        try:
            raw = request.read(length)
        except TimeoutError as error:
            # The client declared a body and stopped sending it. That is a
            # framing failure of theirs, not a fault of this server's.
            raise auth.HttpRefusal(
                408, "request-timeout", "the request body was not received in time"
            ) from error
        if len(raw) != length:
            # A short body is a truncated one: parsing what arrived could act
            # on a prefix the client never meant as a whole request.
            raise auth.invalid_request(
                f"request body ended after {len(raw)} of {length} declared bytes"
            )
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise auth.invalid_request(
                f"request body must be utf-8: {error}"
            ) from error
        try:
            # ``NaN`` and the infinities are not JSON, and a body carrying one
            # would reach a bound or a store that cannot hold it. Refused here,
            # with the same answer any other malformed body gets.
            # A deeply nested body exhausts the decoder's recursion instead of
            # failing to parse; it is just as malformed and gets the same 400.
            parsed = json.loads(decoded, parse_constant=refuse_non_finite)
        except (ValueError, RecursionError) as error:
            raise auth.invalid_request(
                f"request body must be valid JSON: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise auth.invalid_request("request body must be a JSON object")
        return parsed

    def _body_length(self, request: messages.Request, route: routes.Route) -> int:
        limit = route.max_body_bytes or self.policy.max_body_bytes
        encoding = (request.header("transfer-encoding") or "").strip().lower()
        if encoding:
            raise auth.HttpRefusal(
                411,
                "length-required",
                "request body must declare Content-Length",
                details={"transfer_encoding": encoding},
            )
        raw = request.header("content-length")
        if raw is None:
            return 0
        # ASCII digits and nothing else (RFC 9110 section 8.6). ``int()`` would
        # also take a sign, an underscore and non-ASCII digits, and a framing
        # number two parsers can read differently is one they will.
        declared = raw.strip(" \t")
        if not _CONTENT_LENGTH.fullmatch(declared):
            raise auth.invalid_request(
                "Content-Length must be a non-negative decimal integer",
                content_length=raw[:32],
            )
        length = int(declared)
        if length == 0:
            return 0
        if length > limit:
            raise auth.HttpRefusal(
                413,
                "request-too-large",
                f"Content-Length {length} exceeds {limit}-byte cap",
            )
        return length

    def _check_content_type(self, request: messages.Request) -> None:
        if not self.policy.require_json_content_type:
            return
        declared = (request.header("content-type") or "").split(";")[0].strip().lower()
        if declared != "application/json":
            raise auth.HttpRefusal(
                415,
                "unsupported-media-type",
                "request body must be declared application/json",
            )

    # -- Capabilities -------------------------------------------------------

    def capabilities_payload(self) -> dict[str, Any]:
        """What this mode can carry out, and the reason for anything it cannot.

        Reported in the same words the refusal would use, because a frontend
        that greys out a control owes the operator the explanation they would
        have got by clicking it.
        """
        operations = []
        for operation in routes.OPERATIONS:
            reason = (
                None
                if operation == routes.CAPABILITY_OPERATION
                else self.provider.unavailable(operation)
            )
            operations.append(
                {
                    "operation": operation,
                    "available": reason is None,
                    "reason": reason,
                    "role": routes.OPERATION_ROLES[operation],
                }
            )
        return {
            "mode": self.provider.mode,
            "operations": operations,
            "routes": [
                {
                    "method": route.method,
                    "path": route.path,
                    "operation": route.operation,
                    "role": route.role,
                    "mutating": route.mutating,
                    "summary": route.summary,
                }
                for route in routes.ROUTES
            ],
            "unavailable_operations": [
                entry["operation"] for entry in operations if not entry["available"]
            ],
        }
