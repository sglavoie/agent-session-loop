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


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .credentials import (
    ROLE_OWNER as ROLE_OWNER,
)
from .http_application import (
    Application as Application,
)
from .http_auth import (
    _ROLE_RANK as _ROLE_RANK,
)
from .http_auth import (
    HttpRefusal as HttpRefusal,
)
from .http_auth import (
    LocalTokenAuthenticator as LocalTokenAuthenticator,
)
from .http_auth import (
    OwnerIdentityAuthenticator as OwnerIdentityAuthenticator,
)
from .http_auth import (
    Principal as Principal,
)
from .http_auth import (
    invalid_request as invalid_request,
)
from .http_auth import (
    role_satisfies as role_satisfies,
)
from .http_calls import (
    Authenticator as Authenticator,
)
from .http_calls import (
    Call as Call,
)
from .http_calls import (
    Provider as Provider,
)
from .http_constants import (
    COMMAND_NAME as COMMAND_NAME,
)
from .http_constants import (
    COMMAND_REDEEM_PATH as COMMAND_REDEEM_PATH,
)
from .http_constants import (
    MAX_REQUEST_BODY as MAX_REQUEST_BODY,
)
from .http_constants import (
    MODE_LOCAL as MODE_LOCAL,
)
from .http_constants import (
    MODE_REMOTE as MODE_REMOTE,
)
from .http_constants import (
    MODES as MODES,
)
from .http_constants import (
    SAFE_METHODS as SAFE_METHODS,
)
from .http_constants import (
    UPLINK_PATH as UPLINK_PATH,
)
from .http_messages import (
    _CLOSING as _CLOSING,
)
from .http_messages import (
    API_CONTENT_SECURITY_POLICY as API_CONTENT_SECURITY_POLICY,
)
from .http_messages import (
    DOCUMENT_CONTENT_SECURITY_POLICY as DOCUMENT_CONTENT_SECURITY_POLICY,
)
from .http_messages import (
    JSON_CACHE_CONTROL as JSON_CACHE_CONTROL,
)
from .http_messages import (
    SECURITY_HEADERS as SECURITY_HEADERS,
)
from .http_messages import (
    Request as Request,
)
from .http_messages import (
    Response as Response,
)
from .http_messages import (
    _no_body as _no_body,
)
from .http_messages import (
    normalize_headers as normalize_headers,
)
from .http_messages import (
    unavailable_from as unavailable_from,
)
from .http_payloads import (
    error_payload as error_payload,
)
from .http_payloads import (
    success_payload as success_payload,
)
from .http_policy import (
    LOCAL_POLICY as LOCAL_POLICY,
)
from .http_policy import (
    REMOTE_POLICY as REMOTE_POLICY,
)
from .http_policy import (
    RequestPolicy as RequestPolicy,
)
from .http_routes import (
    CAPABILITY_OPERATION as CAPABILITY_OPERATION,
)
from .http_routes import (
    OP_ARCHIVE_DELETE as OP_ARCHIVE_DELETE,
)
from .http_routes import (
    OP_ARCHIVE_LIST as OP_ARCHIVE_LIST,
)
from .http_routes import (
    OP_ARCHIVE_RESTORE as OP_ARCHIVE_RESTORE,
)
from .http_routes import (
    OP_ARCHIVE_SETTINGS as OP_ARCHIVE_SETTINGS,
)
from .http_routes import (
    OP_ARTIFACTS as OP_ARTIFACTS,
)
from .http_routes import (
    OP_CAPABILITIES as OP_CAPABILITIES,
)
from .http_routes import (
    OP_COMMAND_REDEEM as OP_COMMAND_REDEEM,
)
from .http_routes import (
    OP_COMMITS as OP_COMMITS,
)
from .http_routes import (
    OP_CONTROL_ABORT as OP_CONTROL_ABORT,
)
from .http_routes import (
    OP_CONTROL_REDIRECT as OP_CONTROL_REDIRECT,
)
from .http_routes import (
    OP_CONTROL_SELECT_HARNESS as OP_CONTROL_SELECT_HARNESS,
)
from .http_routes import (
    OP_CONTROL_STOP_AFTER_CURRENT as OP_CONTROL_STOP_AFTER_CURRENT,
)
from .http_routes import (
    OP_CONVERSATION_RECOVERY as OP_CONVERSATION_RECOVERY,
)
from .http_routes import (
    OP_EVENTS as OP_EVENTS,
)
from .http_routes import (
    OP_FINAL_REVIEW as OP_FINAL_REVIEW,
)
from .http_routes import (
    OP_HEALTH as OP_HEALTH,
)
from .http_routes import (
    OP_HISTORY as OP_HISTORY,
)
from .http_routes import (
    OP_INGEST as OP_INGEST,
)
from .http_routes import (
    OP_LAST_MESSAGE as OP_LAST_MESSAGE,
)
from .http_routes import (
    OP_LAUNCH as OP_LAUNCH,
)
from .http_routes import (
    OP_LOGS as OP_LOGS,
)
from .http_routes import (
    OP_PLAN as OP_PLAN,
)
from .http_routes import (
    OP_PLAN_ARCHIVE as OP_PLAN_ARCHIVE,
)
from .http_routes import (
    OP_PLANS_ARCHIVE as OP_PLANS_ARCHIVE,
)
from .http_routes import (
    OP_PROGRESS as OP_PROGRESS,
)
from .http_routes import (
    OP_QUESTION_REPLY as OP_QUESTION_REPLY,
)
from .http_routes import (
    OP_QUESTIONS as OP_QUESTIONS,
)
from .http_routes import (
    OP_QUEUE as OP_QUEUE,
)
from .http_routes import (
    OP_RECOVERY as OP_RECOVERY,
)
from .http_routes import (
    OP_REDIRECT_STATUS as OP_REDIRECT_STATUS,
)
from .http_routes import (
    OP_REGISTRY as OP_REGISTRY,
)
from .http_routes import (
    OP_REGISTRY_PIN as OP_REGISTRY_PIN,
)
from .http_routes import (
    OP_REGISTRY_UNPIN as OP_REGISTRY_UNPIN,
)
from .http_routes import (
    OP_REPLY as OP_REPLY,
)
from .http_routes import (
    OP_RESULT as OP_RESULT,
)
from .http_routes import (
    OP_STATE as OP_STATE,
)
from .http_routes import (
    OP_SUMMARY as OP_SUMMARY,
)
from .http_routes import (
    OP_UNLOCK as OP_UNLOCK,
)
from .http_routes import (
    OP_VERIFICATION as OP_VERIFICATION,
)
from .http_routes import (
    OPERATION_ROLES as OPERATION_ROLES,
)
from .http_routes import (
    OPERATIONS as OPERATIONS,
)
from .http_routes import (
    ROUTES as ROUTES,
)
from .http_routes import (
    ROUTES_BY_KEY as ROUTES_BY_KEY,
)
from .http_routes import (
    Route as Route,
)
from .http_routes import (
    _operation_roles as _operation_roles,
)
from .http_routes import (
    methods_for_path as methods_for_path,
)
from .http_routes import (
    operation_role as operation_role,
)
from .http_static import (
    STATIC_ASSET_CACHE_CONTROL as STATIC_ASSET_CACHE_CONTROL,
)
from .http_static import (
    STATIC_DEFAULT_MIME as STATIC_DEFAULT_MIME,
)
from .http_static import (
    STATIC_INDEX_CACHE_CONTROL as STATIC_INDEX_CACHE_CONTROL,
)
from .http_static import (
    STATIC_INDEX_FILE as STATIC_INDEX_FILE,
)
from .http_static import (
    STATIC_MIME_TYPES as STATIC_MIME_TYPES,
)
from .http_static import (
    STATIC_ROOT as STATIC_ROOT,
)
from .http_static import (
    STATIC_URL_PREFIX as STATIC_URL_PREFIX,
)
from .http_static import (
    StaticBundle as StaticBundle,
)
from .http_static import (
    compute_etag as compute_etag,
)
from .http_static import (
    parse_etag_list as parse_etag_list,
)
from .http_static import (
    shell_bundle as shell_bundle,
)

__all__ = [
    "API_CONTENT_SECURITY_POLICY",
    "CAPABILITY_OPERATION",
    "COMMAND_REDEEM_PATH",
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
