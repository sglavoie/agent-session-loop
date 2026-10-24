"""Fixtures and live-surface readers for the Workbench consolidation tests.

Two kinds of helper, both in service of the same problem: the consolidation
needs an inventory that cannot quietly go stale.

* **Isolated fixtures.** :class:`WorkbenchAPIFixture` starts the real local API
  on loopback against a temporary state root and registry, following the
  pattern ``tests/asl_workbench_server_support.py`` established, and
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
import http.server
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent_session_loop import (
    cli,
    events,
    plans_files,
    state,
    workbench_archive,
    workbench_history,
    workbench_registry_models,
    workbench_remote_reads,
    workbench_server,
)
from asl_workbench import catalog, observation
from asl_workbench import http as workbench_http
from asl_workbench.credentials import IDENTITY_HEADER, CredentialStore
from asl_workbench.observation import ObservationPolicy
from asl_workbench.remote_reads import ReadAnswer
from asl_workbench.service import RemoteProvider
from asl_workbench.store_workbench import WorkbenchStore

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
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
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

    :class:`agent_session_loop.bridge_workers.WorkbenchLifecycle` takes a
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


# -- The one shell, in both deployments -------------------------------------
#
# The claim session 15 has to be able to fail is "local and Pi modes serve the
# same assets, and the same navigation and detail workflows work against
# both". Proving it takes both providers running at once, so the two servers
# are built here rather than in either test that wants them: the Python suite
# drives them in-process and the Playwright harness starts the same two
# through `tests/workbench_browser/fixture_server.py`. One definition means a
# scenario cannot pass in one runner because it was seeded differently there.

#: The identity Tailscale Serve would have stamped. The remote mode trusts the
#: header, so the fixture supplies it exactly as Serve does.
SHELL_OWNER_IDENTITY = "owner@example.ts.net"
#: The connector whose observations the remote store is seeded from.
SHELL_CONNECTOR_ID = "mac"
#: A fixed clock, so "stale" stays stale however long a browser run takes and
#: relative timestamps render identically on every machine.
SHELL_FROZEN_NOW = 1789574400.0
SHELL_CONTRACT_SET_ID = observation.SUPPORTED_CONTRACT_SET_IDS[0]

#: The plans both modes carry, in the order they are seeded. The names are the
#: same on both sides so a scenario reads the same whichever provider answers.
SHELL_PLAN_NAMES: tuple[str, ...] = ("alpha", "bravo", "charlie")

#: ``alpha`` is paused waiting for an operator, ``bravo`` has never run, and
#: ``charlie`` finished a run and went quiet. Three states rather than one
#: because the shell's hardest jobs are telling them apart in the queue,
#: offering only the actions the run actually permits, and filing work that has
#: gone quiet behind the inactive-work window -- none of which a corpus of
#: identical plans can exercise.
SHELL_RUN_IDS: Mapping[str, str | None] = {
    "alpha": "20260918T080000Z-alpha",
    "bravo": None,
    "charlie": "20260915T100000Z-charlie",
}

#: How long ago ``charlie``'s run stopped moving. Comfortably past the
#: eight-hour window, so the row lands behind the older-runs toggle in both
#: modes rather than near the boundary where a slow test run could tip it.
SHELL_QUIET_SECONDS = 30 * 60 * 60

#: ``charlie``'s two finished runs, oldest first, as ASL would have recorded
#: them. One failure and one clean early stop, because the history pane's job
#: is to tell those apart and a corpus of one outcome cannot show it.
SHELL_HISTORY_RUNS: tuple[Mapping[str, Any], ...] = (
    {
        "run_id": "20260913T090000Z-charlie",
        "started_ago": 52 * 60 * 60,
        "duration": 900.0,
        "ok": False,
        "reason": "harness-error",
        "completed_sessions": ["1"],
        "pending": 1,
    },
    {
        "run_id": "20260915T100000Z-charlie",
        "started_ago": SHELL_QUIET_SECONDS + 1800,
        "duration": 1800.0,
        "ok": True,
        "reason": "max-sessions",
        "completed_sessions": ["1"],
        "pending": 1,
    },
)

#: Two archived plans, so the archive browser has both verdicts to draw.
#:
#: The archive is part of this corpus rather than of a fixture of its own
#: because the expired half of it is the one place the interface asks for a
#: deliberate acknowledgement before a destructive action: a purge offered over
#: an archive with nothing expired in it would never reach that gate, and a
#: purge offered over an archive where *everything* is expired would never
#: reach the case where the gate has to stay out of the way.
#:
#: ``archived_ago`` is measured against ``time.time()`` for the same reason the
#: history events are: the retention verdict is computed from the real clock,
#: so an entry pinned to :data:`SHELL_FROZEN_NOW` would quietly change verdict
#: as the fixture aged.
SHELL_ARCHIVE_ENTRIES: tuple[Mapping[str, Any], ...] = (
    {
        "archive_id": "20260731T090000Z-delta",
        "plan_name": "delta",
        "archived_ago": 45 * 24 * 60 * 60,
        "retention_days": workbench_registry_models.DEFAULT_ARCHIVE_RETENTION_DAYS,
        "summary": "delta: archived long enough ago to have expired",
    },
    {
        "archive_id": "20260916T090000Z-echo",
        "plan_name": "echo",
        "archived_ago": 2 * 24 * 60 * 60,
        "retention_days": workbench_registry_models.DEFAULT_ARCHIVE_RETENTION_DAYS,
        "summary": "echo: archived recently and still inside the window",
    },
)

#: The freshness proof ASL issued with ``alpha``'s paused state. The remote
#: mode redeems it on a mutation; without it a submission is refused as
#: unauthorized, which is the behaviour, not a fixture shortcut.
SHELL_ACTION_TOKEN = "shellfixtureactiontoken"

#: What ``alpha``'s paused run will admit. Published on the observation the Pi
#: holds, because the Pi decides nothing about authority: it repeats what the
#: Mac said was permitted.
SHELL_PERMITTED_ACTIONS: tuple[str, ...] = (
    "plan.reply",
    "plan.stop-after-current",
    "plan.abort",
)


__all__ = ["argparse", "http", "json", "threading", "time", "Callable", "Mapping", "dataclass", "field", "UTC", "datetime", "sha256", "Path", "Any", "cli", "events", "plans_files", "state", "workbench_archive", "workbench_history", "workbench_registry_models", "workbench_remote_reads", "workbench_server", "catalog", "observation", "workbench_http", "IDENTITY_HEADER", "CredentialStore", "ObservationPolicy", "ReadAnswer", "RemoteProvider", "WorkbenchStore", "STATIC_ROUTE_KEYS", "CLI_WORKBENCH_COMMAND", "CLI_PLANS_COMMAND", "SurfaceReadError", "_subparser_choices", "workbench_cli_entry_points", "workbench_api_routes", "RecordingWorkbenchServer", "recording_server_factory", "WorkbenchAPIFixture", "SHELL_OWNER_IDENTITY", "SHELL_CONNECTOR_ID", "SHELL_FROZEN_NOW", "SHELL_CONTRACT_SET_ID", "SHELL_PLAN_NAMES", "SHELL_RUN_IDS", "SHELL_QUIET_SECONDS", "SHELL_HISTORY_RUNS", "SHELL_ARCHIVE_ENTRIES", "SHELL_ACTION_TOKEN", "SHELL_PERMITTED_ACTIONS"]  # fmt: skip
