"""Workbench HTTP server.

Stdlib-only local control plane for the Workbench. Exposes a small JSON
API surface backed by the existing Workbench registry, summary, control
queue, launch, and unlock helpers. Bind defaults to ``127.0.0.1`` so the
server is reachable only from the operator's local machine.

Authentication
--------------
Mutating endpoints (``POST``) require an ``Authorization: Bearer <token>``
header. The token is generated with :mod:`secrets` at server startup and
printed to the operator console alongside the bound URL. Read endpoints
(``GET``) are unauthenticated since the server is loopback-only and read
side effects are limited to file reads under the configured state and
plan directories.

Endpoints
---------
GET  ``/``                               - bundled Workbench shell (index.html).
GET  ``/static/<path>``                  - bundled vanilla assets (CSS, JS).
GET  ``/api/health``                     - liveness probe.
GET  ``/api/queue``                      - Workbench queue summaries.
GET  ``/api/plan?plan_dir=<path>``       - one plan detail summary.
GET  ``/api/recovery?plan_dir=<path>``   - recovery diagnosis for one plan.
GET  ``/api/registry``                   - registry payload.
GET  ``/api/artifacts?plan_dir=<path>``  - artifact paths and metadata.
GET  ``/api/state?plan_dir=<path>``      - latest runner state payload.
GET  ``/api/events?plan_dir=<path>``     - paginated events from ``events.jsonl``.
GET  ``/api/logs?plan_dir=<path>``       - bounded stdout/stderr tail or offset read.
GET  ``/api/last-message?plan_dir=<path>`` - bounded last-message text.
GET  ``/api/result?plan_dir=<path>``     - active turn ``result.json`` payload.
GET  ``/api/verification?plan_dir=<path>`` - active verification details.
GET  ``/api/commits?plan_dir=<path>``    - commit artifact metadata from events.
GET  ``/api/progress?plan_dir=<path>``   - parsed progress journal entries.
GET  ``/api/final-review?plan_dir=<path>`` - bounded final-review.md content.
GET  ``/api/questions?plan_dir=<path>``  - paused-run resolver prompt and Q&A context.
GET  ``/api/redirect-status?plan_dir=<path>`` - durable redirect operations and stages.
GET  ``/api/conversation-recovery?plan_dir=<path>`` - recovery decision and commands.
GET  ``/api/archive``                    - archived entries and cleanup candidates.
POST ``/api/registry/pin``               - upsert a registry pin.
POST ``/api/registry/unpin``             - remove a registry pin.
POST ``/api/plan/archive``               - archive one plan (plan + ASL state).
POST ``/api/plans/archive``              - bulk-archive many plans (per-entry results).
POST ``/api/plan/delete``                - backward-compatible archive alias.
POST ``/api/archive/restore``            - restore one archived entry to its origin.
POST ``/api/archive/delete``             - permanently purge archive entries.
POST ``/api/archive/settings``           - update archive retention days.
POST ``/api/launch``                     - spawn ``asl run`` for a plan.
POST ``/api/reply``                      - resume a paused run with reply.
POST ``/api/control/stop-after-current`` - enqueue stop-after-current.
POST ``/api/control/abort``              - enqueue abort of the in-flight turn.
POST ``/api/control/select-harness``     - enqueue harness selection.
POST ``/api/control/redirect``           - submit an urgent correction to the live turn.
POST ``/api/unlock``                     - remove a stale operational lock.

Retention
---------
``archive_retention_days`` marks archive entries as cleanup *candidates*; it no
longer empties them. ``GET /api/archive`` reports each entry's ``cleanup_state``
and a summary under ``archive_cleanup``, and removes nothing -- reading the
archive is a read. ``POST /api/archive/delete`` is the only route that empties
an entry, and it requires a typed count matching the selection.

Idempotency
-----------
``/api/control/redirect`` requires a client-chosen ``operation_id`` and answers
a resent request from the redirect journal, so a retry whose response was lost
returns the original receipt instead of submitting a second correction.
``/api/launch`` and ``/api/reply`` accept an optional ``launch_id`` that plays
the same role for spawning a run, and refuse outright while a run already holds
the plan or a process this server started for it is still alive. Both spawning
routes admit through ``workbench_operations``, so the receipt is a durable
per-plan journal rather than this process's memory: a retry that reaches a
restarted server, a second server, or the connector is recognised as the same
submission, and a server killed mid-launch leaves a record that says either
"certainly did not run" or "nobody can say" instead of nothing at all.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import secrets
import signal
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from asl_workbench import actions as workbench_action_rules
from asl_workbench import controls as portable_controls
from asl_workbench import details as workbench_detail_rules
from asl_workbench.details import DetailError, Window
from asl_workbench.operations import OperationError

from . import (
    intervention,
    plans,
    redirect_admission,
    redirect_cli,
    redirect_store,
    responses,
    state,
    workbench,
    workbench_actions,
    workbench_archive,
    workbench_catalog,
    workbench_controls,
    workbench_details,
)

TOKEN_BYTES = 32
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BODY = 1024 * 1024  # 1 MiB cap on request body.
AUTH_SCHEME = "Bearer "
COMMAND_NAME = "workbench"

# The read bounds are the portable package's, not this server's. Re-exported
# under their historic names so existing callers keep working, but defined once
# in `asl_workbench.details` so a remote connector serving the same routes
# cannot answer with a wider window than the local server would.
DEFAULT_LOG_BYTES = workbench_detail_rules.DEFAULT_LOG_BYTES
MAX_LOG_BYTES = workbench_detail_rules.MAX_READ_BYTES
DEFAULT_EVENT_LIMIT = workbench_detail_rules.DEFAULT_EVENT_LIMIT
MAX_EVENT_LIMIT = workbench_detail_rules.MAX_EVENT_LIMIT
DEFAULT_LAST_MESSAGE_BYTES = workbench_detail_rules.DEFAULT_LAST_MESSAGE_BYTES
DEFAULT_FINAL_REVIEW_BYTES = workbench_detail_rules.DEFAULT_FINAL_REVIEW_BYTES
_LOG_STREAMS = tuple(sorted(workbench_detail_rules.STREAM_ARTIFACTS))

#: Bounded window on a redirect status read. Re-exported from the portable rules
#: rather than restated: two surfaces with different windows would disagree about
#: which corrections exist, which is the one thing a bounded view must not do.
MAX_REDIRECT_OPERATIONS = portable_controls.MAX_REDIRECT_OPERATIONS
#: How many launch receipts one server remembers. A receipt exists so a browser
#: that reconnected and resent its launch gets its own answer back instead of a
#: second process; nothing needs it once the operator has moved on.
MAX_LAUNCH_RECEIPTS = 64
#: Same ceiling the redirect journal puts on an operation id, for the same
#: reason: an idempotency key is an identifier a client chooses, and an
#: unbounded one is an unbounded allocation on this side of the wire.
MAX_LAUNCH_ID_CHARS = redirect_store.REDIRECT_MAX_OPERATION_ID_CHARS

_JSONDict = dict[str, Any]

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_INDEX_FILE = "index.html"
STATIC_URL_PREFIX = "/static/"
STATIC_MIME_TYPES: dict[str, str] = {
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
# Cache policy: the HTML shell stays ``no-store`` so a server upgrade is
# never masked by a stale cached index; the JS/CSS bundle uses revalidation
# so the browser can short-circuit identical responses while still
# refreshing immediately after the operator restarts the server.
STATIC_INDEX_CACHE_CONTROL = "no-store"
STATIC_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"


@dataclass(frozen=True)
class ServerConfig:
    """Operator-supplied configuration for the Workbench server."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    state_dir: Path | None = None
    registry_path: Path | None = None
    plan_roots: tuple[str, ...] = ()
    include_default_dirs: bool = True


class WorkbenchHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server with a generated startup token."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        config: ServerConfig,
        *,
        token: str | None = None,
    ) -> None:
        super().__init__((config.host, config.port), WorkbenchRequestHandler)
        self.config = config
        self.token = token if token is not None else secrets.token_urlsafe(TOKEN_BYTES)
        # Launch bookkeeping. Deliberately in memory and deliberately per
        # server: it exists to stop *this* server from spawning a second run for
        # a plan while the first is still starting, and the durable defence
        # against two runs is the plan lock the children take. Persisting it
        # would add a second source of truth about run ownership without
        # covering anything the lock does not already cover once it is held.
        self._launch_lock = threading.Lock()
        self._plan_launch_locks: dict[str, threading.Lock] = {}
        self._launch_receipts: dict[tuple[str, str], _JSONDict] = {}
        self._launch_pids: dict[str, int] = {}

    def launch_guard(self, plan_dir: Path) -> threading.Lock:
        """Return the lock that serializes launch decisions for one plan.

        This server answers requests on concurrent threads, so the ownership
        check and the spawn have to happen under one lock. Without it two
        reconnecting browser tabs can both read "nothing holds this plan" and
        both spawn a run, and the loser only finds out by dying on the lock.
        """
        key = str(plan_dir)
        with self._launch_lock:
            guard = self._plan_launch_locks.get(key)
            if guard is None:
                guard = threading.Lock()
                self._plan_launch_locks[key] = guard
            return guard

    def recorded_launch(self, plan_dir: Path, launch_id: str) -> _JSONDict | None:
        """Return the answer an earlier identical launch request already got."""
        with self._launch_lock:
            recorded = self._launch_receipts.get((str(plan_dir), launch_id))
            return dict(recorded) if recorded is not None else None

    def record_launch(
        self, plan_dir: Path, launch_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Remember one launch so its request can be retried without repeating it."""
        with self._launch_lock:
            while len(self._launch_receipts) >= MAX_LAUNCH_RECEIPTS:
                # Oldest first: a dict preserves insertion order, and the
                # receipt a reconnecting browser asks for is a recent one.
                del self._launch_receipts[next(iter(self._launch_receipts))]
            self._launch_receipts[(str(plan_dir), launch_id)] = dict(payload)

    def record_launch_pid(self, plan_dir: Path, pid: int) -> None:
        """Remember the process this server spawned for one plan."""
        with self._launch_lock:
            self._launch_pids[str(plan_dir)] = pid

    def launched_pid(self, plan_dir: Path) -> int | None:
        """Return the last process this server spawned for one plan, if any."""
        with self._launch_lock:
            return self._launch_pids.get(str(plan_dir))

    def forget_launch_pid(self, plan_dir: Path) -> None:
        """Drop the recorded pid for a plan whose process has exited."""
        with self._launch_lock:
            self._launch_pids.pop(str(plan_dir), None)

    @property
    def bound_host(self) -> str:
        # AF_INET keeps server_address a (str, int) pair; the stdlib annotation
        # widens it to cover the Unix-socket case this server never uses.
        return cast(str, self.server_address[0])

    @property
    def bound_port(self) -> int:
        return self.server_address[1]


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
    plan_roots: Sequence[str | Path] = (),
    include_default_dirs: bool = True,
    token: str | None = None,
) -> WorkbenchHTTPServer:
    """Build a :class:`WorkbenchHTTPServer` from operator-facing inputs."""
    config = ServerConfig(
        host=host,
        port=port,
        state_dir=Path(state_dir) if state_dir is not None else None,
        registry_path=Path(registry_path) if registry_path is not None else None,
        plan_roots=tuple(str(root) for root in plan_roots),
        include_default_dirs=include_default_dirs,
    )
    return WorkbenchHTTPServer(config, token=token)


# ---------------------------------------------------------------------------
# Request handler


class WorkbenchRequestHandler(http.server.BaseHTTPRequestHandler):
    """Dispatch JSON requests to the Workbench helpers."""

    server_version = "ASLWorkbench/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Silence the default per-request access log so the operator-facing
        # console only carries deliberate startup output. Run-control events
        # remain the canonical audit trail for mutating actions.
        return

    # -- HTTP method entry points ------------------------------------------

    def do_GET(self) -> None:
        self._safe_dispatch("GET")

    def do_POST(self) -> None:
        self._safe_dispatch("POST")

    def _safe_dispatch(self, method: str) -> None:
        try:
            self._dispatch(method)
        except _BadRequestError as error:
            self._send_error(400, error.reason, error.message, details=error.details)
        except DetailError as error:
            # A refused detail read. The status and reason were decided in
            # `asl_workbench.details` rather than here, so the local server and
            # a remote connector refuse the same request the same way.
            self._send_error(
                error.status, error.reason, error.message, details=error.details
            )
        except workbench_controls.ControlRefused as error:
            # A control or correction refused by the service both surfaces
            # share. The status is derived from the refusal's own scope, so this
            # server and the bridge send a client after the same thing in their
            # two vocabularies rather than disagreeing about whose problem it is.
            self._send_error(
                _refusal_status(error),
                error.reason,
                error.message,
                details=(
                    {"plan_dir": str(error.plan_dir), **error.details}
                    if error.plan_dir is not None
                    else dict(error.details)
                ),
                retryable=error.retryable,
            )
        except intervention.InterventionError as error:
            # Reached only where a handler did not want to name the plan in the
            # refusal; `inspect_run` refuses an unreadable plan directory and
            # nothing else, which is a fact about the request.
            self._send_error(
                _intervention_status(error.reason),
                error.reason,
                error.message,
                details=dict(error.details),
                retryable=error.retryable,
            )
        except Exception as error:  # noqa: BLE001 - want one safety net
            self._send_error(500, "internal-error", str(error))

    # -- Routing ------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if method == "GET":
            if path == "/":
                self._serve_static_file(
                    STATIC_DIR / STATIC_INDEX_FILE,
                    STATIC_MIME_TYPES[".html"],
                    cache_control=STATIC_INDEX_CACHE_CONTROL,
                    use_etag=False,
                )
                return
            if path.startswith(STATIC_URL_PREFIX):
                self._handle_static_asset(path[len(STATIC_URL_PREFIX) :])
                return
        routes: dict[tuple[str, str], Callable[..., None]] = {
            ("GET", "/api/health"): self._handle_health,
            ("GET", "/api/queue"): self._handle_queue,
            ("GET", "/api/plan"): self._handle_plan,
            ("GET", "/api/recovery"): self._handle_recovery,
            ("GET", "/api/registry"): self._handle_registry,
            ("GET", "/api/artifacts"): self._handle_artifacts,
            ("GET", "/api/state"): self._handle_state,
            ("GET", "/api/events"): self._handle_events,
            ("GET", "/api/logs"): self._handle_logs,
            ("GET", "/api/last-message"): self._handle_last_message,
            ("GET", "/api/result"): self._handle_result,
            ("GET", "/api/verification"): self._handle_verification,
            ("GET", "/api/commits"): self._handle_commits,
            ("GET", "/api/progress"): self._handle_progress,
            ("GET", "/api/final-review"): self._handle_final_review,
            ("GET", "/api/questions"): self._handle_questions,
            ("GET", "/api/redirect-status"): self._handle_redirect_status,
            ("GET", "/api/conversation-recovery"): (self._handle_conversation_recovery),
            ("GET", "/api/archive"): self._handle_archive_list,
            ("POST", "/api/registry/pin"): self._handle_registry_pin,
            ("POST", "/api/registry/unpin"): self._handle_registry_unpin,
            ("POST", "/api/plan/archive"): self._handle_plan_archive,
            ("POST", "/api/plans/archive"): self._handle_plans_archive,
            # ``/api/plan/delete`` is a backward-compatible archive alias: it
            # keeps the historic route/confirmation contract but archives the
            # plan instead of permanently removing it.
            ("POST", "/api/plan/delete"): self._handle_plan_archive,
            ("POST", "/api/archive/restore"): self._handle_archive_restore,
            ("POST", "/api/archive/delete"): self._handle_archive_delete,
            ("POST", "/api/archive/settings"): self._handle_archive_settings,
            ("POST", "/api/launch"): self._handle_launch,
            ("POST", "/api/reply"): self._handle_reply,
            ("POST", "/api/control/stop-after-current"): (
                self._handle_control_stop_after_current
            ),
            ("POST", "/api/control/abort"): self._handle_control_abort,
            ("POST", "/api/control/select-harness"): (
                self._handle_control_select_harness
            ),
            ("POST", "/api/control/redirect"): self._handle_control_redirect,
            ("POST", "/api/unlock"): self._handle_unlock,
        }
        handler = routes.get((method, path))
        if handler is None:
            allowed = sorted({m for (m, p) in routes if p == path})
            if allowed:
                self._send_error(
                    405,
                    "method-not-allowed",
                    f"method {method} not supported on {path}",
                    allow=allowed,
                )
            else:
                self._send_error(404, "not-found", f"unknown route {path}")
            return

        mutating = method != "GET"
        if mutating and not self._authorize():
            return

        handler(query=query)

    # -- Auth ---------------------------------------------------------------

    def _authorize(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header.startswith(AUTH_SCHEME):
            provided = header[len(AUTH_SCHEME) :].strip()
            if provided and secrets.compare_digest(provided, self._server().token):
                return True
        self._send_error(
            401,
            "unauthorized",
            "missing or invalid Authorization bearer token",
        )
        return False

    # -- Body helpers -------------------------------------------------------

    def _read_json_body(self) -> _JSONDict:
        length_value = self.headers.get("Content-Length")
        if not length_value:
            return {}
        try:
            length = int(length_value)
        except ValueError as error:
            raise _BadRequestError(
                "invalid-request",
                f"Content-Length is not an integer: {error}",
            ) from error
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BODY:
            raise _BadRequestError(
                "request-too-large",
                f"Content-Length {length} exceeds {MAX_REQUEST_BODY}-byte cap",
            )
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _BadRequestError(
                "invalid-request",
                f"request body must be utf-8: {error}",
            ) from error
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise _BadRequestError(
                "invalid-request",
                f"request body must be valid JSON: {error}",
            ) from error
        if not isinstance(parsed, dict):
            raise _BadRequestError(
                "invalid-request", "request body must be a JSON object"
            )
        return parsed

    # -- Response helpers ---------------------------------------------------

    def _send_json(self, status: int, payload: _JSONDict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except BrokenPipeError, ConnectionResetError:
            # Client disconnected before the response could be flushed; the
            # server keeps running for the next request.
            return

    def _send_error(
        self,
        status: int,
        reason: str,
        error: str,
        *,
        details: _JSONDict | None = None,
        **extra: Any,
    ) -> None:
        payload = responses.error_payload(
            COMMAND_NAME,
            reason=reason,
            error=error,
            paused=False,
            details=details or {},
            **extra,
        )
        self._send_json(status, payload)

    def _send_success(self, **fields: Any) -> None:
        payload = responses.success_payload(COMMAND_NAME, **fields)
        self._send_json(200, payload)

    # -- Static asset serving ----------------------------------------------

    def _serve_static_file(
        self,
        path: Path,
        content_type: str,
        *,
        cache_control: str = STATIC_INDEX_CACHE_CONTROL,
        use_etag: bool = False,
    ) -> None:
        etag: str | None = None
        if use_etag:
            try:
                stat_result = path.stat()
            except FileNotFoundError:
                self._send_error(
                    404,
                    "not-found",
                    f"static asset not found: {path.name}",
                    details={"path": str(path)},
                )
                return
            except OSError as error:
                self._send_error(
                    500,
                    "os-error",
                    str(error),
                    details={"path": str(path)},
                )
                return
            etag = _compute_etag(stat_result)
            if_none_match = self.headers.get("If-None-Match", "")
            if etag and etag in _parse_etag_list(if_none_match):
                try:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", cache_control)
                    self.end_headers()
                except BrokenPipeError, ConnectionResetError:
                    pass
                return
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self._send_error(
                404,
                "not-found",
                f"static asset not found: {path.name}",
                details={"path": str(path)},
            )
            return
        except OSError as error:
            self._send_error(
                500,
                "os-error",
                str(error),
                details={"path": str(path)},
            )
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            if etag is not None:
                self.send_header("ETag", etag)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except BrokenPipeError, ConnectionResetError:
            return

    def _handle_static_asset(self, relative: str) -> None:
        if not relative or relative.startswith("/"):
            self._send_error(404, "not-found", "missing static asset path")
            return
        static_root = STATIC_DIR.resolve()
        try:
            candidate = (static_root / relative).resolve()
        except OSError as error:
            self._send_error(
                400,
                "invalid-request",
                f"invalid static path: {error}",
                details={"path": relative},
            )
            return
        try:
            candidate.relative_to(static_root)
        except ValueError:
            self._send_error(
                400,
                "invalid-request",
                "static path escapes the bundle",
                details={"path": relative},
            )
            return
        if not candidate.is_file():
            self._send_error(
                404,
                "not-found",
                f"static asset not found: {relative}",
                details={"path": relative},
            )
            return
        content_type = STATIC_MIME_TYPES.get(
            candidate.suffix.lower(), STATIC_DEFAULT_MIME
        )
        self._serve_static_file(
            candidate,
            content_type,
            cache_control=STATIC_ASSET_CACHE_CONTROL,
            use_etag=True,
        )

    # -- Read endpoints -----------------------------------------------------

    def _handle_health(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        self._send_success(reason="ok", status="ok")

    def _handle_queue(self, *, query: Mapping[str, list[str]]) -> None:
        sort_mode = _optional_query_value(query, "sort")
        if sort_mode is not None and sort_mode != "oldest":
            raise _BadRequestError(
                "invalid-request",
                "sort must be 'oldest' when provided",
                details={"sort": sort_mode},
            )
        config = self._server().config
        state_root = state.state_root(config.state_dir)
        try:
            registry = workbench.load_registry(config.registry_path)
        except workbench.WorkbenchError as error:
            self._send_error(500, "registry-unreadable", str(error))
            return
        # One non-reclaiming catalog scan answers the queue. Polling the queue
        # no longer empties expired archive entries or reclaims plan locks: a
        # read that repairs makes the answer a function of who looked and when.
        # Retention now runs only where an operator asked for it, on the
        # archive route; `observational-reads` in `asl_workbench.changes`
        # records the decision.
        scan = workbench_catalog.scan(
            state_dir=config.state_dir,
            registry=registry,
            plan_roots=config.plan_roots,
            include_default_dirs=config.include_default_dirs,
        )
        summaries: Sequence[workbench.WorkbenchSummary] = scan.summaries
        if sort_mode == "oldest":
            summaries = _sort_summaries_oldest_first(summaries)
        plan_payloads = [
            _augment_archivable(summary.to_payload(), summary) for summary in summaries
        ]
        self._send_success(
            reason="ok",
            plans=plan_payloads,
            registry_warnings=list(registry.warnings),
            discovery_warnings=list(scan.discovery.warnings),
            catalog_warnings=list(scan.projection_warnings),
            catalog_generation=scan.catalog.generation,
            catalog_complete=scan.catalog.complete,
            catalog_total=scan.catalog.total,
            state_root=str(state_root),
            state_root_exists=state_root.is_dir(),
            archive_retention_days=registry.archive_retention_days,
        )

    def _handle_plan(self, *, query: Mapping[str, list[str]]) -> None:
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        try:
            registry = workbench.load_registry(config.registry_path)
        except workbench.WorkbenchError as error:
            self._send_error(500, "registry-unreadable", str(error))
            return
        self._send_success(
            reason="ok",
            **workbench_details.read_plan(
                plan_dir, registry=registry, state_dir=config.state_dir
            ),
        )

    def _handle_recovery(self, *, query: Mapping[str, list[str]]) -> None:
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        self._send_success(
            reason="ok",
            **workbench_details.read_recovery(plan_dir, state_dir=config.state_dir),
        )

    def _handle_registry(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        config = self._server().config
        try:
            registry = workbench.load_registry(config.registry_path)
        except workbench.WorkbenchError as error:
            self._send_error(500, "registry-unreadable", str(error))
            return
        self._send_success(reason="ok", registry=registry.to_payload())

    def _handle_artifacts(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(reason="ok", **workbench_details.read_artifacts(reader))

    def _handle_state(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(reason="ok", **workbench_details.read_state(reader))

    def _handle_events(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(
            reason="ok",
            **workbench_details.read_events(
                reader,
                offset=self._require_non_negative_int(query, "offset", default=0),
                limit=self._require_bounded_int(
                    query,
                    "limit",
                    default=DEFAULT_EVENT_LIMIT,
                    maximum=MAX_EVENT_LIMIT,
                ),
                run_id=_optional_query_value(query, "run_id"),
            ),
        )

    def _handle_logs(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        # `artifact` is the named form; `stream` is its two-value alias and
        # `path` the compatibility form. Which of the three is acceptable, and
        # what each resolves to, is decided by the scope rather than here --
        # every one of them lands inside the selected plan or is refused.
        self._send_success(
            reason="ok",
            **workbench_details.read_logs(
                reader,
                stream=_optional_query_value(query, "stream"),
                artifact=_optional_query_value(query, "artifact"),
                path=_optional_query_value(query, "path"),
                window=Window(
                    offset=_optional_int_query(query, "offset"),
                    tail_lines=_optional_int_query(query, "tail_lines"),
                    max_bytes=self._require_bounded_int(
                        query,
                        "max_bytes",
                        default=DEFAULT_LOG_BYTES,
                        maximum=MAX_LOG_BYTES,
                    ),
                ),
            ),
        )

    def _handle_last_message(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(
            reason="ok",
            **workbench_details.read_last_message(
                reader,
                max_bytes=self._require_bounded_int(
                    query,
                    "max_bytes",
                    default=DEFAULT_LAST_MESSAGE_BYTES,
                    maximum=MAX_LOG_BYTES,
                ),
            ),
        )

    def _handle_result(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(reason="ok", **workbench_details.read_result(reader))

    def _handle_verification(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(reason="ok", **workbench_details.read_verification(reader))

    def _handle_commits(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(
            reason="ok",
            **workbench_details.read_commits(
                reader, run_id=_optional_query_value(query, "run_id")
            ),
        )

    def _handle_progress(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(reason="ok", **workbench_details.read_progress(reader))

    def _handle_final_review(self, *, query: Mapping[str, list[str]]) -> None:
        reader = self._open_plan(query)
        self._send_success(
            reason="ok",
            **workbench_details.read_final_review(
                reader,
                max_bytes=self._require_bounded_int(
                    query,
                    "max_bytes",
                    default=DEFAULT_FINAL_REVIEW_BYTES,
                    maximum=MAX_LOG_BYTES,
                ),
            ),
        )

    def _handle_questions(self, *, query: Mapping[str, list[str]]) -> None:
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        self._send_success(
            reason="ok",
            **workbench_details.read_questions(plan_dir, state_dir=config.state_dir),
        )

    # -- Mutating endpoints -------------------------------------------------

    def _send_refusal(self, error: workbench_archive.ArchiveRefused) -> None:
        """Render one archive refusal as HTTP. The whole translation layer.

        The status comes from the refusal rather than from a table kept here, so
        a surface without status codes -- the CLI, the connector -- translates
        the same refusal through its own vocabulary without this server having
        to agree with it first.
        """
        self._send_error(
            error.status, error.reason, error.message, details=error.details
        )

    def _handle_registry_pin(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        label = _optional_string(body, "label")
        profile = _coerce_profile_payload(body.get("profile"))
        config = self._server().config
        try:
            update = workbench_archive.pin_plan(
                plan_dir,
                registry_path=config.registry_path,
                label=label,
                profile=profile,
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(reason="ok", **update.to_payload())

    def _handle_registry_unpin(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        config = self._server().config
        try:
            update = workbench_archive.unpin_plan(
                plan_dir, registry_path=config.registry_path
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(reason="ok", **update.to_payload())

    def _handle_plan_archive(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        confirm_plan_dir = _require_string(body, "confirm_plan_dir")
        config = self._server().config
        try:
            result = workbench_archive.archive_one(
                plan_dir,
                confirm_plan_dir,
                state_dir=config.state_dir,
                registry_path=config.registry_path,
                plan_roots=config.plan_roots,
                include_default_dirs=config.include_default_dirs,
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(reason="ok", **result.to_payload())

    def _handle_plans_archive(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        config = self._server().config
        try:
            bulk = workbench_archive.archive_many(
                body.get("plan_dirs"),
                state_dir=config.state_dir,
                registry_path=config.registry_path,
                plan_roots=config.plan_roots,
                include_default_dirs=config.include_default_dirs,
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(reason="ok", **bulk.to_payload())

    def _handle_archive_list(self, *, query: Mapping[str, list[str]]) -> None:
        """Report the archive and what retention makes of it. Delete nothing.

        This route used to run a maintenance pass first, so opening the archive
        tab was the act that permanently emptied expired entries -- an operator
        who went looking for something old destroyed it by looking. Now the
        retention window only marks candidates, and emptying them is
        ``POST /api/archive/delete`` with a typed count.
        """
        del query
        config = self._server().config
        try:
            view = workbench_archive.review_archive(
                state_dir=config.state_dir, registry_path=config.registry_path
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(reason="ok", **view.to_payload())

    def _handle_archive_restore(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        archive_id = _require_string(body, "archive_id")
        config = self._server().config
        try:
            result = workbench_archive.restore_entry(
                archive_id,
                state_dir=config.state_dir,
                registry_path=config.registry_path,
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(reason="ok", **result.to_payload())

    def _handle_archive_delete(self, *, query: Mapping[str, list[str]]) -> None:
        """The one route that destroys, and the only one that ever did."""
        del query
        body = self._read_json_body()
        config = self._server().config
        try:
            result = workbench_archive.purge_entries(
                body.get("archive_ids"),
                body.get("confirm_count"),
                state_dir=config.state_dir,
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(reason="ok", **result.to_payload())

    def _handle_archive_settings(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        config = self._server().config
        try:
            update = workbench_archive.set_retention(
                body.get("archive_retention_days"),
                registry_path=config.registry_path,
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(
            reason="ok",
            **update.to_payload(),
            archive_retention_days=update.registry.archive_retention_days,
        )

    def _handle_launch(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        self._spawn_run(body, reply_text=None)

    def _handle_reply(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        # Read before the guard is taken so a request with no reply at all is
        # refused as malformed rather than counted as a launch attempt.
        self._spawn_run(body, reply_text=_require_string(body, "reply_text"))

    def _spawn_run(self, body: Mapping[str, Any], *, reply_text: str | None) -> None:
        """Start one run for a plan, at most once per plan and per ``launch_id``.

        The two spawning endpoints share this path because they share the
        failure an operator actually hits: a browser that reconnects, or an
        impatient second click, resending a launch that already succeeded. Four
        things stand between that and a second process, in this order:

        1. Durable admission. ``workbench_actions`` records the submission in
           the plan's own operation journal under an exclusive lock, so a
           resent ``launch_id`` is recognised as the same submission even by a
           server that did not answer the first one -- and a submission that
           arrives while another is genuinely in flight is refused rather than
           spawned. This is also the write that makes an interrupted launch
           accountable afterwards, because dispatch is recorded before the
           process is started rather than after.
        2. ``launch_id`` replay. A resent request carrying the id of one that
           already ran gets that run's own answer back, so a retry is the same
           request rather than a second one.
        3. The ownership pre-check below, which reports a live run as a conflict
           instead of spawning a process that dies on the plan lock. The plan
           lock remains the authority on execution; admission only keeps a
           second attempt from being started in front of it.
        4. The per-plan guard, which makes 2 and 3 mean anything on a threaded
           server by serializing check-and-spawn for one plan.

        A client that supplies no ``launch_id`` gets a minted one, so its
        submission is still accounted for even though nothing can recognize a
        retry of it as a retry.

        All four live in ``workbench_actions``, which is also what the bridge
        calls. The two surfaces used to admit, spawn, and settle by their own
        arrangements; a browser click and a supervisor's command now differ
        only in how they arrived.
        """
        plan_dir = _require_string(body, "plan_dir")
        launch_id = _optional_launch_id(body)
        profile = _coerce_profile_payload(body.get("profile"))
        config = self._server().config
        action = (
            workbench_action_rules.ACTION_LAUNCH
            if reply_text is None
            else workbench_action_rules.ACTION_RESUME
        )
        try:
            canonical = plans.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            self._send_error(
                400,
                workbench.LAUNCH_REASON_INVALID_PLAN,
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            )
            return
        with self._server().launch_guard(canonical):
            try:
                outcome = workbench_actions.perform(
                    canonical,
                    action=action,
                    operation_id=launch_id,
                    reply_text=reply_text,
                    profile=profile,
                    state_dir=config.state_dir,
                    guard=self._launch_conflict,
                )
            except OperationError as error:
                self._send_error(
                    error.status, error.reason, error.message, details=error.details
                )
                return
            except intervention.InterventionError as error:
                self._send_error(
                    409, error.reason, error.message, details=error.details
                )
                return
            except workbench_actions.ActionRefused as error:
                self._send_error(
                    error.status,
                    error.reason,
                    error.message,
                    details={"plan_dir": str(canonical)},
                )
                return
            except workbench.LaunchError as error:
                self._send_error(
                    400,
                    error.reason,
                    error.message,
                    details={"plan_dir": plan_dir},
                )
                return
            if outcome.replayed:
                self._answer_prior_submission(outcome, canonical, launch_id)
                return
            assert outcome.launch is not None  # a carried-out spawn has one
            payload = outcome.launch.to_payload()
            self._server().record_launch_pid(canonical, outcome.launch.pid)
            if launch_id is not None:
                self._server().record_launch(canonical, launch_id, payload)
        self._send_success(
            reason="ok",
            launch=payload,
            launch_id=launch_id,
            replayed=False,
            operation=outcome.operation,
        )

    def _answer_prior_submission(
        self,
        outcome: workbench_actions.ActionOutcome,
        plan_dir: Path,
        launch_id: str | None,
    ) -> None:
        """Answer a submission the journal has already seen.

        A replay gets this server's own receipt when it happens to hold one,
        and the journal row otherwise. The distinction is worth keeping rather
        than collapsing: only the process that answered the first request can
        say what pid it started, so a cross-process replay reports *that the
        submission is already recorded* instead of inventing a launch payload
        for a process it never saw.
        """
        recorded = (
            self._server().recorded_launch(plan_dir, launch_id)
            if launch_id is not None
            else None
        )
        admission = outcome.admission
        self._send_success(
            reason="ok",
            launch=recorded,
            launch_id=launch_id,
            replayed=True,
            operation=(
                admission.to_payload() if admission is not None else outcome.operation
            ),
        )

    def _launch_conflict(self, facts: intervention.RunFacts) -> tuple[str, str] | None:
        """Name what already owns this plan, or ``None`` when nothing does.

        Two checks, because neither sees the other's window. Once a child has
        taken the plan lock, the shared service reports a live run. Before it
        has -- the seconds a reconnecting browser is most likely to resend in --
        nothing durable says so yet, and the pid this server spawned is the only
        evidence a launch is already under way.

        The facts are the ones the shared action already read, rather than a
        second reading of its own: a guard that answers about a different
        instant than the operation it guards is a guard with a gap in it.

        A paused run is deliberately not a conflict: resuming one is exactly
        what ``/api/reply`` is for.
        """
        plan_dir = facts.plan_dir
        if facts.run_active:
            return (
                "run-already-active",
                "a run already holds this plan"
                + (f": {facts.run_id}" if facts.run_id else ""),
            )
        pid = self._server().launched_pid(plan_dir)
        if pid is not None:
            if state.is_pid_running(pid):
                return (
                    "launch-already-in-flight",
                    f"this server already started a run for this plan (pid {pid})",
                )
            # Forget a process that has exited. Remembering it costs nothing
            # today and everything the day the operating system reuses its
            # number: a long-dead pid that reads as alive again would refuse a
            # launch that nothing is actually running.
            self._server().forget_launch_pid(plan_dir)
        return None

    def _dispatch_control(
        self, kind: str, body: Mapping[str, Any], **kwargs: Any
    ) -> workbench_controls.ControlOutcome:
        """Queue one control through the service both surfaces share.

        The service carries the same best-effort wakeup the CLI producer sends,
        so this surface is not the slower way to reach a live runner, and it
        reads the run the same way every other reader does, so the activity an
        abort reports here is not a second parse of the lock.
        """
        return workbench_controls.dispatch_control(
            _require_string(body, "plan_dir"),
            kind,
            state_dir=self._server().config.state_dir,
            command_id=_optional_string(body, "command_id"),
            **kwargs,
        )

    def _handle_control_stop_after_current(
        self, *, query: Mapping[str, list[str]]
    ) -> None:
        del query
        body = self._read_json_body()
        outcome = self._dispatch_control(
            portable_controls.CONTROL_STOP_AFTER_CURRENT, body
        )
        self._send_queued_control(outcome)

    def _handle_control_abort(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        outcome = self._dispatch_control(
            portable_controls.CONTROL_ABORT,
            body,
            # Passed through untouched: ``append_command`` is the only validator
            # of grace and reason, so this surface cannot accept a window the
            # CLI would reject or reject one it would accept.
            grace_seconds=body.get("grace_seconds"),
            reason=body.get("reason"),
        )
        queued = outcome.command
        activity = outcome.activity
        self._send_json(
            202,
            responses.success_payload(
                COMMAND_NAME,
                reason="accepted",
                accepted=True,
                run_active=activity.runner_present,
                turn_active=activity.turn_active,
                grace_seconds=queued.grace_seconds,
                # The CLI calls this field `reason`; here the top-level
                # `reason` is the HTTP response status, so the abort's own
                # reason keeps a distinct name.
                abort_reason=queued.reason,
                note=activity.note,
                queued_command=queued.to_payload(),
                signal=outcome.dispatch.signal.to_payload(),
            ),
        )

    def _handle_control_select_harness(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        outcome = self._dispatch_control(
            portable_controls.CONTROL_SELECT_HARNESS,
            body,
            harness=_require_string(body, "harness"),
        )
        self._send_queued_control(outcome)

    def _send_queued_control(self, outcome: workbench_controls.ControlOutcome) -> None:
        """Answer a control whose only promise is that it was written down."""
        self._send_json(
            202,
            responses.success_payload(
                COMMAND_NAME,
                reason="accepted",
                accepted=True,
                queued_command=outcome.command.to_payload(),
                signal=outcome.dispatch.signal.to_payload(),
            ),
        )

    # -- Redirect -----------------------------------------------------------

    @staticmethod
    def _redirect_payload(
        submission: workbench_controls.RedirectSubmission,
    ) -> _JSONDict:
        """Name the shared submission's facts the way this API already does.

        Every field comes from the service, which re-read the journal after the
        submission rather than building an answer from the outcome in hand, so
        the status a browser renders and the status
        ``asl control redirect-status`` computes next are one projection of the
        same bytes.
        """
        return {
            # Null exactly when nothing was persisted. Echoing the client's own
            # id for a refusal that never reached the journal would invite it to
            # ask after a correction that does not exist.
            "operation_id": submission.operation_id,
            "plan_dir": str(submission.facts.plan_dir),
            "run_id": submission.facts.run_id,
            # Null on a replay: that call submitted nothing, so it has no
            # submission outcome to report and `operation` carries the truth.
            "outcome": submission.outcome,
            # The CLI and the bridge both call this `reason`; here the top-level
            # `reason` is the HTTP response status, so a refusal keeps its own
            # name.
            "refusal": submission.reason,
            "target": submission.target,
            "waited_seconds": round(submission.waited_seconds, 3),
            "replayed": submission.replayed,
            "operation": submission.operation_payload(),
            "journal_complete": submission.journal_complete,
        }

    def _handle_control_redirect(self, *, query: Mapping[str, list[str]]) -> None:
        """Submit one urgent correction against the turn this run is on now.

        Replay is served out of the redirect journal before the freshness proof
        is checked, and that ordering is the whole reason a browser may retry
        this request. A correction that landed moves the plan, which moves the
        action token, which would make the retry of a request whose response was
        lost look like a stale second correction. Asking the journal first makes
        the ``operation_id`` the client already holds the answer.

        What is *not* short-circuited is the request itself. The body is read
        and compared against the record that id already names, because a retry
        is only a retry when it carries the same correction: an operator whose
        response was lost still has an editable textarea, and reporting their
        edited words as a successful replay of the words the worker actually got
        is the one falsehood this path exists to avoid.
        """
        del query
        body = self._read_json_body()
        try:
            submission = workbench_controls.submit_correction(
                _require_string(body, "plan_dir"),
                operation_id=_require_string(body, "operation_id"),
                text=body.get("text"),
                state_dir=self._server().config.state_dir,
                expected_target_raw=body.get("expected_target"),
                wait_seconds_raw=body.get("wait_seconds"),
                action_token=_optional_string(body, "action_token"),
                run_id=_optional_string(body, "run_id"),
            )
        except workbench_controls.RedirectConflict as error:
            # Nothing was written, and nothing about the recorded request
            # changed. 409 rather than 400 because the body is well formed:
            # what it contradicts is a durable record, and the client resolves
            # that by reconciling the id, not by fixing a field.
            self._send_error(
                409,
                error.reason,
                error.message,
                details={
                    "plan_dir": str(error.plan_dir),
                    "operation_id": error.operation_id,
                    "conflict": error.conflict,
                    "redirect": self._redirect_payload(error.submission),
                },
                retryable=False,
            )
            return
        receipt = self._redirect_payload(submission)
        if submission.replayed:
            # 200 rather than 202: this call accepted nothing, it recognized a
            # request that was already accepted once.
            self._send_success(reason="ok", redirect=receipt)
            return
        if not submission.persisted:
            # Nothing reached the journal, so there is nothing to ask after
            # later. Reporting this as accepted is the one falsehood the whole
            # redirect path is built to avoid.
            self._send_error(
                409,
                submission.reason or redirect_admission.REFUSE_NO_ACTIVE_TURN,
                f"redirect was refused: {submission.reason}",
                details={
                    "plan_dir": str(submission.facts.plan_dir),
                    "redirect": receipt,
                },
                retryable=(submission.reason in redirect_admission.UNAVAILABLE_REASONS),
            )
            return
        self._send_json(
            202,
            responses.success_payload(
                COMMAND_NAME,
                reason="accepted",
                # Persisted, which is what this surface can promise. Whether the
                # runner took it is `redirect.operation.stage`, and an
                # `unavailable` or `uncertain` outcome says so there.
                accepted=submission.accepted,
                redirect=receipt,
            ),
        )

    def _handle_redirect_status(self, *, query: Mapping[str, list[str]]) -> None:
        """Project this plan's durable redirect state, writing nothing.

        A read, and unauthenticated like every other read, because the client
        most in need of it is the one that just lost its own record of what it
        submitted. Every field is derived through :mod:`.redirect_cli`, so the
        stage a browser renders and the stage an operator reads out of
        ``asl control redirect-status`` are one derivation of the same facts.
        """
        view = workbench_controls.status_view(
            self._require_query_value(query, "plan_dir"),
            state_dir=self._server().config.state_dir,
            operation_ids=query.get("operation_id", ()),
        )
        facts = view.facts
        report = view.report
        live_target = view.live_target
        operations = view.operation_payloads()
        self._send_success(
            reason="ok",
            plan_dir=str(facts.plan_dir),
            run_id=facts.run_id,
            journal_path=report.journal_path,
            owner=report.owner.liveness,
            owner_run_id=report.owner.run_id,
            live_target=(live_target.to_payload() if live_target is not None else None),
            operations=operations,
            uncertain_operation_ids=[
                operation["operation_id"]
                for operation in operations
                if operation["stage"] == redirect_cli.STAGE_UNCERTAIN
            ],
            # `journal_complete` is the honesty flag and `truncated` is its
            # projection counterpart: a torn journal means a record may be
            # missing from the source, a full window that one is missing here.
            journal_complete=report.complete,
            torn_tail=report.torn_tail,
            unreadable_lines=report.unreadable_lines,
            truncated=view.truncated,
            checked_at=report.checked_at,
            conversations=report.conversations.to_payload(),
            # The freshness proof a redirect must carry back. Handed out by the
            # read so a composer never has to guess at one.
            action_token=intervention.action_token(facts),
            permitted_actions=list(intervention.permitted_actions(facts)),
            disabled_reasons=intervention.disabled_reasons(facts),
        )

    def _handle_conversation_recovery(self, *, query: Mapping[str, list[str]]) -> None:
        """Report which recovery a pause is waiting on, and how to authorize it.

        A read, and deliberately only a read. Performing the recovery here would
        persist the operator's one-use authorization in the one place it must
        never live: a request a browser can replay. The pause-to-decision mapping
        and both commands come from :mod:`.intervention` and
        :mod:`.redirect_cli`, so this response, the recovery card, and
        ``asl control recover-conversation`` cannot hand an operator three
        different answers about the same pause.
        """
        view = workbench_controls.recovery_view(
            self._require_query_value(query, "plan_dir"),
            state_dir=self._server().config.state_dir,
        )
        classified = view.decision
        self._send_success(
            reason="ok",
            plan_dir=str(view.facts.plan_dir),
            run_id=view.facts.run_id,
            recovery_required=classified.required,
            # Reported whether or not it maps to a decision: "paused, and no
            # manual recovery is what it is waiting on" is an answer, while an
            # unexplained null is a second question.
            pause_reason=classified.pause_reason,
            decision=classified.decision,
            auto_resume=classified.auto_resume,
            authorized_by=classified.authorized_by,
            inspect_command=view.inspect_command,
            authorization_command=view.authorization_command,
            # Both describe the authorization command, so both are false and
            # null where there is none. `predecessor-not-stopped` has no
            # authorization at all, and telling an operator its absent
            # authorization is one-use would read as an invitation to find it.
            one_use=view.one_use,
            note=view.note,
            conversations=view.report.conversations.to_payload(),
            # The corrections a fresh start would still carry. Reported here so
            # an operator authorizing one sees what it inherits rather than
            # discovering it in the successor's first prompt.
            retained_operations=view.retained_payloads(),
        )

    def _handle_unlock(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        force = bool(body.get("force", False))
        config = self._server().config
        try:
            result = workbench_archive.unlock(
                plan_dir, state_dir=config.state_dir, force=force
            )
        except workbench_archive.ArchiveRefused as error:
            self._send_refusal(error)
            return
        self._send_success(
            reason="ok", **workbench_archive.unlock_payload(plan_dir, result)
        )

    # -- Helpers ------------------------------------------------------------

    def _server(self) -> WorkbenchHTTPServer:
        # `BaseRequestHandler.server` is declared as the base class, and this
        # handler is only ever constructed by `WorkbenchHTTPServer`. Asserting
        # it is what carries the concrete type without an inline suppression.
        server = self.server
        assert isinstance(server, WorkbenchHTTPServer)
        return server

    def _require_query_value(self, query: Mapping[str, list[str]], key: str) -> str:
        values = query.get(key)
        if not values:
            raise _BadRequestError(
                "invalid-request", f"missing required query parameter {key!r}"
            )
        value = values[0].strip()
        if not value:
            raise _BadRequestError(
                "invalid-request", f"query parameter {key!r} must not be empty"
            )
        return value

    def _require_non_negative_int(
        self,
        query: Mapping[str, list[str]],
        key: str,
        *,
        default: int,
    ) -> int:
        raw = _optional_int_query(query, key)
        value = default if raw is None else raw
        if value < 0:
            raise _BadRequestError(
                "invalid-request",
                f"query parameter {key!r} must be non-negative",
            )
        return value

    def _require_bounded_int(
        self,
        query: Mapping[str, list[str]],
        key: str,
        *,
        default: int,
        maximum: int,
    ) -> int:
        value = self._require_non_negative_int(query, key, default=default)
        if value > maximum:
            raise _BadRequestError(
                "invalid-request",
                f"query parameter {key!r} exceeds maximum {maximum}",
            )
        return value

    def _open_plan(
        self, query: Mapping[str, list[str]]
    ) -> workbench_details.PlanReader:
        """Open the requested plan for one detail read, reclaiming nothing.

        The single door every detail route goes through. Classification happens
        once per request and produces the scope that decides which files that
        request may reach, so no handler below resolves a path of its own.
        """
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        return workbench_details.open_plan(plan_dir, state_dir=config.state_dir)


# ---------------------------------------------------------------------------
# Module-level helpers


class _BadRequestError(Exception):
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        details: _JSONDict | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details or {}


def _require_string(body: Mapping[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise _BadRequestError(
            "invalid-request",
            f"missing required string field {key!r}",
        )
    if not value:
        raise _BadRequestError(
            "invalid-request",
            f"field {key!r} must not be empty",
        )
    return value


# Plan states that hold an active operational lock; archiving one is blocked
# by the same active-lock guard the archive helper enforces.
_ARCHIVE_BLOCKING_STATES = frozenset({"verifying", "running"})


def _sort_summaries_oldest_first(
    summaries: Sequence[workbench.WorkbenchSummary],
) -> list[workbench.WorkbenchSummary]:
    """Sort plans oldest-activity-first; plans with no activity sort oldest.

    Complete plans are ordered by ``completed_at`` so cleanup surfaces the
    longest-finished plans first; every other state falls back to
    ``last_activity_at``. Missing timestamps sort oldest either way.
    """

    def sort_key(summary: workbench.WorkbenchSummary) -> tuple[int, str]:
        if summary.primary_state == "complete":
            activity = summary.completed_at
        else:
            activity = summary.last_activity_at
        if not activity:
            # No recorded activity is treated as the oldest possible.
            return (0, "")
        return (1, activity)

    return sorted(summaries, key=sort_key)


def _augment_archivable(
    payload: _JSONDict, summary: workbench.WorkbenchSummary
) -> _JSONDict:
    """Add ``archivable`` / ``archive_blocker`` fields to a queue plan payload."""
    blocked = summary.primary_state in _ARCHIVE_BLOCKING_STATES
    payload["archivable"] = not blocked
    payload["archive_blocker"] = "active-lock" if blocked else None
    return payload


#: The status each shared refusal scope answers with. Written as a table so the
#: mapping is a statement rather than a chain of conditionals: a control refused
#: by the shared service is refused for exactly one of these reasons, and each
#: reason has exactly one thing the client should do next.
_REFUSAL_STATUS: Mapping[str, int] = {
    # The body is wrong, and resending it unchanged cannot succeed.
    portable_controls.SCOPE_REQUEST: 400,
    # A durable record contradicts the request. Well formed, so not a 400: what
    # resolves it is reconciling the id, not fixing a field.
    portable_controls.SCOPE_CONFLICT: 409,
    # Retryable and unmistakably not a refusal: a journal this server cannot
    # read is a fact about the machine, and answering as though the plan held no
    # corrections would be the wrong kind of wrong.
    portable_controls.SCOPE_UNAVAILABLE: 503,
}


def _refusal_status(error: workbench_controls.ControlRefused) -> int:
    """Map a shared refusal to the status that says whose problem it is."""
    fixed = _REFUSAL_STATUS.get(error.scope)
    if fixed is not None:
        return fixed
    if error.scope == portable_controls.SCOPE_INTERVENTION:
        return _intervention_status(error.reason)
    # A submission that was refused describes the correction; one that failed
    # describes this machine, and `retryable` is the service's own word for
    # which happened.
    return 500 if error.retryable else 400


#: Service refusals that are facts about the request rather than about the run.
#: Resending one of these unchanged will never succeed, which is what separates
#: them from a stale token or an action the run does not currently permit.
_INTERVENTION_REQUEST_REASONS = frozenset(
    {
        intervention.REASON_INVALID_PLAN,
        intervention.REASON_INVALID_ACTION,
        intervention.REASON_EMPTY_TEXT,
    }
)


def _intervention_status(reason: str) -> int:
    """Map a shared-service refusal to the status that says whose problem it is.

    409 is the default on purpose: a stale action token, an action the run no
    longer permits, and a run that moved on are all resolved by re-reading the
    plan, not by rewriting the body, and 400 would tell a client to do the one
    thing that cannot help.
    """
    return 400 if reason in _INTERVENTION_REQUEST_REASONS else 409


def _optional_launch_id(body: Mapping[str, Any]) -> str | None:
    """Narrow the client's idempotency key, or refuse one this side cannot hold.

    Bounded and stripped for the same reason the redirect journal bounds an
    operation id: the key is chosen by the caller and remembered here, and a key
    that differs from another only by surrounding whitespace would read as a
    second launch to the receipt table and as the same one to the operator.
    """
    value = _optional_string(body, "launch_id")
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        raise _BadRequestError(
            "invalid-request", "field 'launch_id' must not be blank when provided"
        )
    if len(trimmed) > MAX_LAUNCH_ID_CHARS:
        raise _BadRequestError(
            "invalid-request",
            f"field 'launch_id' exceeds {MAX_LAUNCH_ID_CHARS} characters",
        )
    return trimmed


def _optional_string(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BadRequestError(
            "invalid-request",
            f"field {key!r} must be a string when provided",
        )
    return value or None


def _coerce_commit_mode(value: Any) -> workbench.CommitMode | None:
    """Narrow a request's ``commit_mode`` to the vocabulary, or refuse it.

    Unlike the harness names -- which the launch path resolves against a
    registry -- ``commit_mode`` is a two-value closed set, so an unrecognized
    value is a malformed request and nothing else. Refusing it here reports it
    as one instead of letting it travel to the launch as an invalid profile.
    """
    text = _coerce_optional_str(value)
    if text is None:
        return None
    mode = next((mode for mode in workbench.COMMIT_MODES if mode == text), None)
    if mode is None:
        raise _BadRequestError(
            "invalid-request",
            "field 'commit_mode' must be one of "
            + ", ".join(repr(each) for each in workbench.COMMIT_MODES),
        )
    return mode


def _coerce_profile_payload(raw: Any) -> workbench.LaunchProfile | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _BadRequestError(
            "invalid-request", "profile must be a JSON object when provided"
        )
    return workbench.LaunchProfile(
        cwd=_coerce_optional_str(raw.get("cwd")),
        harness=_coerce_optional_str(raw.get("harness")),
        harness_model=_coerce_optional_str(raw.get("harness_model")),
        effort=_coerce_optional_str(raw.get("effort")),
        timeout=_coerce_optional_int(raw.get("timeout")),
        commit_mode=_coerce_commit_mode(raw.get("commit_mode")),
        review_harness=_coerce_optional_str(raw.get("review_harness")),
        review_harness_model=_coerce_optional_str(raw.get("review_harness_model")),
        max_review_fix_cycles=_coerce_max_review_fix_cycles(
            raw.get("max_review_fix_cycles")
        ),
        unsafe_bypass=_coerce_optional_bool(raw.get("unsafe_bypass")),
        allow_not_loop_ready=_coerce_optional_bool(raw.get("allow_not_loop_ready")),
        note=_coerce_optional_str(raw.get("note")),
    )


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BadRequestError(
            "invalid-request", "profile fields must be strings when set"
        )
    text = value.strip()
    return text or None


def _coerce_optional_int(value: Any, *, field_name: str = "timeout") -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadRequestError(
            "invalid-request",
            f"profile {field_name} must be an integer when set",
        )
    return value


def _coerce_max_review_fix_cycles(value: Any) -> int | None:
    cap = _coerce_optional_int(value, field_name="max_review_fix_cycles")
    if cap is not None and cap < 0:
        raise _BadRequestError(
            "invalid-request",
            "profile max_review_fix_cycles must be zero or a positive integer when set",
        )
    return cap


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _BadRequestError(
            "invalid-request",
            "profile boolean fields must be booleans when set",
        )
    return value


def _optional_query_value(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    text = values[0].strip()
    return text or None


def _optional_int_query(query: Mapping[str, list[str]], key: str) -> int | None:
    raw = _optional_query_value(query, key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as error:
        raise _BadRequestError(
            "invalid-request",
            f"query parameter {key!r} must be an integer: {error}",
            details={key: raw},
        ) from error


def _compute_etag(stat_result: os.stat_result) -> str:
    """Build a strong validator from inode-style fields.

    Mixes mtime_ns and size so any in-place edit invalidates the cache.
    The token is quoted per RFC 7232 so clients echo it verbatim in
    ``If-None-Match``.
    """
    return f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'


def _parse_etag_list(header_value: str) -> tuple[str, ...]:
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


# ---------------------------------------------------------------------------
# CLI entry points


def _install_shutdown_handlers(
    server: WorkbenchHTTPServer,
) -> Callable[[], None]:
    """Wire SIGINT / SIGTERM to ``server.shutdown()``.

    ``server.serve_forever()`` ordinarily breaks on ``KeyboardInterrupt``
    raised by SIGINT, but SIGTERM and tests that do not run on the main
    thread bypass that path. This helper schedules ``shutdown()`` from a
    daemon thread so the listening socket is released cleanly in both
    cases. The returned callable restores the previous signal handlers
    once the server has stopped.
    """

    def _trigger_shutdown(signum: int, _frame: Any) -> None:
        del signum

        def runner() -> None:
            # Shutdown must not crash here.
            with contextlib.suppress(Exception):
                server.shutdown()

        threading.Thread(target=runner, name="workbench-shutdown", daemon=True).start()

    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _trigger_shutdown)
        except ValueError, OSError:
            # signal() raises ValueError when called outside the main
            # thread (notably in test threads); skip silently — tests
            # call ``server.shutdown()`` directly.
            continue

    def restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except ValueError, OSError:
                continue

    return restore


def _default_open_in_browser(url: str) -> bool:
    """Attempt to open ``url`` in the operator's default browser."""
    try:
        return bool(webbrowser.open(url, new=1))
    except webbrowser.Error, OSError:
        return False


def workbench_serve_from_args(
    args: Any,
    *,
    open_browser: Callable[[str], bool] | None = None,
    serve_forever: Callable[[WorkbenchHTTPServer], None] | None = None,
    install_signal_handlers: Callable[[WorkbenchHTTPServer], Callable[[], None]]
    | None = None,
) -> int:
    """Run ``asl workbench serve`` from parsed CLI arguments.

    The keyword hooks let tests inject deterministic doubles for browser
    opening, the blocking ``serve_forever`` loop, and signal-handler
    installation without monkey-patching module-level state.
    """
    import sys

    open_browser_impl = open_browser or _default_open_in_browser
    serve_forever_impl = serve_forever or (lambda srv: srv.serve_forever())
    install_handlers = install_signal_handlers or _install_shutdown_handlers

    plan_roots = tuple(getattr(args, "plan_root", None) or ())
    try:
        server = create_server(
            host=getattr(args, "host", None) or DEFAULT_HOST,
            port=args.port,
            state_dir=args.state_dir,
            plan_roots=plan_roots,
        )
    except OSError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    COMMAND_NAME,
                    reason="bind-failed",
                    error=str(error),
                    paused=False,
                )
            )
        else:
            print(
                f"agent-session-loop workbench serve: {error}",
                file=sys.stderr,
            )
        return 1

    bound_host = server.bound_host
    bound_port = server.bound_port
    base_url = f"http://{bound_host}:{bound_port}"
    open_url = f"{base_url}/?token={server.token}"

    if args.json:
        responses.print_json(
            responses.success_payload(
                COMMAND_NAME,
                reason="serving",
                host=bound_host,
                port=bound_port,
                url=base_url,
                token=server.token,
            )
        )
    else:
        print(f"Workbench serving on {base_url}")
        print(f"Authorization token: {server.token}")

    if getattr(args, "open", False):
        open_browser_impl(open_url)

    restore_signals = install_handlers(server)
    try:
        serve_forever_impl(server)
    except KeyboardInterrupt:
        # Mirrors the historical contract: SIGINT during ``serve_forever``
        # is a clean exit, not an error.
        pass
    finally:
        try:
            restore_signals()
        finally:
            server.server_close()
    return 0
