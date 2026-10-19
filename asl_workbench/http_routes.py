"""Http routes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import http_constants as constants
from .credentials import (
    ROLE_CONNECTOR,
    ROLE_OWNER,
    ROLE_PUBLIC,
    ROLE_SUMMARY,
)
from .transport import MAX_FRAME_BYTES

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


OP_QUESTION_REPLY = "question-reply"


OP_CONTROL_STOP_AFTER_CURRENT = "control-stop-after-current"


OP_CONTROL_ABORT = "control-abort"


OP_CONTROL_SELECT_HARNESS = "control-select-harness"


OP_CONTROL_REDIRECT = "control-redirect"


OP_UNLOCK = "unlock"


OP_COMMAND_REDEEM = "command-redeem"


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
        return self.method not in constants.SAFE_METHODS

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
        "/api/question-reply",
        OP_QUESTION_REPLY,
        ROLE_OWNER,
        "answer a live question without restarting its run",
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
    Route(
        "POST",
        constants.COMMAND_REDEEM_PATH,
        OP_COMMAND_REDEEM,
        ROLE_CONNECTOR,
        "exchange a dispatched command token for its in-memory body",
    ),
    # -- The connector's own path -------------------------------------------
    #
    # One route, both directions: the frame goes up in the request and
    # whatever the service is holding comes back down in the acknowledgement.
    # The Companion's `POST /v1/commands/poll` is deliberately not migrated --
    # it was a second round trip for something the first one can carry.
    Route(
        "POST",
        constants.UPLINK_PATH,
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
