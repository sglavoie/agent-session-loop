"""Http auth."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import http_constants as constants
from . import http_messages as messages
from . import http_payloads as payloads
from . import http_routes as routes
from .credentials import (
    IDENTITY_HEADER,
    ROLE_CONNECTOR,
    ROLE_OWNER,
    ROLE_PUBLIC,
    ROLE_SUMMARY,
    CredentialStore,
    parse_authorization,
    resolve_role,
)

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

#: How far each role reaches. Connector is deliberately absent: it is not a
#: weaker owner but a different kind of caller, and ranking it against the
#: others would eventually let an upload credential read a plan.
_ROLE_RANK: Mapping[str, int] = {ROLE_PUBLIC: 0, ROLE_SUMMARY: 1, ROLE_OWNER: 2}


def role_satisfies(presented: str, required: str) -> bool:
    """Whether a caller presenting ``presented`` may ask for ``required``."""
    if required == ROLE_CONNECTOR or presented == ROLE_CONNECTOR:
        # Exact match only, in both directions.
        return presented == required or required == ROLE_PUBLIC
    return _ROLE_RANK.get(presented, -1) >= _ROLE_RANK.get(required, 99)


@dataclass(frozen=True)
class Principal:
    """Who the application decided is asking, and on what proof."""

    role: str
    credential_id: str | None = None
    subject: str | None = None

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    def to_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "credential_id": self.credential_id,
            "subject": self.subject,
        }


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class HttpRefusal(Exception):
    """A request this application will not carry out, and the status that says so.

    Carries the whole answer rather than a code to be interpreted later: a
    refusal assembled in two places is a refusal that can disagree with itself.
    """

    def __init__(
        self,
        status: int,
        reason: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.extra: dict[str, Any] = dict(extra or {})
        # Response headers the status itself calls for: ``Allow`` on a 405 and
        # ``WWW-Authenticate`` on a 401 are part of the refusal, not the body.
        self.headers: dict[str, str] = dict(headers or {})

    def to_payload(self) -> dict[str, Any]:
        return payloads.error_payload(
            constants.COMMAND_NAME,
            reason=self.reason,
            error=self.message,
            paused=False,
            details=self.details,
            **self.extra,
        )


#: The challenge every 401 carries (RFC 9110 section 11.6.1): both
#: boundaries accept a bearer credential in ``Authorization``.
BEARER_CHALLENGE: Mapping[str, str] = {"WWW-Authenticate": "Bearer"}


def unauthorized(message: str) -> HttpRefusal:
    """The refusal a missing or unusable credential gets, challenge included."""
    return HttpRefusal(401, "unauthorized", message, headers=BEARER_CHALLENGE)


def invalid_request(message: str, **details: Any) -> HttpRefusal:
    """The refusal a malformed request gets, spelled the one historic way."""
    return HttpRefusal(400, "invalid-request", message, details=details)


class LocalTokenAuthenticator:
    """The loopback boundary, preserved deliberately rather than by omission.

    Reads are token-free and mutations are not. That asymmetry is old, and it
    is kept on purpose: the server binds loopback, the reads it serves have no
    effects, and the deep link an operator follows out of ``asl workbench`` or
    AgentMux carries the token in the query string for the page to use on the
    writes. Requiring a token on reads would break every one of those links
    and would buy nothing the bind address does not already provide -- so the
    reader of this class should see a decision, not an oversight.

    The token is read through a callable because the server mints it at
    startup and this object is built before the socket is bound.
    """

    def __init__(self, token: Callable[[], str]) -> None:
        self._token = token

    def authenticate(self, request: messages.Request, route: routes.Route) -> Principal:
        if not route.mutating:
            return Principal(role=ROLE_OWNER)
        presented = parse_authorization(request.header("authorization"))
        expected = self._token()
        # Compared as bytes: ``compare_digest`` refuses a ``str`` holding
        # anything but ASCII with a TypeError, and a header is whatever the
        # caller sent, so a non-ASCII token would have been a 500, not a 401.
        if presented and secrets.compare_digest(
            presented.encode("utf-8", "surrogatepass"),
            expected.encode("utf-8", "surrogatepass"),
        ):
            return Principal(role=ROLE_OWNER)
        raise unauthorized("missing or invalid Authorization bearer token")


class OwnerIdentityAuthenticator:
    """The private-network boundary: an identity header, or an issued credential.

    Identity ranks above tokens, exactly as :func:`resolve_role` decides it,
    so an owner who also happens to hold a summary credential is still the
    owner. A caller who proves nothing is public rather than refused; the
    refusal belongs to whatever they then ask for.

    Offering a credential the application does not accept is a different thing
    from offering none, and it answers ``401`` rather than falling through as
    public. This is the boundary the Companion drew, in the same two places and
    for the same reason: a caller holding a broken credential needs to be told
    that it is broken, or they will spend the afternoon reading the route table
    instead of reissuing a token. The distinction is worth one bit of route
    existence -- and only to a caller who already went to the trouble of
    presenting something -- because the alternative is a private service whose
    only answer to an expired token is the answer a typo gets.

    A public route is exempt, again as the Companion had it: it asks for no
    proof, so an unusable proof offered alongside is not what makes the request
    fail. The health route a monitor polls stays the one thing that answers the
    same way to everyone.

    The uplink is exempt the other way. A connector route is exact-match in
    :func:`role_satisfies`, in both directions, so an owner is not a connector
    and no identity header can make one: reading identity first there would let
    the proxy's statement outrank the publisher's own credential and refuse the
    publisher on the only route it has. Serve adds that header to everything the
    tailnet sends, including the connector's own frames, so this is not an edge
    case -- it is every publication. The Companion never met it because it
    dispatched on the route's role rather than on whatever the caller happened
    to bring, and this restores that split at the one route where the two
    orderings disagree.
    """

    def __init__(self, store: CredentialStore, *, owner: str | None) -> None:
        self._store = store
        self._owner = owner

    def authenticate(self, request: messages.Request, route: routes.Route) -> Principal:
        presented = request.header("authorization")
        authorization = resolve_role(
            self._store,
            authorization=presented,
            identity=None
            if route.role == ROLE_CONNECTOR
            else request.header(IDENTITY_HEADER),
            owner=self._owner,
        )
        if (
            route.role != ROLE_PUBLIC
            and authorization.role == ROLE_PUBLIC
            and parse_authorization(presented) is not None
        ):
            raise unauthorized("the credential was not accepted")
        return Principal(
            role=authorization.role,
            credential_id=authorization.credential_id,
            subject=authorization.subject,
        )
