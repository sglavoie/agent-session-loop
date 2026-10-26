"""Http messages."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import http_constants as constants
from . import http_payloads as payloads

# ---------------------------------------------------------------------------
# Requests and responses
# ---------------------------------------------------------------------------


def _no_body(length: int) -> bytes:
    del length
    return b""


@dataclass(frozen=True)
class Request:
    """One inbound request, independent of the transport that carried it.

    ``read`` is supplied by the adapter and is called at most once, and only
    after the framing and size rules have already decided how many bytes are
    allowed to exist. Handing the application a socket instead would make the
    size cap advisory.
    """

    method: str
    path: str
    query: Mapping[str, tuple[str, ...]]
    headers: Mapping[str, str]
    host: str = ""
    read: Callable[[int], bytes] = _no_body

    @classmethod
    def build(
        cls,
        method: str,
        target: str,
        headers: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        *,
        read: Callable[[int], bytes] | None = None,
    ) -> Request:
        """Parse a request target and normalise headers for case-free lookup."""
        parsed = urllib.parse.urlparse(target)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        query = {
            key: tuple(values)
            for key, values in urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True
            ).items()
        }
        lowered = normalize_headers(headers if headers is not None else {})
        return cls(
            method=method.upper(),
            path=path,
            query=query,
            headers=lowered,
            host=lowered.get("host", ""),
            read=read or _no_body,
        )

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True)
class Response:
    """One outbound response, headers included, body already encoded."""

    status: int
    body: bytes = b""
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    close: bool = False

    @classmethod
    def json(
        cls,
        status: int,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        close: bool = False,
    ) -> Response:
        body = json.dumps(dict(payload), sort_keys=True).encode("utf-8")
        merged = {
            **SECURITY_HEADERS,
            "Cache-Control": JSON_CACHE_CONTROL,
            "Content-Security-Policy": API_CONTENT_SECURITY_POLICY,
            **(headers or {}),
        }
        return cls(
            status=status,
            body=body,
            content_type="application/json; charset=utf-8",
            headers=merged,
            close=close,
        )

    @classmethod
    def success(cls, status: int = 200, /, **fields: Any) -> Response:
        """An ``ok`` envelope at ``status``.

        The status is positional-only because ``status`` is also a field name
        this API answers with -- ``/api/health`` reports ``status: ok`` -- and
        a keyword that could mean either would silently become the wrong one.
        """
        return cls.json(
            status, payloads.success_payload(constants.COMMAND_NAME, **fields)
        )

    @property
    def has_body(self) -> bool:
        return self.status != 304 and bool(self.body)

    @property
    def sends_content_length(self) -> bool:
        """304 carries no representation, so it carries no length either."""
        return self.status != 304


#: Sent on every response in both modes. Cache-Control is deliberately absent:
#: it is per-response, because the HTML shell, the asset bundle, and a JSON
#: answer have three different correct values and one shared table would have
#: to be wrong about two of them.
SECURITY_HEADERS: Mapping[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


#: JSON answers are never stored. Every one of them is a statement about a run
#: at an instant, and a cached one is a statement about a different instant.
JSON_CACHE_CONTROL = "no-store"


#: A JSON body loads nothing and is framed by nobody.
API_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"


#: The shell loads its own bundle and nothing else. ``style-src`` admits
#: inline because the frontend sets style attributes directly; narrowing that
#: is the frontend sessions' to do, and doing it here would break the shell
#: without anyone having asked for it.
DOCUMENT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


#: Statuses whose response ends the connection. A refused body was never read,
#: so whatever the client was still sending has nowhere to go but the next
#: request's parser.
_CLOSING: frozenset[int] = frozenset({408, 411, 413, 415})


def unavailable_from(
    supported: Iterable[str], *, reasons: Mapping[str, str], default: str
) -> Callable[[str], str | None]:
    """Build a provider's ``unavailable`` from the set it does support.

    Written once because both providers answer the same question and the wrong
    way to answer it -- listing what is missing -- goes stale the moment a
    route is added. Listing what is supported means a new operation is
    unavailable until someone says otherwise, which is the safe direction.
    """
    available = frozenset(supported)

    def unavailable(operation: str) -> str | None:
        if operation in available:
            return None
        return reasons.get(operation, default)

    return unavailable


def normalize_headers(
    headers: Mapping[str, Any] | Sequence[tuple[str, Any]],
) -> dict[str, str]:
    """Lowercase header names for case-free lookup, whatever shape they arrive in."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {str(name).lower(): str(value) for name, value in items}
