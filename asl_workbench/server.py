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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Silence the default per-request access log. On the Mac the console
        # carries deliberate startup output only; on the Pi a request line is a
        # private URL and a plan name written to a file nobody rotates.
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
        self._write(self.application().handle(request))

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
                error_payload(
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
