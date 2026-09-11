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
GET  ``/api/archive``                    - archived entries, retention, auto-empty.
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

Idempotency
-----------
``/api/control/redirect`` requires a client-chosen ``operation_id`` and answers
a resent request from the redirect journal, so a retry whose response was lost
returns the original receipt instead of submitting a second correction.
``/api/launch`` and ``/api/reply`` accept an optional ``launch_id`` that plays
the same role for spawning a run, and refuse outright while a run already holds
the plan or a process this server started for it is still alive.
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

from . import (
    artifacts,
    control_cli,
    control_queue,
    harnesses,
    intervention,
    plans,
    qna,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store,
    responses,
    state,
    tail,
    workbench,
)
from . import (
    events as events_module,
)

TOKEN_BYTES = 32
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BODY = 1024 * 1024  # 1 MiB cap on request body.
AUTH_SCHEME = "Bearer "
COMMAND_NAME = "workbench"

DEFAULT_LOG_BYTES = 64 * 1024  # 64 KiB default tail/offset window.
MAX_LOG_BYTES = 1024 * 1024  # 1 MiB hard cap on a single log read.
DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 2000
DEFAULT_LAST_MESSAGE_BYTES = 64 * 1024
DEFAULT_FINAL_REVIEW_BYTES = 256 * 1024
_LOG_STREAMS = ("stdout", "stderr")

#: Bounded window on a redirect status read, matching the bridge's. The oldest
#: corrections on a long-lived plan are the settled ones, and an operator asking
#: where a correction got to is asking about a recent one.
MAX_REDIRECT_OPERATIONS = 64
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
        except _RedirectUnavailableError as error:
            # Retryable and unmistakably not a refusal: a journal this server
            # cannot read is a fact about the machine, and answering as though
            # the plan had no corrections would be the wrong kind of wrong.
            self._send_error(
                503,
                "redirect-unavailable",
                error.message,
                details=error.details,
                retryable=True,
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
        # Auto-empty expired archive entries on poll so retention takes effect
        # without a dedicated maintenance daemon; surface the counts/warnings.
        maintenance = workbench.maintain_archive(
            state_dir=config.state_dir,
            registry_path=config.registry_path,
        )
        discovery = workbench.discover_plans(
            state_root=config.state_dir,
            registry=registry,
            plan_roots=config.plan_roots,
            include_default_dirs=config.include_default_dirs,
        )
        summaries = workbench.build_workbench_summaries(
            discovery.plans, state_dir=config.state_dir
        )
        if sort_mode == "oldest":
            summaries = _sort_summaries_oldest_first(summaries)
        plan_payloads = [
            _augment_archivable(summary.to_payload(), summary) for summary in summaries
        ]
        self._send_success(
            reason="ok",
            plans=plan_payloads,
            registry_warnings=list(registry.warnings),
            discovery_warnings=list(discovery.warnings),
            state_root=str(state_root),
            state_root_exists=state_root.is_dir(),
            archive_retention_days=registry.archive_retention_days,
            archive_maintenance=maintenance.to_payload(),
        )

    def _handle_plan(self, *, query: Mapping[str, list[str]]) -> None:
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        try:
            registry = workbench.load_registry(config.registry_path)
        except workbench.WorkbenchError as error:
            self._send_error(500, "registry-unreadable", str(error))
            return
        try:
            canonical = plans.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            self._send_error(
                400,
                "invalid-plan",
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            )
            return
        pin = next((p for p in registry.pins if p.plan_dir == str(canonical)), None)
        discovered = workbench.DiscoveredPlan(
            plan_dir=canonical,
            sources=("registry",) if pin is not None else (),
            label=pin.label if pin is not None else None,
            profile=pin.profile if pin is not None else None,
            exists=canonical.is_dir(),
        )
        summary = workbench.build_workbench_summary(
            discovered, state_dir=config.state_dir
        )
        self._send_success(reason="ok", plan=summary.to_payload())

    def _handle_recovery(self, *, query: Mapping[str, list[str]]) -> None:
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        try:
            canonical = plans.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            self._send_error(
                400,
                "invalid-plan",
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            )
            return
        if not canonical.is_dir():
            self._send_error(
                400,
                "invalid-plan",
                f"plan directory does not exist: {canonical}",
                details={"plan_dir": str(canonical)},
            )
            return
        try:
            snapshot = plans.inspect_plan(canonical)
        except plans.PlanError as error:
            self._send_error(
                500,
                "malformed-plan",
                str(error),
                details={"plan_dir": str(canonical)},
            )
            return
        discovered = workbench.DiscoveredPlan(plan_dir=snapshot.plan_dir, exists=True)
        recovery = workbench.build_workbench_recovery(
            discovered, state_dir=config.state_dir
        )
        self._send_success(
            reason="ok",
            plan_dir=str(snapshot.plan_dir),
            recovery=recovery.to_payload(),
            artifacts=[artifact.to_payload() for artifact in recovery.artifacts],
            run_id=recovery.run_id,
            session_id=recovery.session_id,
            turn_index=recovery.turn_index,
            commit_index=recovery.commit_index,
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
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        try:
            canonical = plans.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            self._send_error(
                400,
                "invalid-plan",
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            )
            return
        discovered = workbench.DiscoveredPlan(
            plan_dir=canonical, exists=canonical.is_dir()
        )
        summary = workbench.build_workbench_summary(
            discovered, state_dir=config.state_dir
        )
        candidate_paths: dict[str, Path | None] = {
            "state_path": summary.state_path,
            "events_path": summary.events_path,
            "progress_path": summary.progress_path,
            "final_review_path": summary.final_review_path,
            "final_review_artifact_path": (
                canonical / workbench.FINAL_REVIEW_ARTIFACT_FILE_NAME
            ),
            "artifact_dir": summary.artifact_dir,
            "stdout_path": summary.stdout_path,
            "stderr_path": summary.stderr_path,
            "last_message_path": summary.last_message_path,
            "result_path": summary.result_path,
        }
        artifacts: _JSONDict = {}
        for name, path in candidate_paths.items():
            artifacts[name] = _artifact_descriptor(path)
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            artifacts=artifacts,
            run_id=summary.run_id,
            harness=summary.harness,
        )

    def _handle_state(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, plan_state = self._resolve_plan_state(query)
        state_path = plan_state / events_module.STATE_FILE_NAME
        try:
            snapshot = plans.inspect_plan(canonical)
        except plans.PlanError as error:
            self._send_error(
                500,
                "malformed-plan",
                str(error),
                details={"plan_dir": str(canonical)},
            )
            return
        lock_info = state._read_lock_path(
            plan_state / state.LOCK_FILE_NAME,
            snapshot.plan_dir,
        )
        payload = state._runner_state_payload(
            state_path,
            lock_info=lock_info,
        )
        try:
            workspace = workbench._workspace_projection(canonical, runtime=payload)
        except plans.PlanError, OSError, ValueError:
            workspace = {}
        next_pending = snapshot.next_pending
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            state_path=str(state_path),
            state=payload,
            sessions=[state._session_payload(session) for session in snapshot.sessions],
            counts=state._status_counts(snapshot),
            next_pending_id=next_pending.id if next_pending is not None else None,
            workspace=workspace,
            **workbench._workspace_payload(
                workspace,
                cwd=payload.get("cwd"),
            ),
        )

    def _handle_events(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, plan_state = self._resolve_plan_state(query)
        events_path = plan_state / events_module.EVENTS_FILE_NAME
        offset = self._require_non_negative_int(query, "offset", default=0)
        limit = self._require_bounded_int(
            query,
            "limit",
            default=DEFAULT_EVENT_LIMIT,
            maximum=MAX_EVENT_LIMIT,
        )
        filter_run = _optional_query_value(query, "run_id")
        try:
            records = tail._read_event_records(events_path)
        except tail.TailError as error:
            self._send_error(
                500,
                "os-error",
                str(error),
                details={"plan_dir": str(canonical)},
            )
            return
        state_path = plan_state / events_module.STATE_FILE_NAME
        try:
            state_payload = tail._read_json_file(state_path)
        except tail.TailError:
            state_payload = None
        run_id = (
            filter_run if filter_run else tail._latest_run_id(state_payload, records)
        )
        run_records = tail._event_records_for_run(records, run_id)
        total = len(run_records)
        sliced = run_records[offset : offset + limit]
        next_offset = offset + len(sliced) if offset + len(sliced) < total else None
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            events_path=str(events_path),
            run_id=run_id,
            total=total,
            offset=offset,
            limit=limit,
            next_offset=next_offset,
            events=sliced,
        )

    def _handle_logs(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, plan_state = self._resolve_plan_state(query)
        stream = _optional_query_value(query, "stream")
        explicit_path = _optional_query_value(query, "path")
        if explicit_path is not None and stream is not None:
            raise _BadRequestError(
                "invalid-request",
                "specify either 'stream' or 'path', not both",
            )
        if explicit_path is None and stream is None:
            raise _BadRequestError(
                "invalid-request",
                "missing required query parameter 'stream' or 'path'",
            )
        if stream is not None and stream not in _LOG_STREAMS:
            raise _BadRequestError(
                "invalid-request",
                f"stream must be one of {list(_LOG_STREAMS)}",
                details={"stream": stream},
            )

        config = self._server().config
        if explicit_path is None:
            summary = self._build_summary(canonical, config.state_dir)
            path = summary.stdout_path if stream == "stdout" else summary.stderr_path
            if path is None:
                self._send_error(
                    404,
                    "not-found",
                    f"no active {stream} log for plan",
                    details={"plan_dir": str(canonical)},
                )
                return
        else:
            path = self._validate_log_path(explicit_path, plan_state)

        tail_lines = _optional_int_query(query, "tail_lines")
        offset = _optional_int_query(query, "offset")
        if tail_lines is not None and offset is not None:
            raise _BadRequestError(
                "invalid-request",
                "specify either 'tail_lines' or 'offset', not both",
            )
        if tail_lines is not None and tail_lines < 0:
            raise _BadRequestError(
                "invalid-request",
                "tail_lines must be non-negative",
            )
        if offset is not None and offset < 0:
            raise _BadRequestError(
                "invalid-request",
                "offset must be non-negative",
            )
        max_bytes = self._require_bounded_int(
            query,
            "max_bytes",
            default=DEFAULT_LOG_BYTES,
            maximum=MAX_LOG_BYTES,
        )

        payload = _read_log_window(
            path,
            offset=offset,
            tail_lines=tail_lines,
            max_bytes=max_bytes,
        )
        if payload is None:
            self._send_error(
                404,
                "not-found",
                f"log file does not exist: {path}",
                details={"path": str(path)},
            )
            return
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            **payload,
        )

    def _handle_last_message(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, _plan_state = self._resolve_plan_state(query)
        config = self._server().config
        summary = self._build_summary(canonical, config.state_dir)
        path = summary.last_message_path
        if path is None:
            self._send_error(
                404,
                "not-found",
                "no active last-message file for plan",
                details={"plan_dir": str(canonical)},
            )
            return
        max_bytes = self._require_bounded_int(
            query,
            "max_bytes",
            default=DEFAULT_LAST_MESSAGE_BYTES,
            maximum=MAX_LOG_BYTES,
        )
        payload = _read_bounded_text(path, max_bytes=max_bytes)
        if payload is None:
            self._send_error(
                404,
                "not-found",
                f"last-message file does not exist: {path}",
                details={"path": str(path)},
            )
            return
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            **payload,
        )

    def _handle_result(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, _plan_state = self._resolve_plan_state(query)
        config = self._server().config
        summary = self._build_summary(canonical, config.state_dir)
        path = summary.result_path
        if path is None:
            self._send_error(
                404,
                "not-found",
                "no active result.json for plan",
                details={"plan_dir": str(canonical)},
            )
            return
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._send_error(
                404,
                "not-found",
                f"result file does not exist: {path}",
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
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError as error:
            self._send_error(
                500,
                "os-error",
                f"result file is not valid JSON: {error}",
                details={"path": str(path)},
            )
            return
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            path=str(path),
            result=parsed,
        )

    def _handle_verification(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, plan_state = self._resolve_plan_state(query)
        state_path = plan_state / events_module.STATE_FILE_NAME
        lock_info = state._read_lock_path(
            plan_state / state.LOCK_FILE_NAME,
            canonical,
        )
        runner_state = state._runner_state_payload(
            state_path,
            lock_info=lock_info,
        )
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            state_path=str(state_path),
            active_verification=runner_state.get("active_verification"),
            run_id=runner_state.get("run_id"),
            harness=runner_state.get("harness"),
        )

    def _handle_commits(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, plan_state = self._resolve_plan_state(query)
        events_path = plan_state / events_module.EVENTS_FILE_NAME
        filter_run = _optional_query_value(query, "run_id")
        try:
            records = tail._read_event_records(events_path)
        except tail.TailError as error:
            self._send_error(
                500,
                "os-error",
                str(error),
                details={"plan_dir": str(canonical)},
            )
            return
        state_path = plan_state / events_module.STATE_FILE_NAME
        try:
            state_payload = tail._read_json_file(state_path)
        except tail.TailError:
            state_payload = None
        run_id = (
            filter_run if filter_run else tail._latest_run_id(state_payload, records)
        )
        run_records = tail._event_records_for_run(records, run_id)
        commits = _build_commit_rows(run_records)
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            events_path=str(events_path),
            run_id=run_id,
            commits=commits,
        )

    def _handle_progress(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, _plan_state = self._resolve_plan_state(query)
        path = plans.progress_path(canonical)
        try:
            entries = plans.read_progress_entries(canonical)
        except plans.PlanError as error:
            self._send_error(
                500,
                "malformed-plan",
                str(error),
                details={"plan_dir": str(canonical), "path": str(path)},
            )
            return
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            path=str(path),
            exists=path.is_file(),
            entries=[_progress_entry_payload(entry) for entry in entries],
        )

    def _handle_final_review(self, *, query: Mapping[str, list[str]]) -> None:
        canonical, _plan_state = self._resolve_plan_state(query)
        config = self._server().config
        summary = self._build_summary(canonical, config.state_dir)
        path = summary.final_review_path
        if path is None:
            path = canonical / workbench.FINAL_REVIEW_FILE_NAME
        max_bytes = self._require_bounded_int(
            query,
            "max_bytes",
            default=DEFAULT_FINAL_REVIEW_BYTES,
            maximum=MAX_LOG_BYTES,
        )
        payload = _read_bounded_text(path, max_bytes=max_bytes)
        if payload is None:
            self._send_error(
                404,
                "not-found",
                f"final-review file does not exist: {path}",
                details={"path": str(path)},
            )
            return
        artifact_path = canonical / workbench.FINAL_REVIEW_ARTIFACT_FILE_NAME
        structured_review = None
        structured_error = None
        try:
            if artifact_path.is_file() and artifact_path.stat().st_size <= max_bytes:
                decoded = json.loads(artifact_path.read_text(encoding="utf-8"))
                if isinstance(decoded, dict):
                    structured_review = decoded
                else:
                    structured_error = "final-review.json did not contain an object"
        except (OSError, json.JSONDecodeError) as error:
            structured_error = str(error)
        try:
            workspace = workbench._workspace_projection(canonical)
        except plans.PlanError, OSError, ValueError:
            workspace = {}
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            artifact_path=str(artifact_path),
            structured_review=structured_review,
            structured_error=structured_error,
            review_resume=summary.review_resume,
            workspace=workspace,
            **workbench._workspace_payload(workspace, cwd=summary.cwd),
            **payload,
        )

    def _handle_questions(self, *, query: Mapping[str, list[str]]) -> None:
        plan_dir = self._require_query_value(query, "plan_dir")
        config = self._server().config
        try:
            context = qna.load_qna_context(plan_dir, state_dir=config.state_dir)
        except qna.QnAError as error:
            status = 404 if error.reason == qna.REASON_NO_PAUSED_RUN else 400
            self._send_error(
                status,
                error.reason,
                error.error,
                details={"plan_dir": plan_dir, **error.details},
            )
            return
        except plans.PlanError as error:
            self._send_error(
                400,
                "malformed-plan",
                str(error),
                details={"plan_dir": plan_dir},
            )
            return
        payload = qna.questions_payload(context)
        # ``questions_payload`` already wraps the response envelope under the
        # ``questions`` command name. Re-emit under the workbench command name
        # so the frontend sees a consistent ``command="workbench"`` envelope
        # while keeping every field the resolver prompt UI needs.
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"ok", "command", "reason", "paused"}
        }
        self._send_success(reason="ok", **fields)

    # -- Mutating endpoints -------------------------------------------------

    def _handle_registry_pin(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        label = _optional_string(body, "label")
        profile = _coerce_profile_payload(body.get("profile"))
        config = self._server().config
        try:
            updated, written_path = workbench.mutate_registry(
                config.registry_path,
                lambda current: workbench.upsert_pin(
                    current, plan_dir, label=label, profile=profile
                ),
            )
        except workbench.WorkbenchError as error:
            self._send_error(
                400, "invalid-plan", str(error), details={"plan_dir": plan_dir}
            )
            return
        self._send_success(
            reason="ok",
            registry_path=str(written_path),
            registry=updated.to_payload(),
        )

    def _handle_registry_unpin(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        config = self._server().config
        try:
            updated, written_path = workbench.mutate_registry(
                config.registry_path,
                lambda current: workbench.remove_pin(current, plan_dir),
            )
        except workbench.WorkbenchError as error:
            self._send_error(
                400, "invalid-plan", str(error), details={"plan_dir": plan_dir}
            )
            return
        self._send_success(
            reason="ok",
            registry_path=str(written_path),
            registry=updated.to_payload(),
        )

    def _handle_plan_archive(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        confirm_plan_dir = _require_string(body, "confirm_plan_dir")
        try:
            canonical = plans.canonicalize_path(plan_dir)
            confirmed = plans.canonicalize_path(confirm_plan_dir)
        except (OSError, ValueError) as error:
            self._send_error(
                400,
                "invalid-plan",
                f"invalid plan_dir: {error}",
                details={
                    "plan_dir": plan_dir,
                    "confirm_plan_dir": confirm_plan_dir,
                },
            )
            return
        if canonical != confirmed:
            self._send_error(
                400,
                "confirmation-mismatch",
                "confirm_plan_dir must match plan_dir after canonicalization",
                details={
                    "plan_dir": str(canonical),
                    "confirm_plan_dir": str(confirmed),
                },
            )
            return
        config = self._server().config
        try:
            result = workbench.archive_plan(
                canonical,
                state_dir=config.state_dir,
                registry_path=config.registry_path,
                plan_roots=config.plan_roots,
                include_default_dirs=config.include_default_dirs,
            )
        except workbench.ArchiveError as error:
            self._send_error(
                error.status,
                error.reason,
                error.message,
                details={"plan_dir": str(canonical)},
            )
            return
        self._send_success(reason="ok", **result.to_payload())

    def _handle_plans_archive(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dirs = _require_string_list(body, "plan_dirs")
        config = self._server().config
        results: list[_JSONDict] = []
        archived_count = 0
        failed_count = 0
        for raw_plan_dir in plan_dirs:
            try:
                canonical = plans.canonicalize_path(raw_plan_dir)
            except (OSError, ValueError) as error:
                results.append(
                    {
                        "plan_dir": raw_plan_dir,
                        "archived": False,
                        "reason": "invalid-plan",
                        "error": f"invalid plan_dir: {error}",
                    }
                )
                failed_count += 1
                continue
            try:
                result = workbench.archive_plan(
                    canonical,
                    state_dir=config.state_dir,
                    registry_path=config.registry_path,
                    plan_roots=config.plan_roots,
                    include_default_dirs=config.include_default_dirs,
                )
            except workbench.ArchiveError as error:
                results.append(
                    {
                        "plan_dir": str(canonical),
                        "archived": False,
                        "reason": error.reason,
                        "error": error.message,
                    }
                )
                failed_count += 1
                continue
            results.append(
                {
                    "plan_dir": str(canonical),
                    "archived": True,
                    "reason": "ok",
                    "error": None,
                    "result": result.to_payload(),
                }
            )
            archived_count += 1
        self._send_success(
            reason="ok",
            results=results,
            archived_count=archived_count,
            failed_count=failed_count,
        )

    def _handle_archive_list(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        config = self._server().config
        try:
            registry = workbench.load_registry(config.registry_path)
        except workbench.WorkbenchError as error:
            self._send_error(500, "registry-unreadable", str(error))
            return
        maintenance = workbench.maintain_archive(
            state_dir=config.state_dir,
            registry_path=config.registry_path,
        )
        listing = workbench.list_archive_entries(config.state_dir)
        self._send_success(
            reason="ok",
            entries=[entry.to_payload() for entry in listing.entries],
            warnings=list(listing.warnings),
            archive_retention_days=registry.archive_retention_days,
            archive_maintenance=maintenance.to_payload(),
        )

    def _handle_archive_restore(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        archive_id = _require_string(body, "archive_id")
        config = self._server().config
        try:
            result = workbench.restore_archive(
                archive_id,
                state_dir=config.state_dir,
                registry_path=config.registry_path,
            )
        except workbench.ArchiveError as error:
            self._send_error(
                error.status,
                error.reason,
                error.message,
                details={"archive_id": archive_id},
            )
            return
        self._send_success(reason="ok", **result.to_payload())

    def _handle_archive_delete(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        archive_ids = _require_string_list(body, "archive_ids")
        confirm_count = _require_int(body, "confirm_count")
        if confirm_count != len(archive_ids):
            self._send_error(
                400,
                "confirmation-mismatch",
                "confirm_count must match the number of archive_ids",
                details={
                    "confirm_count": confirm_count,
                    "archive_ids": len(archive_ids),
                },
            )
            return
        config = self._server().config
        result = workbench.purge_archive_entries(
            archive_ids, state_dir=config.state_dir
        )
        self._send_success(reason="ok", **result.to_payload())

    def _handle_archive_settings(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        retention_days = _require_int(body, "archive_retention_days")
        if retention_days < 1:
            self._send_error(
                400,
                "invalid-request",
                "archive_retention_days must be a positive integer",
                details={"archive_retention_days": retention_days},
            )
            return
        config = self._server().config
        try:
            updated, written_path = workbench.mutate_registry(
                config.registry_path,
                lambda current: workbench.set_archive_retention_days(
                    current, retention_days
                ),
            )
        except workbench.WorkbenchError as error:
            self._send_error(400, "invalid-request", str(error))
            return
        self._send_success(
            reason="ok",
            registry_path=str(written_path),
            registry=updated.to_payload(),
            archive_retention_days=updated.archive_retention_days,
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
        impatient second click, resending a launch that already succeeded. Three
        things stand between that and a second process, in this order:

        1. ``launch_id`` replay. A resent request carrying the id of one that
           already ran gets that run's own answer back, so a retry is the same
           request rather than a second one.
        2. The ownership pre-check below, which reports a live run as a conflict
           instead of spawning a process that dies on the plan lock.
        3. The per-plan guard, which makes 1 and 2 mean anything on a threaded
           server by serializing check-and-spawn for one plan.

        A client that supplies no ``launch_id`` still gets 2 and 3, which is the
        most this surface can do for a request it cannot recognize as a retry.
        """
        plan_dir = _require_string(body, "plan_dir")
        launch_id = _optional_launch_id(body)
        profile = _coerce_profile_payload(body.get("profile"))
        config = self._server().config
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
            if launch_id is not None:
                recorded = self._server().recorded_launch(canonical, launch_id)
                if recorded is not None:
                    self._send_success(
                        reason="ok",
                        launch=recorded,
                        launch_id=launch_id,
                        replayed=True,
                    )
                    return
            refusal = self._launch_conflict(canonical, config)
            if refusal is not None:
                reason, message = refusal
                self._send_error(
                    409,
                    reason,
                    message,
                    details={"plan_dir": str(canonical)},
                )
                return
            try:
                if reply_text is None:
                    result = workbench.launch_plan(
                        plan_dir,
                        profile=profile,
                        state_dir=config.state_dir,
                    )
                else:
                    result = workbench.resume_plan_with_reply(
                        plan_dir,
                        reply_text=reply_text,
                        profile=profile,
                        state_dir=config.state_dir,
                    )
            except workbench.LaunchError as error:
                self._send_error(
                    400,
                    error.reason,
                    error.message,
                    details={"plan_dir": plan_dir},
                )
                return
            payload = result.to_payload()
            self._server().record_launch_pid(canonical, result.pid)
            if launch_id is not None:
                self._server().record_launch(canonical, launch_id, payload)
        self._send_success(
            reason="ok",
            launch=payload,
            launch_id=launch_id,
            replayed=False,
        )

    def _launch_conflict(
        self, plan_dir: Path, config: ServerConfig
    ) -> tuple[str, str] | None:
        """Name what already owns this plan, or ``None`` when nothing does.

        Two checks, because neither sees the other's window. Once a child has
        taken the plan lock, the shared service reports a live run. Before it
        has -- the seconds a reconnecting browser is most likely to resend in --
        nothing durable says so yet, and the pid this server spawned is the only
        evidence a launch is already under way.

        A paused run is deliberately not a conflict: resuming one is exactly
        what ``/api/reply`` is for.
        """
        try:
            facts = intervention.inspect_run(plan_dir, state_dir=config.state_dir)
        except intervention.InterventionError:
            # An unreadable plan is the launch path's refusal to make, not this
            # check's: reporting it here would mask `LaunchError`'s own reason.
            return None
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

    def _handle_control_stop_after_current(
        self, *, query: Mapping[str, list[str]]
    ) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        command_id = _optional_string(body, "command_id")
        config = self._server().config
        try:
            # The shared service carries the same best-effort wakeup the CLI
            # producer sends, so the HTTP surface is not the slower way to
            # reach a live runner.
            dispatch = intervention.dispatch_control_command(
                plan_dir,
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                state_dir=config.state_dir,
                command_id=command_id,
            )
        except control_queue.ControlQueueError as error:
            self._send_error(400, "invalid-command", str(error))
            return
        self._send_json(
            202,
            responses.success_payload(
                COMMAND_NAME,
                reason="accepted",
                accepted=True,
                queued_command=dispatch.command.to_payload(),
                signal=dispatch.signal.to_payload(),
            ),
        )

    def _handle_control_abort(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        command_id = _optional_string(body, "command_id")
        # Passed through untouched: ``append_command`` is the only validator of
        # grace and reason, so the HTTP surface cannot accept a window the CLI
        # would reject or reject one it would accept.
        raw_grace: Any = body.get("grace_seconds")
        raw_reason: Any = body.get("reason")
        config = self._server().config
        run_active, turn_active = self._abort_run_activity(plan_dir, config)
        try:
            dispatch = intervention.dispatch_control_command(
                plan_dir,
                control_queue.COMMAND_REQUEST_ABORT_TURN,
                state_dir=config.state_dir,
                grace_seconds=raw_grace,
                reason=raw_reason,
                command_id=command_id,
            )
        except control_queue.ControlQueueError as error:
            code = str(error)
            if code == control_queue.REJECT_INVALID_GRACE:
                message = (
                    "grace_seconds must be a whole number of seconds between "
                    f"{control_queue.MIN_ABORT_GRACE_SECONDS} and "
                    f"{control_queue.MAX_ABORT_GRACE_SECONDS}"
                )
            elif code == control_queue.REJECT_INVALID_REASON:
                message = "reason must be non-empty text when provided"
            else:
                code = "invalid-command"
                message = str(error)
            self._send_error(
                400,
                code,
                message,
                details={
                    "grace_seconds": repr(raw_grace),
                    "reason": repr(raw_reason),
                },
            )
            return
        queued = dispatch.command
        notified = dispatch.signal
        if not run_active:
            note = control_cli.NO_ACTIVE_RUN_NOTE
        elif turn_active:
            note = control_cli.ABORT_WILL_APPLY_NOTE
        else:
            note = control_cli.ABORT_DEGRADES_NOTE
        self._send_json(
            202,
            responses.success_payload(
                COMMAND_NAME,
                reason="accepted",
                accepted=True,
                run_active=run_active,
                turn_active=turn_active,
                grace_seconds=queued.grace_seconds,
                # The CLI calls this field `reason`; here the top-level
                # `reason` is the HTTP response status, so the abort's own
                # reason keeps a distinct name.
                abort_reason=queued.reason,
                note=note,
                queued_command=queued.to_payload(),
                signal=notified.to_payload(),
            ),
        )

    def _abort_run_activity(
        self, plan_dir: str, config: ServerConfig
    ) -> tuple[bool, bool]:
        """Whether a runner holds the plan and whether a turn is in flight."""
        try:
            # The lock records the canonical plan directory, so comparing
            # against the raw request path would call every lock stale on a
            # host where the plan sits behind a symlink.
            canonical = plans.canonicalize_path(plan_dir)
            plan_state = state.plan_state_dir(canonical, config.state_dir)
        except OSError, ValueError:
            return False, False
        lock_info = state._read_lock_path(plan_state / state.LOCK_FILE_NAME, canonical)
        run_active = lock_info is not None and lock_info.active
        if not run_active:
            return False, False
        runtime = state._runner_state_payload(
            plan_state / events_module.STATE_FILE_NAME, lock_info=lock_info
        )
        return True, runtime.get("active_turn") is not None

    def _handle_control_select_harness(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        harness = _require_string(body, "harness")
        command_id = _optional_string(body, "command_id")
        if harness not in harnesses.HARNESS_NAMES:
            self._send_error(
                400,
                "invalid-harness",
                f"unsupported harness {harness!r}",
                details={"harness": harness},
            )
            return
        config = self._server().config
        try:
            dispatch = intervention.dispatch_control_command(
                plan_dir,
                control_queue.COMMAND_SELECT_FUTURE_HARNESS,
                state_dir=config.state_dir,
                harness=harness,
                command_id=command_id,
            )
        except control_queue.ControlQueueError as error:
            self._send_error(400, "invalid-command", str(error))
            return
        self._send_json(
            202,
            responses.success_payload(
                COMMAND_NAME,
                reason="accepted",
                accepted=True,
                queued_command=dispatch.command.to_payload(),
                signal=dispatch.signal.to_payload(),
            ),
        )

    # -- Redirect -----------------------------------------------------------

    def _redirect_state_dir(self) -> str | None:
        state_dir = self._server().config.state_dir
        return str(state_dir) if state_dir is not None else None

    def _run_facts(self, plan_dir: str) -> intervention.RunFacts:
        """Read the shared intervention facts, or refuse the request."""
        try:
            return intervention.inspect_run(
                plan_dir, state_dir=self._server().config.state_dir
            )
        except intervention.InterventionError as error:
            raise _BadRequestError(
                error.reason, error.message, details=dict(error.details)
            ) from error

    def _redirect_report(self, plan_dir: Path) -> redirect_recovery.RecoveryReport:
        """Read this plan's durable redirect state, or refuse retryably.

        The read every redirect handler starts from, including the mutating one.
        It is what makes this server's answer survive its own restart: the
        journal, not anything held in this process, is where a correction's fate
        lives, so a browser that reconnects to a *restarted* server still learns
        what became of what it submitted.
        """
        try:
            return redirect_recovery.inspect_plan_recovery(
                plan_dir, state_dir=self._server().config.state_dir
            )
        except redirect_store.RedirectStoreError as error:
            raise _RedirectUnavailableError(
                f"redirect journal could not be read: {error.message}",
                details={"reason": error.reason},
            ) from error
        except (OSError, ValueError) as error:
            raise _RedirectUnavailableError(
                f"redirect journal could not be read: {error}"
            ) from error

    def _live_redirect_target(
        self, plan_dir: Path
    ) -> redirect_store.RedirectTarget | None:
        """Read the turn this run is on now, or ``None`` when it is not on one.

        Reported even where a redirect would be refused: a terminal audit or a
        target that already moved still tells a reader which turn an earlier
        correction is being measured against.
        """
        try:
            facts = redirect_admission.inspect_target(
                plan_dir, state_dir=self._server().config.state_dir
            )
        except redirect_admission.RedirectAdmissionError, OSError:
            return None
        return facts.target

    def _redirect_receipt(
        self,
        *,
        facts: intervention.RunFacts,
        operation_id: str | None,
        outcome: str | None,
        reason: str | None,
        target: Mapping[str, Any] | None,
        waited_seconds: float,
        replayed: bool,
    ) -> _JSONDict:
        """Answer a redirect request from the journal rather than from memory.

        ``operation`` is re-read after the submission instead of being built from
        the ``SubmissionOutcome`` in hand, so the status a browser is told and
        the status ``asl control redirect-status`` computes next are the same
        projection of the same bytes.
        """
        report = self._redirect_report(facts.plan_dir)
        live_target = self._live_redirect_target(facts.plan_dir)
        operation = next(
            (
                redirect_cli.operation_payload(item, live_target=live_target)
                for item in report.operations
                if item.operation_id == operation_id
            ),
            None,
        )
        return {
            # Null exactly when nothing was persisted. Echoing the client's own
            # id for a refusal that never reached the journal would invite it to
            # ask after a correction that does not exist.
            "operation_id": operation_id if operation is not None else None,
            "plan_dir": str(facts.plan_dir),
            "run_id": facts.run_id,
            # Null on a replay: this call submitted nothing, so it has no
            # submission outcome to report and `operation` carries the truth.
            "outcome": outcome,
            "refusal": reason,
            "target": dict(target) if target is not None else None,
            "waited_seconds": round(waited_seconds, 3),
            "replayed": replayed,
            "operation": operation,
            "journal_complete": report.complete,
        }

    def _handle_control_redirect(self, *, query: Mapping[str, list[str]]) -> None:
        """Submit one urgent correction against the turn this run is on now.

        Replay is served out of the redirect journal before anything else is
        checked, and that ordering is the whole reason a browser may retry this
        request. A correction that landed moves the plan, which moves the action
        token, which would make the retry of a request whose response was lost
        look like a stale second correction. Asking the journal first makes the
        ``operation_id`` the client already holds the answer.
        """
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        operation_id = _require_string(body, "operation_id")
        facts = self._run_facts(plan_dir)
        replay = self._redirect_receipt(
            facts=facts,
            operation_id=operation_id,
            outcome=None,
            reason=None,
            target=None,
            waited_seconds=0.0,
            replayed=True,
        )
        if replay["operation"] is not None:
            # 200 rather than 202: this call accepted nothing, it recognized a
            # request that was already accepted once.
            self._send_success(reason="ok", redirect=replay)
            return

        token = _require_string(body, "action_token")
        try:
            intervention.validate_action(
                facts,
                intervention.ACTION_REDIRECT,
                token=token,
                run_id=_optional_string(body, "run_id"),
            )
        except intervention.InterventionError as error:
            self._send_error(
                _intervention_status(error.reason),
                error.reason,
                error.message,
                details={"plan_dir": str(facts.plan_dir), **error.details},
                retryable=error.retryable,
            )
            return

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_error(
                400,
                intervention.REASON_EMPTY_TEXT,
                "redirect text must contain non-whitespace content",
                details={"plan_dir": str(facts.plan_dir)},
            )
            return
        expected_target = _expected_redirect_target(body)
        wait_seconds = _redirect_wait_seconds(body)
        try:
            outcome = redirect_admission.submit_redirect(
                facts.plan_dir,
                text,
                state_dir=self._server().config.state_dir,
                operation_id=operation_id,
                expected_target=expected_target,
                wait_seconds=wait_seconds,
            )
        except redirect_admission.RedirectAdmissionError as error:
            self._send_error(
                400,
                error.reason,
                f"redirect could not be submitted: {error.message}",
                details={"plan_dir": str(facts.plan_dir), **error.details},
            )
            return
        except redirect_store.RedirectStoreError as error:
            # Split on the journal's own reason rather than on the exception
            # type, because one exception covers both "this body is not a
            # correction" and "this journal cannot be written". Calling an
            # oversized correction a retryable server fault would send a client
            # back to resend the one thing that cannot succeed.
            request_shaped = error.reason in _REDIRECT_REQUEST_REASONS
            self._send_error(
                400 if request_shaped else 500,
                error.reason,
                (
                    f"redirect could not be submitted: {error.message}"
                    if request_shaped
                    else f"redirect could not be persisted: {error.message}"
                ),
                details={"plan_dir": str(facts.plan_dir), **error.details},
                retryable=not request_shaped,
            )
            return
        except OSError as error:
            self._send_error(
                500,
                "os-error",
                f"redirect could not be persisted: {error}",
                details={"plan_dir": str(facts.plan_dir)},
                retryable=True,
            )
            return

        receipt = self._redirect_receipt(
            facts=facts,
            operation_id=outcome.operation_id,
            outcome=outcome.outcome,
            reason=outcome.reason,
            target=outcome.target.to_payload() if outcome.target is not None else None,
            waited_seconds=outcome.waited_seconds,
            replayed=False,
        )
        if not outcome.persisted:
            # Nothing reached the journal, so there is nothing to ask after
            # later. Reporting this as accepted is the one falsehood the whole
            # redirect path is built to avoid.
            self._send_error(
                409,
                outcome.reason or redirect_admission.REFUSE_NO_ACTIVE_TURN,
                f"redirect was refused: {outcome.reason}",
                details={"plan_dir": str(facts.plan_dir), "redirect": receipt},
                retryable=(outcome.reason in redirect_admission.UNAVAILABLE_REASONS),
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
                accepted=outcome.accepted,
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
        plan_dir = self._require_query_value(query, "plan_dir")
        facts = self._run_facts(plan_dir)
        wanted = frozenset(
            value for value in query.get("operation_id", ()) if value.strip()
        )
        report = self._redirect_report(facts.plan_dir)
        live_target = self._live_redirect_target(facts.plan_dir)
        selected = [
            operation
            for operation in report.operations
            if not wanted or operation.operation_id in wanted
        ]
        operations = [
            redirect_cli.operation_payload(operation, live_target=live_target)
            # Truncated from the front: the oldest corrections on a long-lived
            # plan are the settled ones.
            for operation in selected[-MAX_REDIRECT_OPERATIONS:]
        ]
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
            truncated=len(selected) > len(operations),
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
        plan_dir = self._require_query_value(query, "plan_dir")
        facts = self._run_facts(plan_dir)
        pause_reason = facts.runner_state.get("reason")
        classified = intervention.recovery_decision(
            pause_reason if isinstance(pause_reason, str) else None,
            paused=facts.paused,
        )
        commands = redirect_cli.recovery_commands(
            facts.plan_dir,
            self._redirect_state_dir(),
            decision=classified.decision,
        )
        authorization = commands["authorization_command"]
        report = self._redirect_report(facts.plan_dir)
        live_target = self._live_redirect_target(facts.plan_dir)
        self._send_success(
            reason="ok",
            plan_dir=str(facts.plan_dir),
            run_id=facts.run_id,
            recovery_required=classified.required,
            # Reported whether or not it maps to a decision: "paused, and no
            # manual recovery is what it is waiting on" is an answer, while an
            # unexplained null is a second question.
            pause_reason=classified.pause_reason,
            decision=classified.decision,
            auto_resume=classified.auto_resume,
            authorized_by=classified.authorized_by,
            inspect_command=commands["inspect_command"],
            authorization_command=authorization,
            # Both describe the authorization command, so both are false and
            # null where there is none. `predecessor-not-stopped` has no
            # authorization at all, and telling an operator its absent
            # authorization is one-use would read as an invitation to find it.
            one_use=authorization is not None,
            note=(
                redirect_cli.FRESH_CONVERSATION_NOTE
                if authorization is not None
                else None
            ),
            conversations=report.conversations.to_payload(),
            # The corrections a fresh start would still carry. Reported here so
            # an operator authorizing one sees what it inherits rather than
            # discovering it in the successor's first prompt.
            retained_operations=[
                redirect_cli.operation_payload(operation, live_target=live_target)
                for operation in (*report.unfinished, *report.uncertain)[
                    -MAX_REDIRECT_OPERATIONS:
                ]
            ],
        )

    def _handle_unlock(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        force = bool(body.get("force", False))
        config = self._server().config
        try:
            result = state.unlock_plan(plan_dir, config.state_dir, force=force)
        except plans.PlanError as error:
            self._send_error(
                400,
                "malformed-plan",
                str(error),
                details={"plan_dir": plan_dir},
            )
            return
        except state.StateError as error:
            if "active lock" in str(error).lower():
                self._send_error(
                    409,
                    "active-lock",
                    str(error),
                    details={"plan_dir": plan_dir},
                )
            else:
                self._send_error(
                    400,
                    "state-error",
                    str(error),
                    details={"plan_dir": plan_dir},
                )
            return
        except OSError as error:
            self._send_error(
                500,
                "os-error",
                str(error),
                details={"plan_dir": plan_dir},
            )
            return
        self._send_success(
            reason="ok",
            plan_dir=str(plans.canonicalize_path(plan_dir)),
            lock_path=str(result.path),
            had_lock=result.had_lock,
            removed=result.removed,
            active=result.was_active,
            stale=result.was_stale,
            message=result.message,
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

    def _resolve_plan_state(self, query: Mapping[str, list[str]]) -> tuple[Path, Path]:
        plan_dir = self._require_query_value(query, "plan_dir")
        try:
            canonical = plans.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            raise _BadRequestError(
                "invalid-plan",
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            ) from error
        config = self._server().config
        plan_state = state.plan_state_dir(canonical, config.state_dir)
        return canonical, plan_state

    def _build_summary(
        self, canonical: Path, state_dir: Path | None
    ) -> workbench.WorkbenchSummary:
        discovered = workbench.DiscoveredPlan(
            plan_dir=canonical, exists=canonical.is_dir()
        )
        return workbench.build_workbench_summary(discovered, state_dir=state_dir)

    def _validate_log_path(self, candidate: str, plan_state: Path) -> Path:
        try:
            resolved = plans.canonicalize_path(candidate)
        except (OSError, ValueError) as error:
            raise _BadRequestError(
                "invalid-request",
                f"invalid path: {error}",
                details={"path": candidate},
            ) from error
        try:
            resolved.relative_to(plan_state)
        except ValueError as error:
            raise _BadRequestError(
                "invalid-request",
                "path must be inside the plan state directory",
                details={
                    "path": str(resolved),
                    "plan_state_dir": str(plan_state),
                },
            ) from error
        return resolved


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


def _require_int(body: Mapping[str, Any], key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadRequestError(
            "invalid-request",
            f"missing required integer field {key!r}",
        )
    return value


def _require_string_list(body: Mapping[str, Any], key: str) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list):
        raise _BadRequestError(
            "invalid-request",
            f"missing required list field {key!r}",
        )
    if not value:
        raise _BadRequestError(
            "invalid-request",
            f"field {key!r} must not be empty",
        )
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise _BadRequestError(
                "invalid-request",
                f"field {key!r} must contain non-empty strings",
            )
        items.append(entry)
    return items


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


class _RedirectUnavailableError(Exception):
    """The redirect journal could not be read, so no answer would be truthful."""

    def __init__(self, message: str, *, details: _JSONDict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


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


#: Journal refusals that describe the submitted correction rather than the
#: journal. Enumerated by reason because one exception type carries both, and
#: the two answers a client needs are opposite: rewrite the body, or retry.
#: The caps behind them compose rather than compete -- this server's 1 MiB
#: request cap bounds what it will read, and the journal's own field caps bound
#: what it will carry, which are the smaller of the two.
_REDIRECT_REQUEST_REASONS = frozenset(
    {
        redirect_store.ERROR_INVALID_TARGET,
        redirect_store.ERROR_INVALID_FEEDBACK,
        redirect_store.ERROR_INVALID_TTL,
        redirect_store.ERROR_INVALID_OPERATION_ID,
        redirect_store.ERROR_INVALID_REASON,
    }
)


def _expected_redirect_target(
    body: Mapping[str, Any],
) -> redirect_store.RedirectTarget | None:
    """Build the turn a client believes it is correcting, if it named one.

    Optional, and load-bearing when supplied: a browser that rendered a turn and
    then waited for a person to finish typing is exactly the client whose turn
    may have ended in between, and naming the turn is what turns that race into a
    ``stale-target`` refusal instead of a correction delivered to whatever the
    runner moved on to.
    """
    raw = body.get("expected_target")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _BadRequestError(
            "invalid-request", "field 'expected_target' must be an object"
        )
    turn_index = raw.get("turn_index")
    if isinstance(turn_index, bool) or not isinstance(turn_index, int):
        raise _BadRequestError(
            "invalid-request",
            "field 'expected_target.turn_index' must be an integer",
        )
    target = redirect_store.RedirectTarget(
        run_id=_require_string(raw, "run_id"),
        session_id=_require_string(raw, "session_id"),
        turn_index=turn_index,
        harness=_optional_string(raw, "harness"),
    )
    try:
        redirect_store.validate_target(target)
    except redirect_store.RedirectStoreError as error:
        raise _BadRequestError(
            "invalid-request",
            f"field 'expected_target' is not a valid turn: {error.message}",
            details={"reason": error.reason},
        ) from error
    return target


def _redirect_wait_seconds(body: Mapping[str, Any]) -> float:
    """Bound how long one redirect request may hold a server thread.

    Admission's own ceiling is reused rather than restated: it is the same
    promise measured from the other end.
    """
    raw = body.get("wait_seconds")
    if raw is None:
        return redirect_admission.ACCEPTANCE_WAIT_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise _BadRequestError(
            "invalid-request", "field 'wait_seconds' must be a number"
        )
    if raw < 0 or raw > redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS:
        raise _BadRequestError(
            "invalid-request",
            "field 'wait_seconds' is outside the permitted range",
            details={"maximum": redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS},
        )
    return float(raw)


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


def _is_gzip_artifact(path: Path) -> bool:
    """Return True when ``path`` names a gzip artifact written by this project.

    Gzip verification artifacts always use the ``.gz`` suffix
    (``{check}-stdout.gz`` etc.), so suffix detection is deterministic and lets
    the bounded-window log APIs serve plain and gzip logs uniformly.
    """
    return path.suffix == ".gz"


def _read_gzip_log_window(
    path: Path,
    *,
    offset: int | None,
    tail_lines: int | None,
    max_bytes: int,
) -> _JSONDict | None:
    """Bounded-window read over a gzip artifact.

    Gzip cannot be seeked, so the artifact is decompressed fully and the same
    offset/tail/max_bytes windowing is applied to the decompressed bytes. The
    payload shape matches :func:`_read_log_window` so callers are agnostic to
    the on-disk encoding.
    """
    try:
        data = artifacts.read_gzip(path)
    except artifacts.ArtifactError as error:
        if not path.exists():
            return None
        return {
            "path": str(path),
            "exists": True,
            "error": str(error),
            "size": None,
            "offset": offset or 0,
            "next_offset": None,
            "truncated_head": False,
            "truncated_tail": False,
            "text": "",
        }
    size = len(data)
    read_offset = offset if offset is not None else max(0, size - max_bytes)
    if read_offset > size:
        read_offset = size
    raw = data[read_offset : read_offset + max_bytes]
    text = raw.decode("utf-8", errors="replace")
    truncated_head = read_offset > 0
    next_offset = read_offset + len(raw)
    truncated_tail = next_offset < size
    if tail_lines is not None:
        lines = text.splitlines()
        if tail_lines == 0:
            text = ""
        elif len(lines) > tail_lines:
            text = "\n".join(lines[-tail_lines:])
            truncated_head = True
    return {
        "path": str(path),
        "exists": True,
        "size": size,
        "offset": read_offset,
        "next_offset": next_offset if truncated_tail else None,
        "truncated_head": truncated_head,
        "truncated_tail": truncated_tail,
        "text": text,
    }


def _read_log_window(
    path: Path,
    *,
    offset: int | None,
    tail_lines: int | None,
    max_bytes: int,
) -> _JSONDict | None:
    """Read a bounded slice of a log file (plain text or gzip artifact).

    - ``offset`` mode: byte offset into the file; reads up to ``max_bytes``.
    - ``tail_lines`` mode: read the trailing ``max_bytes`` and keep the last
      ``tail_lines`` lines (the most-recent slice operators care about).
    - When neither is set: read from the end like ``tail -c max_bytes``.

    Gzip artifacts are decompressed transparently and windowed over their
    decompressed contents.
    """
    if _is_gzip_artifact(path):
        return _read_gzip_log_window(
            path, offset=offset, tail_lines=tail_lines, max_bytes=max_bytes
        )
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": False,
            "error": str(error),
            "size": None,
            "offset": offset or 0,
            "next_offset": None,
            "truncated_head": False,
            "truncated_tail": False,
            "text": "",
        }
    read_offset = offset if offset is not None else max(0, size - max_bytes)
    if read_offset > size:
        read_offset = size
    try:
        with path.open("rb") as handle:
            handle.seek(read_offset)
            raw = handle.read(max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — treat as a
        # graceful 404 rather than surfacing the race as a half-filled
        # payload.
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": True,
            "error": str(error),
            "size": size,
            "offset": read_offset,
            "next_offset": None,
            "truncated_head": False,
            "truncated_tail": False,
            "text": "",
        }
    text = raw.decode("utf-8", errors="replace")
    truncated_head = read_offset > 0
    next_offset = read_offset + len(raw)
    truncated_tail = next_offset < size
    if tail_lines is not None:
        lines = text.splitlines()
        if tail_lines == 0:
            text = ""
        elif len(lines) > tail_lines:
            text = "\n".join(lines[-tail_lines:])
            truncated_head = True
    return {
        "path": str(path),
        "exists": True,
        "size": size,
        "offset": read_offset,
        "next_offset": next_offset if truncated_tail else None,
        "truncated_head": truncated_head,
        "truncated_tail": truncated_tail,
        "text": text,
    }


def _read_bounded_text(path: Path, *, max_bytes: int) -> _JSONDict | None:
    """Read up to ``max_bytes`` from the start of ``path`` as text.

    Gzip artifacts are decompressed transparently before the byte bound is
    applied so plain and gzip logs read through the same API.
    """
    if _is_gzip_artifact(path):
        try:
            data = artifacts.read_gzip(path)
        except artifacts.ArtifactError as error:
            if not path.exists():
                return None
            return {
                "path": str(path),
                "exists": True,
                "size": None,
                "error": str(error),
                "truncated": False,
                "text": "",
            }
        raw = data[:max_bytes]
        return {
            "path": str(path),
            "exists": True,
            "size": len(data),
            "truncated": len(raw) < len(data),
            "text": raw.decode("utf-8", errors="replace"),
        }
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": False,
            "size": None,
            "error": str(error),
            "truncated": False,
            "text": "",
        }
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except FileNotFoundError:
        # File vanished between ``stat()`` and ``open()`` — surface as a
        # clean 404 so the operator UI does not see a fake successful
        # zero-byte response.
        return None
    except OSError as error:
        return {
            "path": str(path),
            "exists": True,
            "size": size,
            "error": str(error),
            "truncated": False,
            "text": "",
        }
    text = raw.decode("utf-8", errors="replace")
    return {
        "path": str(path),
        "exists": True,
        "size": size,
        "truncated": len(raw) < size,
        "text": text,
    }


def _progress_entry_payload(entry: plans.ProgressEntry) -> _JSONDict:
    return {
        "session_id": entry.session_id.value,
        "status": entry.status,
        "completed_at": entry.completed_at,
        "files_modified": list(entry.files_modified),
        "key_decisions": entry.key_decisions,
        "discovered_facts": list(entry.discovered_facts),
        "open_questions": list(entry.open_questions),
        "blockers": entry.blockers,
    }


def _build_commit_rows(records: Sequence[Mapping[str, Any]]) -> list[_JSONDict]:
    """Pair commit_started/commit_finished events into commit rows."""
    started: dict[int, _JSONDict] = {}
    rows: list[_JSONDict] = []
    for record in records:
        name = record.get("name")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        index = payload.get("commit_index")
        if not isinstance(index, int):
            continue
        if name == events_module.COMMIT_STARTED_EVENT:
            started[index] = {
                "commit_index": index,
                "scope": payload.get("scope"),
                "session_id": payload.get("session_id") or payload.get("session"),
                "run_id": payload.get("run_id"),
                "started_at": record.get("created_at"),
                "started_payload": payload,
                "finished_at": None,
                "status_result": None,
                "commit_sha": None,
                "duration_seconds": None,
                "finished_payload": None,
                "stdout_path": payload.get("stdout_path"),
                "stderr_path": payload.get("stderr_path"),
                "result_path": payload.get("result_path"),
            }
        elif name == events_module.COMMIT_FINISHED_EVENT:
            row = started.pop(index, None)
            if row is None:
                row = {
                    "commit_index": index,
                    "scope": payload.get("scope"),
                    "session_id": payload.get("session_id") or payload.get("session"),
                    "run_id": payload.get("run_id"),
                    "started_at": None,
                    "started_payload": None,
                    "stdout_path": payload.get("stdout_path"),
                    "stderr_path": payload.get("stderr_path"),
                    "result_path": payload.get("result_path"),
                }
            row["finished_at"] = record.get("created_at")
            row["status_result"] = payload.get("status_result")
            row["commit_sha"] = payload.get("commit_sha")
            row["duration_seconds"] = payload.get("duration_seconds")
            row["finished_payload"] = payload
            row["stdout_path"] = payload.get("stdout_path") or row.get("stdout_path")
            row["stderr_path"] = payload.get("stderr_path") or row.get("stderr_path")
            row["result_path"] = payload.get("result_path") or row.get("result_path")
            rows.append(row)
    # Any started-without-finished commits still in flight surface as rows.
    rows.extend(started.values())
    rows.sort(key=lambda row: row.get("commit_index", 0))
    return rows


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


def _artifact_descriptor(path: Path | None) -> _JSONDict:
    if path is None:
        return {"path": None, "exists": False, "kind": None, "size": None}
    try:
        stat = path.stat()
    except FileNotFoundError, NotADirectoryError:
        return {"path": str(path), "exists": False, "kind": None, "size": None}
    except OSError as error:
        return {
            "path": str(path),
            "exists": False,
            "kind": None,
            "size": None,
            "error": str(error),
        }
    if path.is_dir():
        return {
            "path": str(path),
            "exists": True,
            "kind": "directory",
            "size": None,
        }
    return {
        "path": str(path),
        "exists": True,
        "kind": "file",
        "size": stat.st_size,
    }


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
