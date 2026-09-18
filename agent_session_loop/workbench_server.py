"""Workbench HTTP server: the stdlib transport, and nothing else.

This module used to *be* the Workbench API -- routing, authentication, request
policy, envelopes, static assets, and thirty-four handlers, all of them local
and none of them reachable from the Pi. The application moved to
:mod:`asl_workbench.http`, which the Pi serves too, and the execution moved to
:mod:`agent_session_loop.workbench_local`, which is the Mac's provider. What is
left here is the compatibility adapter the consolidation promised: it binds a
socket, mints the operator's token, turns a stdlib request into an
:class:`asl_workbench.http.Request` and a :class:`asl_workbench.http.Response`
back into bytes, and keeps every name the CLI, the bridge, and the existing
tests already import.

It translates. It does not decide. Which routes exist, which role each needs,
how a body is bounded, what a refusal says, and how a static asset is cached
are all answered in the shared application, so the local server and the Pi
cannot drift apart by one of them being edited.

Authentication
--------------
Mutating requests require an ``Authorization: Bearer <token>`` header. The
token is generated with :mod:`secrets` at server startup and printed to the
operator console alongside the bound URL. Read endpoints are token-free: the
server binds loopback, the reads have no effects, and the deep links ``asl
workbench`` and AgentMux hand out carry the token for the writes.
:class:`asl_workbench.http.LocalTokenAuthenticator` holds that boundary and
says why it is kept.

Endpoints
---------
:data:`asl_workbench.http.ROUTES` is the list, and it is readable at runtime
rather than only in prose -- ``GET /api/capabilities`` returns it, along with
any operation this mode cannot carry out and the reason. The two routes the
consolidation adds to the historic thirty-four are that one and
``GET /api/summary``, the counts-and-freshness view a caller holding only a
summary credential may read.

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
import secrets
import signal
import threading
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from asl_workbench import controls as portable_controls
from asl_workbench import details as workbench_detail_rules
from asl_workbench import http as workbench_http
from asl_workbench.http import (
    LOCAL_POLICY,
    STATIC_ASSET_CACHE_CONTROL,
    STATIC_DEFAULT_MIME,
    STATIC_INDEX_CACHE_CONTROL,
    STATIC_INDEX_FILE,
    STATIC_MIME_TYPES,
    STATIC_URL_PREFIX,
    Application,
    LocalTokenAuthenticator,
    Request,
    Response,
    shell_bundle,
)

from . import responses, workbench_local
from .workbench_local import LaunchBook, LocalProvider

TOKEN_BYTES = 32
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
#: The body cap, owned by the shared request policy so the Pi cannot accept a
#: body this server would refuse.
MAX_REQUEST_BODY = workbench_http.MAX_REQUEST_BODY
AUTH_SCHEME = "Bearer "
COMMAND_NAME = workbench_http.COMMAND_NAME

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
#: Launch bookkeeping bounds, now owned by the provider that does the
#: bookkeeping. Re-exported because both were part of this module's surface.
MAX_LAUNCH_RECEIPTS = workbench_local.MAX_LAUNCH_RECEIPTS
MAX_LAUNCH_ID_CHARS = workbench_local.MAX_LAUNCH_ID_CHARS

#: The queue's oldest-first ordering, kept reachable under its historic name
#: while the rule itself lives with the provider that applies it.
_sort_summaries_oldest_first = workbench_local.sort_summaries_oldest_first

#: This server no longer carries its own copy of the front end. The assets are
#: the portable package's, so "does the Pi serve the same shell as the laptop?"
#: is not a question about two directories staying in sync -- there is one.
#: Kept under its historic name because it was part of this module's surface.
STATIC_DIR = workbench_http.STATIC_ROOT

__all__ = [
    "AUTH_SCHEME",
    "COMMAND_NAME",
    "DEFAULT_EVENT_LIMIT",
    "DEFAULT_FINAL_REVIEW_BYTES",
    "DEFAULT_HOST",
    "DEFAULT_LAST_MESSAGE_BYTES",
    "DEFAULT_LOG_BYTES",
    "DEFAULT_PORT",
    "MAX_EVENT_LIMIT",
    "MAX_LAUNCH_ID_CHARS",
    "MAX_LAUNCH_RECEIPTS",
    "MAX_LOG_BYTES",
    "MAX_REDIRECT_OPERATIONS",
    "MAX_REQUEST_BODY",
    "STATIC_ASSET_CACHE_CONTROL",
    "STATIC_DEFAULT_MIME",
    "STATIC_DIR",
    "STATIC_INDEX_CACHE_CONTROL",
    "STATIC_INDEX_FILE",
    "STATIC_MIME_TYPES",
    "STATIC_URL_PREFIX",
    "ServerConfig",
    "TOKEN_BYTES",
    "WorkbenchHTTPServer",
    "WorkbenchRequestHandler",
    "create_server",
    "workbench_serve_from_args",
]


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
    """Threaded HTTP server holding one token and one shared application."""

    allow_reuse_address = True
    daemon_threads = True
    # The shell is native ES modules, so one page load is one document plus a
    # stylesheet plus every module in the import graph, and a browser opens
    # them all at once. `socketserver`'s default backlog of five means the
    # accept queue overflows mid-burst and the kernel resets the connections
    # that did not fit -- which a browser reports as a module that failed to
    # load, and a failed module is a blank page rather than a slow one.
    request_queue_size = 64

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
        self.launches = LaunchBook()
        self.provider = LocalProvider(config, launches=self.launches)
        self.application = Application(
            self.provider,
            LocalTokenAuthenticator(lambda: self.token),
            policy=LOCAL_POLICY,
            static=shell_bundle(),
        )

    # -- Launch bookkeeping, kept under its historic names -----------------

    def launch_guard(self, plan_dir: Path) -> threading.Lock:
        """Return the lock that serializes launch decisions for one plan."""
        return self.launches.guard(plan_dir)

    def recorded_launch(self, plan_dir: Path, launch_id: str) -> dict[str, Any] | None:
        """Return the answer an earlier identical launch request already got."""
        return self.launches.recorded(plan_dir, launch_id)

    def record_launch(
        self, plan_dir: Path, launch_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Remember one launch so its request can be retried without repeating it."""
        self.launches.record(plan_dir, launch_id, payload)

    def record_launch_pid(self, plan_dir: Path, pid: int) -> None:
        """Remember the process this server spawned for one plan."""
        self.launches.record_pid(plan_dir, pid)

    def launched_pid(self, plan_dir: Path) -> int | None:
        """Return the last process this server spawned for one plan, if any."""
        return self.launches.pid(plan_dir)

    def forget_launch_pid(self, plan_dir: Path) -> None:
        """Drop the recorded pid for a plan whose process has exited."""
        self.launches.forget_pid(plan_dir)

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
    """Carry stdlib requests to the shared application and its answers back.

    Every method below is transport: reading a request line, writing headers,
    surviving a client that hung up. The one decision it makes is how many
    bytes to hand :meth:`Application.handle` when it asks for the body, and
    even that number was decided by the shared request policy.
    """

    server_version = "ASLWorkbench/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Silence the default per-request access log so the operator-facing
        # console only carries deliberate startup output. Run-control events
        # remain the canonical audit trail for mutating actions.
        return

    # -- HTTP method entry points ------------------------------------------

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def _serve(self, method: str) -> None:
        request = Request.build(
            method,
            self.path,
            # `.items()` rather than the message itself: `http.client` hands
            # over an `email.message.Message`, which answers like a mapping
            # without being one, and the application takes pairs.
            self.headers.items(),
            read=self._read_exactly,
        )
        self._write(self._server().application.handle(request))

    def _read_exactly(self, length: int) -> bytes:
        """Read the body the application already decided may exist."""
        return self.rfile.read(length)

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Answer the stdlib's own refusals in this API's envelope.

        The default implementation renders an HTML page that quotes the
        request line back, so a malformed request line becomes a reflected
        copy of whatever was sent. Nothing this server answers with should be
        chosen by the client, so the body is the envelope and the text is
        fixed.
        """
        del message, explain
        self.close_connection = True
        self._write(
            Response.json(
                code,
                workbench_http.error_payload(
                    COMMAND_NAME,
                    reason="invalid-request",
                    error="the request could not be understood",
                    paused=False,
                ),
                close=True,
            )
        )

    def _write(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            if response.content_type is not None:
                self.send_header("Content-Type", response.content_type)
            if response.sends_content_length:
                self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            if response.close:
                # The body was refused before it was read, so whatever the
                # client is still sending has nowhere to go but the next
                # request's parser.
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and response.has_body:
                self.wfile.write(response.body)
        except BrokenPipeError, ConnectionResetError:
            # Client disconnected before the response could be flushed; the
            # server keeps running for the next request.
            return

    def _server(self) -> WorkbenchHTTPServer:
        # `BaseRequestHandler.server` is declared as the base class, and this
        # handler is only ever constructed by `WorkbenchHTTPServer`. Asserting
        # it is what carries the concrete type without an inline suppression.
        server = self.server
        assert isinstance(server, WorkbenchHTTPServer)
        return server


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
