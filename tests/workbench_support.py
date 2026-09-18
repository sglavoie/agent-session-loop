"""Fixtures and live-surface readers for the Workbench consolidation tests.

Two kinds of helper, both in service of the same problem: the consolidation
needs an inventory that cannot quietly go stale.

* **Isolated fixtures.** :class:`WorkbenchAPIFixture` starts the real local API
  on loopback against a temporary state root and registry, following the
  pattern ``tests/test_asl_workbench_server.py`` established, and
  :class:`RecordingWorkbenchServer` stands in for one where only the deep-link
  shape matters. Characterization tests need the behavior, not the machine, so
  nothing here touches the operator's real state directory.

* **Live surface readers.** :func:`workbench_api_routes` and
  :func:`workbench_cli_entry_points` read today's surface out of today's code,
  so a route or subcommand added without a matrix row fails a test instead of
  being absent from an inventory nobody rereads.

Both readers now import the surface rather than parse it. ``cli.build_parser()``
is the CLI surface, and :data:`asl_workbench.http.ROUTES` is the HTTP one: the
route table used to be a local inside ``WorkbenchRequestHandler._dispatch``,
existing only during a request, so the only honest reader was an AST parse of
the server file. The unified application declares its routes at module scope --
one table, serving both the local server and the Pi -- so the inventory check
now reads the same object the application routes with.
"""

from __future__ import annotations

import argparse
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_session_loop import cli, workbench_server
from asl_workbench import http as workbench_http

#: The method/path pairs the front end reaches without the ``/api`` prefix. Kept
#: apart from the API routes because they are served before the route table is
#: consulted, and so are invisible to a reader of it.
STATIC_ROUTE_KEYS: tuple[str, ...] = ("GET /", "GET /static/")

#: The parser path to each Workbench CLI entry point, as a caller types it.
CLI_WORKBENCH_COMMAND = "workbench"
CLI_PLANS_COMMAND = "plans"


class SurfaceReadError(Exception):
    """Raised when the live Workbench surface cannot be read as expected."""


# -- Live surface readers ---------------------------------------------------


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, Any]:
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return dict(action.choices)
    return {}


def workbench_cli_entry_points() -> tuple[str, ...]:
    """Every Workbench-owned command line, as ``asl ...`` invocations.

    Read off the real parser, so a new ``asl workbench`` subcommand shows up
    here the moment it is registered. ``asl plans`` is included because it
    shares the Workbench summary builder and is therefore a surface the
    consolidation has to keep answering for.
    """
    top_level = _subparser_choices(cli.build_parser())
    missing = [
        name
        for name in (CLI_WORKBENCH_COMMAND, CLI_PLANS_COMMAND)
        if name not in top_level
    ]
    if missing:
        raise SurfaceReadError(f"cli.build_parser() has no {missing} command")
    entry_points = [f"asl {CLI_WORKBENCH_COMMAND}", f"asl {CLI_PLANS_COMMAND}"]
    for name in sorted(_subparser_choices(top_level[CLI_WORKBENCH_COMMAND])):
        entry_points.append(f"asl {CLI_WORKBENCH_COMMAND} {name}")
    return tuple(sorted(entry_points))


def workbench_api_routes() -> tuple[str, ...]:
    """Every API route as ``"<METHOD> <path>"``, sorted.

    Read straight off :data:`asl_workbench.http.ROUTES`, which is what the
    application dispatches with in both modes, so a route cannot exist without
    appearing here. A malformed entry raises rather than being skipped: a route
    this reader cannot understand is a route the completeness check would
    silently stop covering, which is the one outcome worse than a missing row.
    """
    routes: list[str] = []
    for route in workbench_http.ROUTES:
        if not route.method or not route.path.startswith("/"):
            raise SurfaceReadError(f"route {route!r} has no usable method/path")
        routes.append(f"{route.method} {route.path}")
    if not routes:
        raise SurfaceReadError("route table read as empty")
    duplicates = sorted({key for key in routes if routes.count(key) > 1})
    if duplicates:
        raise SurfaceReadError(f"route table declares {duplicates} twice")
    return tuple(sorted(routes))


# -- Fixtures ---------------------------------------------------------------


@dataclass
class RecordingWorkbenchServer:
    """A stand-in for a started Workbench, for tests about link shape.

    :class:`agent_session_loop.bridge.WorkbenchLifecycle` takes a
    ``server_factory``, so the deep-link contract -- token in the query string,
    plan and focus in the fragment -- can be pinned without binding a socket.
    It records the arguments it was constructed with, which is how "exactly one
    server, started lazily" is checked.
    """

    token: str = "recorded-token"
    bound_host: str = "127.0.0.1"
    bound_port: int = 4321
    kwargs: dict[str, Any] | None = None
    shutdown_calls: int = 0

    def serve_forever(self, poll_interval: float | None = None) -> None:
        del poll_interval
        self._stop = threading.Event()
        self._stop.wait()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.set()

    def server_close(self) -> None:
        return None


def recording_server_factory() -> tuple[Any, list[RecordingWorkbenchServer]]:
    """A ``server_factory`` and the list of servers it has been asked to build."""
    built: list[RecordingWorkbenchServer] = []

    def factory(**kwargs: Any) -> RecordingWorkbenchServer:
        server = RecordingWorkbenchServer(kwargs=dict(kwargs))
        built.append(server)
        return server

    return factory, built


class WorkbenchAPIFixture:
    """Start the real local API on loopback, isolated from the operator's state.

    A mixin rather than a base class so a :class:`unittest.TestCase` can use it
    alongside its own ``setUp``. Every server started through
    :meth:`start_workbench_server` is shut down and joined on cleanup, so a
    failing assertion cannot leave a listener behind for the next test to trip
    over.
    """

    # Declared, not assigned: the mixin is always combined with a TestCase,
    # which supplies the real method. The annotation is what lets the type
    # checker see that without an ignore comment.
    addCleanup: Callable[..., object]

    def install_workbench_api(self, tmp_path: Path) -> None:
        """Prepare an isolated state root and registry under ``tmp_path``."""
        self.workbench_root = Path(tmp_path).resolve()
        self.state_dir = self.workbench_root / "state"
        self.state_dir.mkdir(exist_ok=True)
        self.registry_path = self.workbench_root / "workbench.json"
        self.plans_root = self.workbench_root / "plans"
        self.plans_root.mkdir(exist_ok=True)

    def start_workbench_server(
        self,
        *,
        token: str | None = None,
        plan_roots: tuple[str, ...] = (),
    ) -> Any:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=plan_roots,
            include_default_dirs=False,
            token=token,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="workbench-foundations",
            daemon=True,
        )
        thread.start()

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    @staticmethod
    def base_url(server: Any) -> str:
        return f"http://{server.bound_host}:{server.bound_port}"

    @staticmethod
    def auth_headers(server: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {server.token}"}
