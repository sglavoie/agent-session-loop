"""Http calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import http_auth as auth
from . import http_messages as messages
from . import http_routes as routes

# ---------------------------------------------------------------------------
# The call a provider is handed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One authorised request, parsed the same way for both providers.

    The accessors matter as much as the fields. Every bound, every refusal
    message, and every coercion a route needs lives on this object, so the Mac
    and the Pi cannot disagree about what ``limit=0`` means or which of the
    two refuses an absent ``plan_dir`` more politely.
    """

    route: routes.Route
    request: messages.Request
    principal: auth.Principal
    body: Mapping[str, Any]

    @property
    def operation(self) -> str:
        return self.route.operation

    @property
    def query(self) -> Mapping[str, tuple[str, ...]]:
        return self.request.query

    # -- Query parameters ---------------------------------------------------

    def require_query(self, key: str) -> str:
        values = self.request.query.get(key)
        if not values:
            raise auth.invalid_request(f"missing required query parameter {key!r}")
        value = values[0].strip()
        if not value:
            raise auth.invalid_request(f"query parameter {key!r} must not be empty")
        return value

    def optional_query(self, key: str) -> str | None:
        values = self.request.query.get(key)
        if not values:
            return None
        text = values[0].strip()
        return text or None

    def query_values(self, key: str) -> tuple[str, ...]:
        return tuple(self.request.query.get(key, ()))

    def optional_int_query(self, key: str) -> int | None:
        raw = self.optional_query(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError as error:
            raise auth.invalid_request(
                f"query parameter {key!r} must be an integer: {error}", **{key: raw}
            ) from error

    def non_negative_int(self, key: str, *, default: int) -> int:
        raw = self.optional_int_query(key)
        value = default if raw is None else raw
        if value < 0:
            raise auth.invalid_request(f"query parameter {key!r} must be non-negative")
        return value

    def bounded_int(self, key: str, *, default: int, maximum: int) -> int:
        value = self.non_negative_int(key, default=default)
        if value > maximum:
            raise auth.invalid_request(
                f"query parameter {key!r} exceeds maximum {maximum}"
            )
        return value

    # -- Body fields --------------------------------------------------------

    def require_string(self, key: str) -> str:
        value = self.body.get(key)
        if not isinstance(value, str):
            raise auth.invalid_request(f"missing required string field {key!r}")
        if not value:
            raise auth.invalid_request(f"field {key!r} must not be empty")
        return value

    def optional_string(self, key: str) -> str | None:
        value = self.body.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise auth.invalid_request(f"field {key!r} must be a string when provided")
        return value or None

    def bounded_id(self, key: str, *, maximum: int) -> str | None:
        """Narrow a client-chosen idempotency key, or refuse one too big to hold.

        Stripped as well as bounded: a key differing from another only by
        surrounding whitespace would read as a second submission here and as
        the same one to the operator who typed it.
        """
        value = self.optional_string(key)
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise auth.invalid_request(f"field {key!r} must not be blank when provided")
        if len(trimmed) > maximum:
            raise auth.invalid_request(f"field {key!r} exceeds {maximum} characters")
        return trimmed


# ---------------------------------------------------------------------------
# Providers and authenticators
# ---------------------------------------------------------------------------


class Provider(Protocol):
    """What a mode contributes: execution, and an honest list of what it cannot do."""

    @property
    def mode(self) -> str:
        """:data:`MODE_LOCAL` or :data:`MODE_REMOTE`."""

    def unavailable(self, operation: str) -> str | None:
        """Why this provider cannot carry out ``operation``, or ``None``.

        The reason is operator-facing prose, reported both in advance through
        ``/api/capabilities`` and at the moment of refusal, so a frontend can
        grey a control out with the same sentence the failure would have
        given.
        """

    def invoke(self, call: Call) -> messages.Response:
        """Carry out one authorised, parsed call."""


class Authenticator(Protocol):
    """What proof a mode accepts, and what role that proof establishes."""

    def authenticate(
        self, request: messages.Request, route: routes.Route
    ) -> auth.Principal:
        """Decide the caller's role, or raise :class:`HttpRefusal`."""
