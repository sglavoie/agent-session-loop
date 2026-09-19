"""The one HTTP application both Workbench modes serve.

Until this module existed there were two route tables: the local server's, a
dict built inside ``WorkbenchRequestHandler._dispatch``, and the Companion's,
a tuple of role-declared ``Route`` records. They answered different questions
about the same plans, refused differently, and could only be compared by
reading both. This module is the single one, and the two modes differ in
exactly three declared ways:

* **Execution.** A :class:`Provider` carries out one operation. The Mac's
  provider calls ASL's services directly; the Pi's serves retained
  observations and forwards bounded requests through the connector. Neither
  owns a route, a status code, or a refusal vocabulary.
* **Authentication.** An :class:`Authenticator` decides which
  :class:`Principal` a request presents. The local server proves the operator
  with the startup bearer token; the Pi proves them with the identity header
  its private network supplies. The *role each route requires* is declared
  once, here, and neither authenticator gets to lower it.
* **Request policy.** A :class:`RequestPolicy` names the size, framing,
  content-type, origin, and diagnostic rules a mode enforces. Both modes run
  the same enforcement code; what differs is the configuration, and every
  divergence is a field with a reason attached rather than a branch somewhere
  in a handler.

Everything else -- which routes exist, which role each needs, how a query
parameter is parsed, what an envelope looks like, which headers go out, how a
static asset is validated and cached -- is stated once and shared. A route
whose operation a provider cannot carry out is not missing: it answers
``operation-unavailable`` with the provider's own reason, and
``GET /api/capabilities`` reports the same set in advance, so a frontend can
explain what it cannot offer instead of discovering it by failing.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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
from .details import DetailError
from .operations import OperationError
from .transport import MAX_FRAME_BYTES
from .uplink import UplinkRefusal

__all__ = [
    "API_CONTENT_SECURITY_POLICY",
    "CAPABILITY_OPERATION",
    "COMMAND_NAME",
    "DOCUMENT_CONTENT_SECURITY_POLICY",
    "JSON_CACHE_CONTROL",
    "LOCAL_POLICY",
    "MAX_REQUEST_BODY",
    "MODES",
    "MODE_LOCAL",
    "MODE_REMOTE",
    "OPERATIONS",
    "OPERATION_ROLES",
    "REMOTE_POLICY",
    "ROUTES",
    "ROUTES_BY_KEY",
    "SAFE_METHODS",
    "SECURITY_HEADERS",
    "STATIC_ASSET_CACHE_CONTROL",
    "STATIC_DEFAULT_MIME",
    "STATIC_INDEX_CACHE_CONTROL",
    "STATIC_INDEX_FILE",
    "STATIC_MIME_TYPES",
    "STATIC_ROOT",
    "STATIC_URL_PREFIX",
    "UPLINK_PATH",
    "Application",
    "Authenticator",
    "Call",
    "HttpRefusal",
    "LocalTokenAuthenticator",
    "OwnerIdentityAuthenticator",
    "Principal",
    "Provider",
    "Request",
    "RequestPolicy",
    "Response",
    "Route",
    "StaticBundle",
    "compute_etag",
    "error_payload",
    "methods_for_path",
    "operation_role",
    "parse_etag_list",
    "role_satisfies",
    "shell_bundle",
    "success_payload",
]

#: The ``command`` field every Workbench envelope carries, in both modes and
#: on the CLI. Kept here because the envelope is now built here.
COMMAND_NAME = "workbench"

#: Which provider is carrying out the work. Named rather than inferred: a
#: capability report that could not say which mode produced it would be a
#: report a frontend could not act on.
MODE_LOCAL = "local"
MODE_REMOTE = "remote"
MODES: tuple[str, ...] = (MODE_LOCAL, MODE_REMOTE)

#: Methods that may not change anything. Everything else is a mutation and is
#: authorised as one.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

#: 1 MiB, the historic local cap. The largest legitimate body is a redirect
#: correction or a reply, both of which are prose.
MAX_REQUEST_BODY = 1024 * 1024

#: The one path a connector posts to, and the only route whose body is a
#: transport frame rather than a form. Named here because the route table is
#: what a deployment reads it from; the Mac connector holds its own copy of the
#: string, because this package is not importable from there.
UPLINK_PATH = "/v1/ingest"


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def success_payload(command: str, **fields: Any) -> dict[str, Any]:
    """Build a successful command response, preserving command-specific keys.

    Identical to ``agent_session_loop.responses.success_payload`` by contract
    rather than by coincidence: ``tests/test_workbench_http.py`` pins the two
    against each other, so the portable half can answer a CLI-shaped envelope
    without importing the CLI.
    """
    payload: dict[str, Any] = {"ok": True, "command": command}
    payload.update(fields)
    return payload


def error_payload(
    command: str,
    *,
    reason: str,
    error: str,
    paused: bool = False,
    details: Mapping[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a failed command response with the common scriptable contract."""
    payload: dict[str, Any] = {
        "ok": False,
        "command": command,
        "reason": reason,
        "error": error,
        "paused": paused,
        "details": {} if details is None else dict(details),
    }
    payload.update(fields)
    return payload


# ---------------------------------------------------------------------------
# The operation vocabulary
# ---------------------------------------------------------------------------

OP_HEALTH = "health"
OP_CAPABILITIES = "capabilities"
OP_SUMMARY = "summary"
OP_QUEUE = "queue"
OP_HISTORY = "history"
OP_PLAN = "plan"
OP_RECOVERY = "recovery"
OP_REGISTRY = "registry"
OP_ARTIFACTS = "artifacts"
OP_STATE = "state"
OP_EVENTS = "events"
OP_LOGS = "logs"
OP_LAST_MESSAGE = "last-message"
OP_RESULT = "result"
OP_VERIFICATION = "verification"
OP_COMMITS = "commits"
OP_PROGRESS = "progress"
OP_FINAL_REVIEW = "final-review"
OP_QUESTIONS = "questions"
OP_REDIRECT_STATUS = "redirect-status"
OP_CONVERSATION_RECOVERY = "conversation-recovery"
OP_ARCHIVE_LIST = "archive-list"
OP_REGISTRY_PIN = "registry-pin"
OP_REGISTRY_UNPIN = "registry-unpin"
OP_PLAN_ARCHIVE = "plan-archive"
OP_PLANS_ARCHIVE = "plans-archive"
OP_ARCHIVE_RESTORE = "archive-restore"
OP_ARCHIVE_DELETE = "archive-delete"
OP_ARCHIVE_SETTINGS = "archive-settings"
OP_LAUNCH = "launch"
OP_REPLY = "reply"
OP_CONTROL_STOP_AFTER_CURRENT = "control-stop-after-current"
OP_CONTROL_ABORT = "control-abort"
OP_CONTROL_SELECT_HARNESS = "control-select-harness"
OP_CONTROL_REDIRECT = "control-redirect"
OP_UNLOCK = "unlock"
OP_INGEST = "ingest"

#: The one operation the application answers itself. A provider that could
#: decline to describe its own capabilities would leave a frontend with no way
#: to find out what it may offer.
CAPABILITY_OPERATION = OP_CAPABILITIES


@dataclass(frozen=True)
class Route:
    """One method/path pair, the operation behind it, and who may ask for it.

    ``role`` is the declaration that used to be an ``if`` in two servers. A
    route with no role is not a route: every entry names one, and the
    application refuses anything a principal's role does not reach before a
    provider is asked to do any work.
    """

    method: str
    path: str
    operation: str
    role: str
    summary: str
    #: The largest body this route will read, when a mode's own cap is the
    #: wrong bound for it. Only the connector uplink declares one: everything
    #: else is a small JSON object, and a ceiling raised for all of them to
    #: accommodate one is a ceiling for none of them.
    max_body_bytes: int | None = None

    @property
    def mutating(self) -> bool:
        return self.method not in SAFE_METHODS

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


#: Every route the unified application serves, in both modes.
#:
#: The list is the local server's historic 34 plus two the consolidation adds:
#: ``/api/capabilities``, which reports what this mode can carry out, and
#: ``/api/summary``, the counts-and-freshness view a caller holding only a
#: summary credential may read. ``POST /api/plan/delete`` remains an alias of
#: the archive operation rather than a second implementation of it, which is
#: what it has been since archiving replaced deletion.
#:
#: Then the four the deployment needs: :data:`UPLINK_PATH`, which is the only
#: path a connector ever posts to, and the Companion's three published read
#: paths as aliases of the ``/api`` routes that answer them.
ROUTES: tuple[Route, ...] = (
    Route("GET", "/api/health", OP_HEALTH, ROLE_PUBLIC, "liveness probe"),
    Route(
        "GET",
        "/api/capabilities",
        OP_CAPABILITIES,
        ROLE_SUMMARY,
        "which operations this mode can carry out, and why not",
    ),
    Route(
        "GET",
        "/api/summary",
        OP_SUMMARY,
        ROLE_SUMMARY,
        "plan counts and observation freshness, carrying no prose",
    ),
    Route("GET", "/api/queue", OP_QUEUE, ROLE_OWNER, "Workbench queue summaries"),
    Route(
        "GET",
        "/api/history",
        OP_HISTORY,
        ROLE_OWNER,
        "runs that ended inside the retention window, newest first",
    ),
    Route("GET", "/api/plan", OP_PLAN, ROLE_OWNER, "one plan detail summary"),
    Route(
        "GET", "/api/recovery", OP_RECOVERY, ROLE_OWNER, "recovery diagnosis for a plan"
    ),
    Route("GET", "/api/registry", OP_REGISTRY, ROLE_OWNER, "registry payload"),
    Route(
        "GET", "/api/artifacts", OP_ARTIFACTS, ROLE_OWNER, "artifact paths and metadata"
    ),
    Route("GET", "/api/state", OP_STATE, ROLE_OWNER, "latest runner state payload"),
    Route("GET", "/api/events", OP_EVENTS, ROLE_OWNER, "paginated run events"),
    Route("GET", "/api/logs", OP_LOGS, ROLE_OWNER, "bounded stdout/stderr window"),
    Route(
        "GET",
        "/api/last-message",
        OP_LAST_MESSAGE,
        ROLE_OWNER,
        "bounded last-message text",
    ),
    Route("GET", "/api/result", OP_RESULT, ROLE_OWNER, "active turn result payload"),
    Route(
        "GET",
        "/api/verification",
        OP_VERIFICATION,
        ROLE_OWNER,
        "active verification details",
    ),
    Route("GET", "/api/commits", OP_COMMITS, ROLE_OWNER, "commit artifact metadata"),
    Route("GET", "/api/progress", OP_PROGRESS, ROLE_OWNER, "parsed progress journal"),
    Route(
        "GET",
        "/api/final-review",
        OP_FINAL_REVIEW,
        ROLE_OWNER,
        "bounded final-review content",
    ),
    Route(
        "GET", "/api/questions", OP_QUESTIONS, ROLE_OWNER, "paused-run prompt and Q&A"
    ),
    Route(
        "GET",
        "/api/redirect-status",
        OP_REDIRECT_STATUS,
        ROLE_OWNER,
        "durable redirect operations and stages",
    ),
    Route(
        "GET",
        "/api/conversation-recovery",
        OP_CONVERSATION_RECOVERY,
        ROLE_OWNER,
        "recovery decision and its commands",
    ),
    Route(
        "GET",
        "/api/archive",
        OP_ARCHIVE_LIST,
        ROLE_OWNER,
        "archived entries and cleanup candidates",
    ),
    Route(
        "POST",
        "/api/registry/pin",
        OP_REGISTRY_PIN,
        ROLE_OWNER,
        "upsert a registry pin",
    ),
    Route(
        "POST",
        "/api/registry/unpin",
        OP_REGISTRY_UNPIN,
        ROLE_OWNER,
        "remove a registry pin",
    ),
    Route(
        "POST",
        "/api/plan/archive",
        OP_PLAN_ARCHIVE,
        ROLE_OWNER,
        "archive one plan and its ASL state",
    ),
    Route(
        "POST",
        "/api/plans/archive",
        OP_PLANS_ARCHIVE,
        ROLE_OWNER,
        "bulk-archive many plans",
    ),
    Route(
        "POST",
        "/api/plan/delete",
        OP_PLAN_ARCHIVE,
        ROLE_OWNER,
        "backward-compatible archive alias",
    ),
    Route(
        "POST",
        "/api/archive/restore",
        OP_ARCHIVE_RESTORE,
        ROLE_OWNER,
        "restore one archived entry",
    ),
    Route(
        "POST",
        "/api/archive/delete",
        OP_ARCHIVE_DELETE,
        ROLE_OWNER,
        "permanently purge archive entries",
    ),
    Route(
        "POST",
        "/api/archive/settings",
        OP_ARCHIVE_SETTINGS,
        ROLE_OWNER,
        "update archive retention days",
    ),
    Route("POST", "/api/launch", OP_LAUNCH, ROLE_OWNER, "spawn a run for a plan"),
    Route(
        "POST",
        "/api/reply",
        OP_REPLY,
        ROLE_OWNER,
        "resume a paused run with a reply",
    ),
    Route(
        "POST",
        "/api/control/stop-after-current",
        OP_CONTROL_STOP_AFTER_CURRENT,
        ROLE_OWNER,
        "enqueue stop-after-current",
    ),
    Route(
        "POST",
        "/api/control/abort",
        OP_CONTROL_ABORT,
        ROLE_OWNER,
        "enqueue abort of the in-flight turn",
    ),
    Route(
        "POST",
        "/api/control/select-harness",
        OP_CONTROL_SELECT_HARNESS,
        ROLE_OWNER,
        "enqueue harness selection",
    ),
    Route(
        "POST",
        "/api/control/redirect",
        OP_CONTROL_REDIRECT,
        ROLE_OWNER,
        "submit an urgent correction to the live turn",
    ),
    Route(
        "POST", "/api/unlock", OP_UNLOCK, ROLE_OWNER, "remove a stale operational lock"
    ),
    # -- The connector's own path -------------------------------------------
    #
    # One route, both directions: the frame goes up in the request and
    # whatever the service is holding comes back down in the acknowledgement.
    # The Companion's `POST /v1/commands/poll` is deliberately not migrated --
    # it was a second round trip for something the first one can carry.
    Route(
        "POST",
        UPLINK_PATH,
        OP_INGEST,
        ROLE_CONNECTOR,
        "a paired connector's upload, answered with whatever rides back down",
        max_body_bytes=MAX_FRAME_BYTES,
    ),
    # -- The Companion's published read paths, as aliases -------------------
    #
    # Homepage's widget, the Uptime Kuma monitor and `IntegrationSite` all
    # name these three. Serving them beside the `/api` paths is what makes a
    # cutover one change rather than four that have to land together, and an
    # alias is not a second implementation: each one names the operation the
    # `/api` route already names, which `_operation_roles` then re-checks so
    # an alias cannot quietly ask for a weaker role than what it aliases.
    Route("GET", "/v1/health", OP_HEALTH, ROLE_PUBLIC, "liveness probe (alias)"),
    Route(
        "GET",
        "/v1/summary",
        OP_SUMMARY,
        ROLE_SUMMARY,
        "plan counts and observation freshness (alias)",
    ),
    Route("GET", "/v1/runs", OP_QUEUE, ROLE_OWNER, "Workbench queue summaries (alias)"),
)

ROUTES_BY_KEY: Mapping[tuple[str, str], Route] = {route.key: route for route in ROUTES}

#: Every distinct operation, sorted. ``/api/plan/delete`` shares
#: ``plan-archive`` with ``/api/plan/archive``, so this is shorter than
#: :data:`ROUTES`.
OPERATIONS: tuple[str, ...] = tuple(sorted({route.operation for route in ROUTES}))


def _operation_roles() -> Mapping[str, str]:
    roles: dict[str, str] = {}
    for route in ROUTES:
        existing = roles.get(route.operation)
        if existing is not None and existing != route.role:
            raise ValueError(
                f"operation {route.operation!r} is declared with two roles: "
                f"{existing!r} and {route.role!r}"
            )
        roles[route.operation] = route.role
    return roles


#: The role each operation requires. Derived rather than restated, and derived
#: strictly: an alias that asked for a weaker role than the route it aliases
#: would be a way in, so building this table refuses that outright.
OPERATION_ROLES: Mapping[str, str] = _operation_roles()


def operation_role(operation: str) -> str:
    """The role an operation requires, by name."""
    try:
        return OPERATION_ROLES[operation]
    except KeyError:
        raise KeyError(f"unknown Workbench operation {operation!r}") from None


def methods_for_path(path: str) -> tuple[str, ...]:
    """Every method declared on one path, sorted. Empty when the path is not one."""
    return tuple(sorted({route.method for route in ROUTES if route.path == path}))


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
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.extra: dict[str, Any] = dict(extra or {})

    def to_payload(self) -> dict[str, Any]:
        return error_payload(
            COMMAND_NAME,
            reason=self.reason,
            error=self.message,
            paused=False,
            details=self.details,
            **self.extra,
        )


def invalid_request(message: str, **details: Any) -> HttpRefusal:
    """The refusal a malformed request gets, spelled the one historic way."""
    return HttpRefusal(400, "invalid-request", message, details=details)


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
        return cls.json(status, success_payload(COMMAND_NAME, **fields))

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


# ---------------------------------------------------------------------------
# Request policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestPolicy:
    """The size, framing, content-type, origin, and diagnostic rules of a mode.

    One enforcement implementation, two configurations. Each field that
    differs between the modes says why in its default below rather than in a
    branch: the point of naming them is that a reader can see the whole
    difference between the loopback server and the public one at once.
    """

    #: The largest body this mode reads, before reading any of it.
    max_body_bytes: int = MAX_REQUEST_BODY
    #: Whether a mutation must declare its length up front. A chunked body has
    #: no length to bound, so refusing it is how the cap above stays a cap.
    require_content_length: bool = True
    #: Whether a request that carries a body must declare it as JSON. Checked
    #: only when a body is present, so a POST with nothing in it is not refused
    #: for failing to describe the nothing.
    require_json_content_type: bool = True
    #: Whether a mutation must prove it came from this application's own page.
    require_origin: bool = False
    #: Origins accepted when :attr:`require_origin` holds, in addition to the
    #: loopback form built from the request's own ``Host``.
    allowed_origins: tuple[str, ...] = ()
    #: A header no cross-origin form can set, required on mutations when
    #: :attr:`require_origin` holds.
    request_header: str | None = None
    #: ``Sec-Fetch-Site`` values refused outright on a mutation.
    refused_fetch_sites: frozenset[str] = frozenset({"cross-site", "same-site"})
    #: Whether an unexpected exception's text may reach the client. True only
    #: on loopback, where the client is the operator who owns the process and
    #: the text is the fastest thing they have; false anywhere reachable, where
    #: it is an oracle.
    disclose_internal_errors: bool = False
    #: Whether a route the caller's role cannot reach answers ``not-found``
    #: instead of ``forbidden``. On a public surface the existence of an
    #: owner-only route is itself worth not confirming.
    conceal_forbidden: bool = False

    def origins(self, host: str) -> frozenset[str]:
        """Every Origin this mode accepts for a mutation, given the request Host."""
        accepted = set(self.allowed_origins)
        if host:
            accepted.add(f"http://{host}")
        return frozenset(accepted)


#: Loopback. The bearer token minted at startup is the proof, so there is no
#: origin to check against and no identity header to trust; and the client is
#: the operator, so an unexpected failure says what it was.
LOCAL_POLICY = RequestPolicy(
    require_content_length=False,
    require_json_content_type=True,
    require_origin=False,
    disclose_internal_errors=True,
    conceal_forbidden=False,
)

#: Reachable over the private network. Everything the Companion enforced, for
#: the reasons it enforced them: a length before a read, a declared JSON body,
#: an origin and a header no cross-site form can forge, and a refusal that
#: says nothing a prober could use.
REMOTE_POLICY = RequestPolicy(
    require_content_length=True,
    require_json_content_type=True,
    require_origin=True,
    request_header="X-Workbench-Request",
    disclose_internal_errors=False,
    conceal_forbidden=True,
)


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

    route: Route
    request: Request
    principal: Principal
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
            raise invalid_request(f"missing required query parameter {key!r}")
        value = values[0].strip()
        if not value:
            raise invalid_request(f"query parameter {key!r} must not be empty")
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
            raise invalid_request(
                f"query parameter {key!r} must be an integer: {error}", **{key: raw}
            ) from error

    def non_negative_int(self, key: str, *, default: int) -> int:
        raw = self.optional_int_query(key)
        value = default if raw is None else raw
        if value < 0:
            raise invalid_request(f"query parameter {key!r} must be non-negative")
        return value

    def bounded_int(self, key: str, *, default: int, maximum: int) -> int:
        value = self.non_negative_int(key, default=default)
        if value > maximum:
            raise invalid_request(f"query parameter {key!r} exceeds maximum {maximum}")
        return value

    # -- Body fields --------------------------------------------------------

    def require_string(self, key: str) -> str:
        value = self.body.get(key)
        if not isinstance(value, str):
            raise invalid_request(f"missing required string field {key!r}")
        if not value:
            raise invalid_request(f"field {key!r} must not be empty")
        return value

    def optional_string(self, key: str) -> str | None:
        value = self.body.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise invalid_request(f"field {key!r} must be a string when provided")
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
            raise invalid_request(f"field {key!r} must not be blank when provided")
        if len(trimmed) > maximum:
            raise invalid_request(f"field {key!r} exceeds {maximum} characters")
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

    def invoke(self, call: Call) -> Response:
        """Carry out one authorised, parsed call."""


class Authenticator(Protocol):
    """What proof a mode accepts, and what role that proof establishes."""

    def authenticate(self, request: Request, route: Route) -> Principal:
        """Decide the caller's role, or raise :class:`HttpRefusal`."""


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

    def authenticate(self, request: Request, route: Route) -> Principal:
        if not route.mutating:
            return Principal(role=ROLE_OWNER)
        presented = parse_authorization(request.header("authorization"))
        expected = self._token()
        if presented and secrets.compare_digest(presented, expected):
            return Principal(role=ROLE_OWNER)
        raise HttpRefusal(
            401, "unauthorized", "missing or invalid Authorization bearer token"
        )


class OwnerIdentityAuthenticator:
    """The private-network boundary: an identity header, or an issued credential.

    Identity ranks above tokens, exactly as :func:`resolve_role` decides it,
    so an owner who also happens to hold a summary credential is still the
    owner. A caller who proves nothing is public rather than refused; the
    refusal belongs to whatever they then ask for.
    """

    def __init__(self, store: CredentialStore, *, owner: str | None) -> None:
        self._store = store
        self._owner = owner

    def authenticate(self, request: Request, route: Route) -> Principal:
        del route
        authorization = resolve_role(
            self._store,
            authorization=request.header("authorization"),
            identity=request.header(IDENTITY_HEADER),
            owner=self._owner,
        )
        return Principal(
            role=authorization.role,
            credential_id=authorization.credential_id,
            subject=authorization.subject,
        )


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

STATIC_INDEX_FILE = "index.html"
STATIC_URL_PREFIX = "/static/"
#: The one copy of the front end. Both deployments serve *this* directory --
#: the loopback server on a laptop and the Pi over Tailscale -- because a
#: second copy is a second thing to keep in step, and the only way to be sure
#: the two modes run the same shell is for there to be nothing to keep in
#: step. The entry is a native ES module; `modules/` holds the rest.
STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_MIME_TYPES: Mapping[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}
STATIC_DEFAULT_MIME = "application/octet-stream"
#: The shell stays unstored so a server upgrade is never masked by a cached
#: index; the bundle revalidates so an unchanged asset costs one 304 and a
#: changed one arrives immediately after a restart.
STATIC_INDEX_CACHE_CONTROL = "no-store"
STATIC_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"


def compute_etag(stat_result: os.stat_result) -> str:
    """Build a strong validator from inode-style fields.

    Mixes mtime_ns and size so any in-place edit invalidates the cache. The
    token is quoted per RFC 7232 so clients echo it verbatim in
    ``If-None-Match``.
    """
    return f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'


def parse_etag_list(header_value: str) -> tuple[str, ...]:
    """Parse a comma-separated ``If-None-Match`` header into ETag tokens."""
    if not header_value:
        return ()
    tokens: list[str] = []
    for chunk in header_value.split(","):
        token = chunk.strip()
        if not token:
            continue
        if token.startswith("W/"):
            token = token[2:].strip()
        tokens.append(token)
    return tuple(tokens)


class StaticBundle:
    """The browser assets, served identically wherever they are served from.

    Both modes hand out the same shell and the same bundle, so the traversal
    refusal, the mime table, the cache policy, and the validator are decided
    once here rather than twice in two servers that would drift.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def index(self, request: Request) -> Response:
        return self._serve(
            request,
            self.root / STATIC_INDEX_FILE,
            STATIC_MIME_TYPES[".html"],
            cache_control=STATIC_INDEX_CACHE_CONTROL,
            use_etag=False,
            csp=DOCUMENT_CONTENT_SECURITY_POLICY,
        )

    def asset(self, request: Request, relative: str) -> Response:
        if not relative or relative.startswith("/"):
            raise HttpRefusal(404, "not-found", "missing static asset path")
        root = self.root.resolve()
        try:
            candidate = (root / relative).resolve()
        except OSError as error:
            raise invalid_request(
                f"invalid static path: {error}", path=relative
            ) from error
        try:
            candidate.relative_to(root)
        except ValueError:
            raise HttpRefusal(
                400,
                "invalid-request",
                "static path escapes the bundle",
                details={"path": relative},
            ) from None
        if not candidate.is_file():
            raise HttpRefusal(
                404,
                "not-found",
                f"static asset not found: {relative}",
                details={"path": relative},
            )
        content_type = STATIC_MIME_TYPES.get(
            candidate.suffix.lower(), STATIC_DEFAULT_MIME
        )
        return self._serve(
            request,
            candidate,
            content_type,
            cache_control=STATIC_ASSET_CACHE_CONTROL,
            use_etag=True,
            csp=API_CONTENT_SECURITY_POLICY,
        )

    def _serve(
        self,
        request: Request,
        path: Path,
        content_type: str,
        *,
        cache_control: str,
        use_etag: bool,
        csp: str,
    ) -> Response:
        headers = {
            **SECURITY_HEADERS,
            "Cache-Control": cache_control,
            "Content-Security-Policy": csp,
        }
        etag: str | None = None
        if use_etag:
            stat_result = self._stat(path)
            etag = compute_etag(stat_result)
            presented = parse_etag_list(request.header("if-none-match") or "")
            if etag and etag in presented:
                return Response(
                    status=304, headers={**headers, "ETag": etag}, content_type=None
                )
        body = self._read(path)
        if etag is not None:
            headers["ETag"] = etag
        return Response(
            status=200, body=body, content_type=content_type, headers=headers
        )

    @staticmethod
    def _stat(path: Path) -> os.stat_result:
        try:
            return path.stat()
        except FileNotFoundError:
            raise HttpRefusal(
                404,
                "not-found",
                f"static asset not found: {path.name}",
                details={"path": str(path)},
            ) from None
        except OSError as error:
            raise HttpRefusal(
                500, "os-error", str(error), details={"path": str(path)}
            ) from error

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise HttpRefusal(
                404,
                "not-found",
                f"static asset not found: {path.name}",
                details={"path": str(path)},
            ) from None
        except OSError as error:
            raise HttpRefusal(
                500, "os-error", str(error), details={"path": str(path)}
            ) from error


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


def shell_bundle() -> StaticBundle:
    """The front end, for whichever deployment is asking.

    There is one right answer and it is the same one in both modes, so it is
    given here instead of at each construction site. An application built
    without it serves no assets at all, which is the correct behavior for a
    caller that only wants the API.
    """
    return StaticBundle(STATIC_ROOT)


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
        provider: Provider,
        authenticator: Authenticator,
        *,
        policy: RequestPolicy,
        static: StaticBundle | None = None,
    ) -> None:
        self.provider = provider
        self.authenticator = authenticator
        self.policy = policy
        self.static = static

    # -- Entry point --------------------------------------------------------

    def handle(self, request: Request) -> Response:
        try:
            return self._handle(request)
        except HttpRefusal as refusal:
            return Response.json(
                refusal.status, refusal.to_payload(), close=refusal.status in _CLOSING
            )
        except DetailError as error:
            # Refused in `asl_workbench.details`, so the local server and a
            # remote connector refuse the same read the same way.
            return Response.json(
                error.status,
                error_payload(
                    COMMAND_NAME,
                    reason=error.reason,
                    error=error.message,
                    paused=False,
                    details=dict(error.details),
                ),
            )
        except OperationError as error:
            return Response.json(
                error.status,
                error_payload(
                    COMMAND_NAME,
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
            return Response.json(
                error.status,
                error_payload(
                    COMMAND_NAME,
                    reason=error.reason,
                    error=error.message,
                    paused=False,
                    details=dict(error.details),
                ),
            )
        except Exception as error:  # noqa: BLE001 - one safety net, by design
            return Response.json(500, self._internal_error(error))

    def _internal_error(self, error: Exception) -> dict[str, Any]:
        """Report an unexpected failure at this mode's declared disclosure level."""
        message = (
            str(error)
            if self.policy.disclose_internal_errors
            else "the request could not be completed"
        )
        return error_payload(
            COMMAND_NAME, reason="internal-error", error=message, paused=False
        )

    # -- The pipeline -------------------------------------------------------

    def _handle(self, request: Request) -> Response:
        if request.method in SAFE_METHODS and self.static is not None:
            if request.path == "/":
                return self.static.index(request)
            if request.path.startswith(STATIC_URL_PREFIX):
                return self.static.asset(
                    request, request.path[len(STATIC_URL_PREFIX) :]
                )
        route = self._route(request)
        principal = self.authenticator.authenticate(request, route)
        self._check_role(route, principal)
        self._check_origin(request, route)
        if route.operation == CAPABILITY_OPERATION:
            # Answered here rather than by a provider: a provider that could
            # decline to describe itself would leave a frontend guessing.
            return Response.success(reason="ok", **self.capabilities_payload())
        self._check_available(route)
        body = self._read_body(request, route)
        return self.provider.invoke(
            Call(route=route, request=request, principal=principal, body=body)
        )

    def _route(self, request: Request) -> Route:
        route = ROUTES_BY_KEY.get((request.method, request.path))
        if route is not None:
            return route
        allowed = methods_for_path(request.path)
        if allowed:
            raise HttpRefusal(
                405,
                "method-not-allowed",
                f"method {request.method} not supported on {request.path}",
                extra={"allow": list(allowed)},
            )
        raise HttpRefusal(404, "not-found", f"unknown route {request.path}")

    def _check_role(self, route: Route, principal: Principal) -> None:
        if role_satisfies(principal.role, route.role):
            return
        if self.policy.conceal_forbidden:
            # The same answer an unknown path gets. On a reachable surface the
            # existence of an owner-only route is itself worth not confirming.
            raise HttpRefusal(404, "not-found", f"unknown route {route.path}")
        raise HttpRefusal(
            403,
            "forbidden",
            f"{route.path} requires the {route.role} role",
            details={"required_role": route.role, "role": principal.role},
        )

    def _check_origin(self, request: Request, route: Route) -> None:
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
            raise HttpRefusal(403, "forbidden", "cross-site request refused")
        if (
            policy.request_header is not None
            and (request.header(policy.request_header) or "").strip() != "1"
        ):
            raise HttpRefusal(403, "forbidden", "cross-site request refused")
        origin = (request.header("origin") or "").strip()
        if origin not in policy.origins(request.host):
            raise HttpRefusal(403, "forbidden", "cross-site request refused")

    def _check_available(self, route: Route) -> None:
        reason = self.provider.unavailable(route.operation)
        if reason is None:
            return
        raise HttpRefusal(
            503,
            "operation-unavailable",
            reason,
            details={"operation": route.operation, "mode": self.provider.mode},
        )

    # -- Framing and body ---------------------------------------------------

    def _read_body(self, request: Request, route: Route) -> Mapping[str, Any]:
        """Read at most the bytes the framing rules already admitted."""
        if not route.mutating:
            return {}
        policy = self.policy
        length = self._body_length(request, route)
        if length == 0:
            if policy.require_content_length:
                raise HttpRefusal(
                    411, "length-required", "request body must declare Content-Length"
                )
            return {}
        self._check_content_type(request)
        raw = request.read(length)
        if not raw:
            return {}
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise invalid_request(f"request body must be utf-8: {error}") from error
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise invalid_request(
                f"request body must be valid JSON: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise invalid_request("request body must be a JSON object")
        return parsed

    def _body_length(self, request: Request, route: Route) -> int:
        limit = route.max_body_bytes or self.policy.max_body_bytes
        encoding = (request.header("transfer-encoding") or "").strip().lower()
        if encoding:
            raise HttpRefusal(
                411,
                "length-required",
                "request body must declare Content-Length",
                details={"transfer_encoding": encoding},
            )
        raw = request.header("content-length")
        if not raw:
            return 0
        try:
            length = int(raw)
        except ValueError as error:
            raise invalid_request(
                f"Content-Length is not an integer: {error}"
            ) from error
        if length <= 0:
            return 0
        if length > limit:
            raise HttpRefusal(
                413,
                "request-too-large",
                f"Content-Length {length} exceeds {limit}-byte cap",
            )
        return length

    def _check_content_type(self, request: Request) -> None:
        if not self.policy.require_json_content_type:
            return
        declared = (request.header("content-type") or "").split(";")[0].strip().lower()
        if declared != "application/json":
            raise HttpRefusal(
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
        for operation in OPERATIONS:
            reason = (
                None
                if operation == CAPABILITY_OPERATION
                else self.provider.unavailable(operation)
            )
            operations.append(
                {
                    "operation": operation,
                    "available": reason is None,
                    "reason": reason,
                    "role": OPERATION_ROLES[operation],
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
                for route in ROUTES
            ],
            "unavailable_operations": [
                entry["operation"] for entry in operations if not entry["available"]
            ],
        }


#: Statuses whose response ends the connection. A refused body was never read,
#: so whatever the client was still sending has nowhere to go but the next
#: request's parser.
_CLOSING: frozenset[int] = frozenset({411, 413, 415})


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
