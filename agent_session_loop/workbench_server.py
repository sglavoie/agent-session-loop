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
GET  ``/api/archive``                    - archived entries, retention, and auto-empty results.
POST ``/api/registry/pin``               - upsert a registry pin.
POST ``/api/registry/unpin``             - remove a registry pin.
POST ``/api/plan/archive``               - archive one plan (plan + ASL state).
POST ``/api/plans/archive``              - bulk-archive many plans (per-entry results).
POST ``/api/plan/delete``                - backward-compatible archive alias (was permanent delete).
POST ``/api/archive/restore``            - restore one archived entry to its origin.
POST ``/api/archive/delete``             - permanently purge one or many archive entries.
POST ``/api/archive/settings``           - update archive retention days.
POST ``/api/launch``                     - spawn ``asl run`` for a plan.
POST ``/api/reply``                      - resume a paused run with reply.
POST ``/api/control/stop-after-current`` - enqueue stop-after-current.
POST ``/api/control/select-harness``     - enqueue harness selection.
POST ``/api/unlock``                     - remove a stale operational lock.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import signal
import threading
import urllib.parse
import webbrowser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import (
    control_queue,
    events as events_module,
    harnesses,
    plans,
    qna,
    responses,
    state,
    tail,
    workbench,
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

    @property
    def bound_host(self) -> str:
        return self.server_address[0]

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


_JSONDict = dict[str, Any]


class WorkbenchRequestHandler(http.server.BaseHTTPRequestHandler):
    """Dispatch JSON requests to the Workbench helpers."""

    server_version = "ASLWorkbench/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
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
            self._send_error(
                400, error.reason, error.message, details=error.details
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
                self._handle_static_asset(path[len(STATIC_URL_PREFIX):])
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
            ("POST", "/api/control/select-harness"): (
                self._handle_control_select_harness
            ),
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
            provided = header[len(AUTH_SCHEME):].strip()
            if provided and secrets.compare_digest(
                provided, self._server().token
            ):
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
            )
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
            )
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise _BadRequestError(
                "invalid-request",
                f"request body must be valid JSON: {error}",
            )
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
        except (BrokenPipeError, ConnectionResetError):
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
                except (BrokenPipeError, ConnectionResetError):
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
        except (BrokenPipeError, ConnectionResetError):
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
            _augment_archivable(summary.to_payload(), summary)
            for summary in summaries
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
        pin = next(
            (p for p in registry.pins if p.plan_dir == str(canonical)), None
        )
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
        discovered = workbench.DiscoveredPlan(
            plan_dir=snapshot.plan_dir, exists=True
        )
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
        payload = state._runner_state_payload(state_path)
        next_pending = snapshot.next_pending
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
            state_path=str(state_path),
            state=payload,
            sessions=[
                state._session_payload(session) for session in snapshot.sessions
            ],
            counts=state._status_counts(snapshot),
            next_pending_id=next_pending.id if next_pending is not None else None,
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
        run_id = filter_run if filter_run else tail._latest_run_id(
            state_payload, records
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
        runner_state = state._runner_state_payload(state_path)
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
        run_id = filter_run if filter_run else tail._latest_run_id(
            state_payload, records
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
        self._send_success(
            reason="ok",
            plan_dir=str(canonical),
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

    def _handle_archive_settings(
        self, *, query: Mapping[str, list[str]]
    ) -> None:
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
        plan_dir = _require_string(body, "plan_dir")
        profile = _coerce_profile_payload(body.get("profile"))
        config = self._server().config
        try:
            result = workbench.launch_plan(
                plan_dir,
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
        self._send_success(reason="ok", launch=result.to_payload())

    def _handle_reply(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        reply_text = _require_string(body, "reply_text")
        profile = _coerce_profile_payload(body.get("profile"))
        config = self._server().config
        try:
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
        self._send_success(reason="ok", launch=result.to_payload())

    def _handle_control_stop_after_current(
        self, *, query: Mapping[str, list[str]]
    ) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        command_id = _optional_string(body, "command_id")
        config = self._server().config
        try:
            queued = control_queue.append_command(
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
                queued_command=queued.to_payload(),
            ),
        )

    def _handle_control_select_harness(
        self, *, query: Mapping[str, list[str]]
    ) -> None:
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
            queued = control_queue.append_command(
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
                queued_command=queued.to_payload(),
            ),
        )

    def _handle_unlock(self, *, query: Mapping[str, list[str]]) -> None:
        del query
        body = self._read_json_body()
        plan_dir = _require_string(body, "plan_dir")
        force = bool(body.get("force", False))
        config = self._server().config
        try:
            result = state.unlock_plan(
                plan_dir, config.state_dir, force=force
            )
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
        return self.server  # type: ignore[return-value]

    def _require_query_value(
        self, query: Mapping[str, list[str]], key: str
    ) -> str:
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

    def _resolve_plan_state(
        self, query: Mapping[str, list[str]]
    ) -> tuple[Path, Path]:
        plan_dir = self._require_query_value(query, "plan_dir")
        try:
            canonical = plans.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            raise _BadRequestError(
                "invalid-plan",
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            )
        config = self._server().config
        plan_state = state.plan_state_dir(canonical, config.state_dir)
        return canonical, plan_state

    def _build_summary(
        self, canonical: Path, state_dir: Path | None
    ) -> workbench.WorkbenchSummary:
        discovered = workbench.DiscoveredPlan(
            plan_dir=canonical, exists=canonical.is_dir()
        )
        return workbench.build_workbench_summary(
            discovered, state_dir=state_dir
        )

    def _validate_log_path(self, candidate: str, plan_state: Path) -> Path:
        try:
            resolved = plans.canonicalize_path(candidate)
        except (OSError, ValueError) as error:
            raise _BadRequestError(
                "invalid-request",
                f"invalid path: {error}",
                details={"path": candidate},
            )
        try:
            resolved.relative_to(plan_state)
        except ValueError:
            raise _BadRequestError(
                "invalid-request",
                "path must be inside the plan state directory",
                details={
                    "path": str(resolved),
                    "plan_state_dir": str(plan_state),
                },
            )
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
        model=_coerce_optional_str(raw.get("model")),
        effort=_coerce_optional_str(raw.get("effort")),
        timeout=_coerce_optional_int(raw.get("timeout")),
        commit_mode=_coerce_optional_str(raw.get("commit_mode")),
        review_harness=_coerce_optional_str(raw.get("review_harness")),
        unsafe_bypass=_coerce_optional_bool(raw.get("unsafe_bypass")),
        allow_not_loop_ready=_coerce_optional_bool(
            raw.get("allow_not_loop_ready")
        ),
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


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadRequestError(
            "invalid-request", "profile timeout must be an integer when set"
        )
    return value


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _BadRequestError(
            "invalid-request",
            "profile boolean fields must be booleans when set",
        )
    return value


def _optional_query_value(
    query: Mapping[str, list[str]], key: str
) -> str | None:
    values = query.get(key)
    if not values:
        return None
    text = values[0].strip()
    return text or None


def _optional_int_query(
    query: Mapping[str, list[str]], key: str
) -> int | None:
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
        )


def _read_log_window(
    path: Path,
    *,
    offset: int | None,
    tail_lines: int | None,
    max_bytes: int,
) -> _JSONDict | None:
    """Read a bounded slice of a log file.

    - ``offset`` mode: byte offset into the file; reads up to ``max_bytes``.
    - ``tail_lines`` mode: read the trailing ``max_bytes`` and keep the last
      ``tail_lines`` lines (the most-recent slice operators care about).
    - When neither is set: read from the end like ``tail -c max_bytes``.
    """
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
    """Read up to ``max_bytes`` from the start of ``path`` as text."""
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
                "session_id": payload.get("session_id")
                or payload.get("session"),
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
                    "session_id": payload.get("session_id")
                    or payload.get("session"),
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
            row["stdout_path"] = payload.get("stdout_path") or row.get(
                "stdout_path"
            )
            row["stderr_path"] = payload.get("stderr_path") or row.get(
                "stderr_path"
            )
            row["result_path"] = payload.get("result_path") or row.get(
                "result_path"
            )
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
    except (FileNotFoundError, NotADirectoryError):
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
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not crash here
                pass

        threading.Thread(
            target=runner, name="workbench-shutdown", daemon=True
        ).start()

    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _trigger_shutdown)
        except (ValueError, OSError):
            # signal() raises ValueError when called outside the main
            # thread (notably in test threads); skip silently — tests
            # call ``server.shutdown()`` directly.
            continue

    def restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                continue

    return restore


def _default_open_in_browser(url: str) -> bool:
    """Attempt to open ``url`` in the operator's default browser."""
    try:
        return bool(webbrowser.open(url, new=1))
    except (webbrowser.Error, OSError):
        return False


def workbench_serve_from_args(
    args: Any,
    *,
    open_browser: Callable[[str], bool] | None = None,
    serve_forever: Callable[[WorkbenchHTTPServer], None] | None = None,
    install_signal_handlers: Callable[
        [WorkbenchHTTPServer], Callable[[], None]
    ]
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
