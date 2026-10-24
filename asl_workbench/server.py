"""The stdlib transport for the shared application, on either host.

:mod:`asl_workbench.http` decides everything about a request that is a
decision: which routes exist, which role each needs, how a body is bounded,
what a refusal says, how a static asset is cached. What was still missing was
the part underneath -- a socket, a request line, a response written back --
and it existed only on the Mac, inside
``agent_session_loop.workbench_server``. A module in ``agent_session_loop``
cannot be packaged for the Pi, so an installed service had an application and
no way to serve it.

This module is that transport, and nothing else. It translates; it does not
decide. Both deployments now run the same eleven lines of header writing, so
"does the Pi answer a malformed request the way the laptop does?" stops being
a question about two implementations agreeing and becomes a question with one
answer.

The split is by what varies. :class:`ApplicationServer` binds a socket and
holds one :class:`~asl_workbench.http.Application`; it knows nothing about
where that application's provider reads from. The Mac subclasses it to build a
local provider and mint an operator token; the installed service builds a
remote provider over its store. :class:`ApplicationRequestHandler` is shared
outright.
"""

from __future__ import annotations

import contextlib
import dataclasses
import http.server
import signal
import socketserver
import threading
from collections.abc import Callable
from typing import Any, cast

from .http import COMMAND_NAME, Application, Request, Response, error_payload

#: The accept backlog. The shell is native ES modules, so one page load is one
#: document plus a stylesheet plus every module in the import graph, and a
#: browser opens them all at once. ``socketserver``'s default of five means the
#: accept queue overflows mid-burst and the kernel resets the connections that
#: did not fit -- which a browser reports as a module that failed to load, and
#: a failed module is a blank page rather than a slow one.
REQUEST_QUEUE_SIZE = 64

#: What this server calls itself. No version of anything else: the header a
#: stranger reads should not name the interpreter running behind it.
SERVER_VERSION = "ASLWorkbench/1"

#: Seconds a connection may sit silent mid-request before its thread gives it
#: up. The handler speaks HTTP/1.0, so every connection carries one request and
#: closes; the bound is for a peer that opens a socket and then stops sending.
#: Every request is one small frame from a browser or a connector on a short
#: poll, so that peer is gone rather than slow, and without a bound each one
#: holds a thread.
REQUEST_TIMEOUT_SECONDS = 30.0


class ApplicationServer(http.server.ThreadingHTTPServer):
    """A threaded HTTP server holding exactly one shared application."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = REQUEST_QUEUE_SIZE

    def __init__(
        self,
        address: tuple[str, int],
        application: Application,
        *,
        handler: type[http.server.BaseHTTPRequestHandler] | None = None,
    ) -> None:
        # The application is attached before the socket is bound: a subclass
        # that failed to build its provider should not leave a listening socket
        # behind for whoever connects next.
        self.application = application
        super().__init__(address, handler or ApplicationRequestHandler)

    @property
    def bound_host(self) -> str:
        # AF_INET keeps server_address a (str, int) pair; the stdlib annotation
        # widens it to cover the Unix-socket case this server never uses.
        return cast(str, self.server_address[0])

    @property
    def bound_port(self) -> int:
        return int(self.server_address[1])


class ApplicationRequestHandler(http.server.BaseHTTPRequestHandler):
    """Carry stdlib requests to the shared application and its answers back.

    Every method below is transport: reading a request line, writing headers,
    surviving a client that hung up. The one decision it makes is how many
    bytes to hand :meth:`Application.handle` when it asks for the body, and
    even that number was decided by the shared request policy.
    """

    server_version = SERVER_VERSION
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:
        # Silence the default per-request access log. On the Mac the console
        # carries deliberate startup output only; on the Pi a request line is a
        # private URL and a plan name written to a file nobody rotates.
        return

    # -- HTTP method entry points ------------------------------------------

    def do_GET(self) -> None:
        self._serve("GET")

    def do_HEAD(self) -> None:
        # HEAD is a safe method: the shell and its assets answer it as GET does,
        # an API route answers 405, and `_write` drops the body either way.
        # Without this entry point the stdlib answers 501 on its own.
        self._serve("HEAD")

    def do_POST(self) -> None:
        self._serve("POST")

    def _serve(self, method: str) -> None:
        self._body_read = False
        request = Request.build(
            method,
            self.path,
            # `.items()` rather than the message itself: `http.client` hands
            # over an `email.message.Message`, which answers like a mapping
            # without being one, and the application takes pairs.
            self.headers.items(),
            read=self._read_exactly,
        )
        response = self.application().handle(request)
        if not self._body_read and _declares_body(request):
            # Refused (or answered) before the body was read: authentication,
            # role, origin and availability all decide first. Whatever the
            # client sent after the headers would be parsed as the next request
            # line, so the connection ends with this answer instead.
            response = dataclasses.replace(response, close=True)
        self._write(response)

    def _read_exactly(self, length: int) -> bytes:
        """Read the body the application already decided may exist."""
        self._body_read = True
        try:
            return self.rfile.read(length)
        except OSError:
            # A timeout or a reset mid-body leaves the stream at an unknown
            # offset, so nothing after it on this connection can be framed.
            self.close_connection = True
            raise

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
        fixed per status.
        """
        del message, explain
        self.close_connection = True
        reason, error = _STDLIB_REFUSALS.get(code, _STDLIB_REFUSAL_DEFAULT)
        self._write(
            Response.json(
                code,
                error_payload(
                    COMMAND_NAME,
                    reason=reason,
                    error=error,
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
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before the response could be flushed; the
            # server keeps running for the next request.
            return

    def application(self) -> Application:
        """The application this handler's server was built around.

        ``BaseRequestHandler.server`` is declared as the base class, and this
        handler is only ever constructed by an :class:`ApplicationServer`.
        Asserting it is what carries the concrete type without an inline
        suppression.
        """
        server = self.server
        assert isinstance(server, ApplicationServer)
        return server.application


#: What each refusal the stdlib raises before the application sees a request
#: says, keyed by status. The text is fixed so nothing the client sent is
#: echoed back; anything unlisted reads as a malformed request.
_STDLIB_REFUSAL_DEFAULT = ("invalid-request", "the request could not be understood")
_STDLIB_REFUSALS: dict[int, tuple[str, str]] = {
    400: _STDLIB_REFUSAL_DEFAULT,
    408: ("request-timeout", "the request was not received in time"),
    414: ("uri-too-long", "the request line is too long"),
    431: ("headers-too-large", "the request headers are too large"),
    501: ("not-implemented", "the request method is not supported"),
    505: ("http-version-not-supported", "the HTTP version is not supported"),
}


def _declares_body(request: Request) -> bool:
    """Whether bytes may follow this request's headers on the connection."""
    if request.header("transfer-encoding"):
        return True
    length = (request.header("content-length") or "").strip()
    return length not in ("", "0")


def install_shutdown_handlers(
    server: socketserver.BaseServer,
) -> Callable[[], None]:
    """Wire SIGINT / SIGTERM to ``server.shutdown()``.

    ``server.serve_forever()`` ordinarily breaks on ``KeyboardInterrupt``
    raised by SIGINT, but SIGTERM and callers that do not run on the main
    thread bypass that path. SIGTERM is what a service manager sends, so on the
    Pi this is the whole of "stops when asked" -- ``launchd`` and ``systemd``
    both escalate to SIGKILL on a process that ignores it. The handler
    schedules ``shutdown()`` from a daemon thread because calling it from
    inside the signal handler would deadlock against ``serve_forever``'s own
    loop. The returned callable restores the previous handlers.
    """

    def trigger(signum: int, _frame: Any) -> None:
        del signum

        def runner() -> None:
            # Shutdown must not crash here.
            with contextlib.suppress(Exception):
                server.shutdown()

        threading.Thread(target=runner, name="workbench-shutdown", daemon=True).start()

    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, trigger)
        except (ValueError, OSError):
            # signal() raises ValueError when called outside the main thread
            # (notably in test threads); skip silently -- those callers call
            # ``server.shutdown()`` directly.
            continue

    def restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                continue

    return restore


__all__ = [
    "REQUEST_QUEUE_SIZE",
    "SERVER_VERSION",
    "ApplicationRequestHandler",
    "ApplicationServer",
    "install_shutdown_handlers",
]
