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
    plans,
    state,
    workbench,
    workbench_archive,
    workbench_history,
    workbench_remote_reads,
    workbench_server,
)
from asl_workbench import catalog, observation
from asl_workbench import http as workbench_http
from asl_workbench.credentials import IDENTITY_HEADER, CredentialStore
from asl_workbench.observation import ObservationPolicy
from asl_workbench.remote_reads import ReadAnswer
from asl_workbench.service import RemoteProvider
from asl_workbench.store import WorkbenchStore

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
        "retention_days": workbench.DEFAULT_ARCHIVE_RETENTION_DAYS,
        "summary": "delta: archived long enough ago to have expired",
    },
    {
        "archive_id": "20260916T090000Z-echo",
        "plan_name": "echo",
        "archived_ago": 2 * 24 * 60 * 60,
        "retention_days": workbench.DEFAULT_ARCHIVE_RETENTION_DAYS,
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


@dataclass(frozen=True)
class ShellCorpus:
    """One set of plans on disk, and the identities both modes derive from it.

    Both deployments are built over *this* object rather than over a directory
    each, because the thing under test is that they are two views of the same
    plans. Two corpora seeded the same way would produce two different sets of
    plan keys -- the key is a digest of the path -- and a ``#run=`` link would
    then resolve in whichever mode minted it, which is exactly the failure this
    fixture has to be able to see.
    """

    root: Path
    state_dir: Path
    registry_path: Path
    plans_root: Path
    plan_dirs: tuple[Path, ...]

    @property
    def plan_roots(self) -> tuple[str, ...]:
        """The search roots to hand a local reader, as it wants them."""
        return (str(self.plans_root),)

    @property
    def plan_keys(self) -> dict[str, str]:
        """Each plan's name mapped to the digest either mode addresses it by."""
        return {plan.name: catalog.plan_key(str(plan)) for plan in self.plan_dirs}

    @property
    def run_keys(self) -> dict[str, str]:
        """Each plan's name mapped to the run key a ``#run=`` link carries.

        Derived from the run the plan's state actually names -- ``None`` for a
        plan that has never run, which is a distinct component rather than the
        string. The remote catalog is published with these rather than with
        invented run ids, because a link that resolved in one mode and not the
        other would be two links wearing one format.
        """
        return {
            name: catalog.run_key(plan_key, SHELL_RUN_IDS.get(name))
            for name, plan_key in self.plan_keys.items()
        }

    def plan_dir(self, name: str) -> Path:
        """The canonical directory of a seeded plan, by name."""
        for plan in self.plan_dirs:
            if plan.name == name:
                return plan
        raise KeyError(name)


def shell_iso(epoch: float) -> str:
    """One moment in the spelling ASL writes into ``state.json``.

    The runner records ISO-8601 and the Pi records epoch seconds; both are read
    by the same window arithmetic. Writing the local side in ASL's own spelling
    is what makes that equivalence something the fixture proves rather than
    something it arranges around.
    """
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _seed_shell_runner_state(
    plan_dir: Path, state_dir: Path, name: str, run_id: str | None
) -> None:
    """Give a plan the runner state its published state claims it has.

    Written as a real ``state.json`` under the real per-plan state directory,
    so the local mode *derives* "paused for a reply" from the same file ASL
    would have written, rather than being told. A plan with no run id is left
    with no state at all, which is how a plan that has never run looks.

    ``charlie`` gets a *finished* state instead, because "this run ended and
    sessions remain" is a different row from "this run is waiting for you", and
    only the finished one can age out of the eight-hour window: a state with no
    ``run_finished_at`` and no live lock reads as stale, not as quiet.
    """
    if run_id is None:
        return
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    plan_state.mkdir(parents=True, exist_ok=True)
    if name == "charlie":
        last = SHELL_HISTORY_RUNS[-1]
        started = time.time() - float(last["started_ago"])
        payload: dict[str, Any] = {
            "plan_dir": str(plan_dir),
            "run_id": run_id,
            "session_id": "1",
            "ok": True,
            "paused": False,
            "reason": str(last["reason"]),
            "run_started_at": shell_iso(started),
            "run_finished_at": shell_iso(started + float(last["duration"])),
            "updated_at": started + float(last["duration"]),
        }
    else:
        payload = {
            "plan_dir": str(plan_dir),
            "run_id": run_id,
            "session_id": "2",
            "paused": True,
            "updated_at": SHELL_FROZEN_NOW - 60.0,
        }
    (plan_state / events.STATE_FILE_NAME).write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    if name == "charlie":
        _seed_shell_history_events(plan_state, plan_dir)


def _seed_shell_history_events(plan_state: Path, plan_dir: Path) -> None:
    """Write the endings ``charlie``'s history pane reads back.

    Real ``events.jsonl`` records rather than pre-built entries, so the local
    provider *interprets* them with the same importer the connector publishes
    from. The remote store is then seeded from that interpretation rather than
    from a second hand-written copy, which is what makes the two panes unable to
    disagree about what a run did.

    Timestamps are relative to the real clock and not to
    :data:`SHELL_FROZEN_NOW`, because the local read applies a seven-day
    retention measured against ``time.time()``: entries pinned to a fixed epoch
    would silently fall out of the window as the fixture ages.
    """
    now = time.time()
    records: list[events.Event] = []
    for run in SHELL_HISTORY_RUNS:
        started = now - float(run["started_ago"])
        finished = started + float(run["duration"])
        records.append(
            events.Event(
                name="run_started",
                created_at=shell_iso(started),
                payload={
                    "run_id": run["run_id"],
                    "plan_dir": str(plan_dir),
                    "harness": "claude",
                    "model": "opus",
                    "run_started_at": shell_iso(started),
                },
            )
        )
        records.append(
            events.Event(
                name="run_finished",
                created_at=shell_iso(finished),
                payload={
                    "run_id": run["run_id"],
                    "plan_dir": str(plan_dir),
                    # Both flags, always. ``classify_outcome`` refuses to name an
                    # outcome unless ``ok`` and ``paused`` are both real
                    # booleans, so a record that merely omits ``paused`` imports
                    # as ``unknown`` -- which would make the pane's whole job
                    # untestable.
                    "ok": run["ok"],
                    "paused": False,
                    "reason": run["reason"],
                    "run_started_at": shell_iso(started),
                    "run_finished_at": shell_iso(finished),
                    "duration_seconds": run["duration"],
                    "completed_session_ids": list(run["completed_sessions"]),
                    "counts": {
                        "complete": len(run["completed_sessions"]),
                        "pending": run["pending"],
                    },
                },
            )
        )
    (plan_state / events.EVENTS_FILE_NAME).write_text(
        "".join(
            json.dumps(record.to_dict(), sort_keys=True) + "\n" for record in records
        ),
        encoding="utf-8",
    )


def _seed_shell_archive(state_dir: Path, plans_root: Path) -> None:
    """Write the archive entries the archive browser reads back.

    Real entry directories under the real archive root, each with the
    ``archive.json`` the Workbench writes, because the listing is produced by
    enumerating that root: a hand-built listing would test the fixture's idea
    of an archive rather than the reader's.

    ``expires_at`` is stored, but it is *not* what decides the verdict -- the
    retention window in the registry is, applied to ``archived_at`` at read
    time. Both are written here so an entry that disagrees with itself would be
    a fixture bug rather than a silent pass.
    """
    now = time.time()
    for entry in SHELL_ARCHIVE_ENTRIES:
        archived_at = now - float(entry["archived_ago"])
        expires_at = archived_at + float(entry["retention_days"]) * 24 * 60 * 60
        entry_dir = workbench_archive.archive_root(state_dir) / str(entry["archive_id"])
        (entry_dir / workbench_archive.ARCHIVE_PLAN_DIR_NAME).mkdir(
            parents=True, exist_ok=True
        )
        (
            entry_dir / workbench_archive.ARCHIVE_PLAN_DIR_NAME / "manifest.md"
        ).write_text(
            f"# {str(entry['plan_name']).title()}\n\nOne archived plan.\n",
            encoding="utf-8",
        )
        (entry_dir / workbench_archive.ARCHIVE_METADATA_FILE_NAME).write_text(
            json.dumps(
                {
                    "archive_id": entry["archive_id"],
                    "original_plan_dir": str(plans_root / str(entry["plan_name"])),
                    "archived_at": shell_iso(archived_at),
                    "expires_at": shell_iso(expires_at),
                    "summary": entry["summary"],
                    "pin": None,
                    "plan_archived": True,
                    "state_archived": False,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def seed_shell_plans(root: Path) -> ShellCorpus:
    """Write the plan corpus both modes read, under ``root``.

    Each plan is a real plan tree with real sessions, because the local
    provider enumerates the filesystem and a stub directory would be
    enumerated as a malformed plan rather than as a plan.
    """
    root = Path(root).resolve()
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry_path = root / workbench.REGISTRY_FILE_NAME
    plans_root = root / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, name in enumerate(SHELL_PLAN_NAMES):
        plan_dir = plans_root / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(
            f"# {name.title()}\n\nOne plan, for the shell fixture.\n", encoding="utf-8"
        )
        # ``alpha`` and ``charlie`` are part-done; ``bravo`` has not started.
        # A plan with no pending session would be ``complete``, which the
        # default queue hides -- and a hidden row proves nothing about the
        # section it was meant to land in.
        statuses = ("pending", "pending") if index == 1 else ("complete", "pending")
        for number, status_value in enumerate(statuses, start=1):
            (plan_dir / f"session-{number}.md").write_text(
                "\n".join(
                    (
                        f"# Session {number}: {name.title()} step {number}",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        f"- **Status**: {status_value}",
                        "- **Files modified**: (none)",
                        "- **Key decisions**: (none)",
                        "- **Blockers for next session**: (none)",
                        "- **Notes**:",
                        "",
                    )
                ),
                encoding="utf-8",
            )
        canonical = plans.canonicalize_path(plan_dir)
        _seed_shell_runner_state(canonical, state_dir, name, SHELL_RUN_IDS.get(name))
        written.append(canonical)
    _seed_shell_archive(state_dir, plans.canonicalize_path(plans_root))
    return ShellCorpus(
        root=root,
        state_dir=state_dir,
        registry_path=registry_path,
        plans_root=plans.canonicalize_path(plans_root),
        plan_dirs=tuple(written),
    )


def start_local_shell_server(
    corpus: ShellCorpus,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
) -> tuple[Any, threading.Thread]:
    """The laptop's deployment: the real local server over a seeded corpus."""
    server = workbench_server.create_server(
        host=host,
        port=port,
        state_dir=corpus.state_dir,
        registry_path=corpus.registry_path,
        plan_roots=corpus.plan_roots,
        include_default_dirs=False,
        token=token,
    )
    thread = threading.Thread(
        target=server.serve_forever, name="workbench-shell-local", daemon=True
    )
    thread.start()
    return server, thread


# -- The remote half --------------------------------------------------------


def _shell_envelope(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": 1,
        "kind": kind,
        "connector_id": SHELL_CONNECTOR_ID,
        "sent_at": payload.get("observed_at", SHELL_FROZEN_NOW),
        "payload": payload,
    }


def shell_catalog_entries(corpus: ShellCorpus) -> list[dict[str, Any]]:
    """One published catalog entry per plan, keyed the way the Pi addresses it.

    The identities come from the corpus, which derives them with the catalog's
    own functions from the same plan directories the local mode enumerates. So
    the remote fixture publishes the *same* ``plan_key`` and ``run_key`` the
    laptop computes, and one ``#run=`` link resolves in either deployment
    rather than in whichever one minted it.
    """
    plan_keys = corpus.plan_keys
    run_keys = corpus.run_keys
    entries = []
    for index, name in enumerate(SHELL_PLAN_NAMES):
        plan_key = plan_keys[name]
        quiet = name == "charlie"
        paused = SHELL_RUN_IDS.get(name) is not None and not quiet
        if quiet:
            state_name, action, group = "recent", "run", "ready"
            activity = "Run finished, sessions remain"
        elif paused:
            state_name, action, group = "paused-for-reply", "reply", "needs-action"
            activity = "Reply needed"
        else:
            state_name, action, group = "ready", "run", "ready"
            activity = "Ready to run"
        entry: dict[str, Any] = {
            "plan_key": plan_key,
            "run_key": run_keys[name],
            # The words the Mac derived, not words invented for the Pi. These
            # plans register no repositories, so the catalog names their project
            # ``Unknown project`` and titles them from the manifest's ``# H1``;
            # publishing anything else here would put two different names for
            # one plan on the two deployments and make a cross-mode assertion
            # impossible to write honestly.
            "title": name.title(),
            "display_label": name.title(),
            # What the Mac derived from this plan's state, repeated. The
            # local mode derives the same words from the same file, so the
            # two queues describe one plan rather than agreeing by luck.
            "primary_state": state_name,
            "recommended_action": action,
            "urgency_rank": index,
            "urgency_group": group,
            "activity_label": activity,
            "projects": [catalog.UNKNOWN_PROJECT],
            "project_group": catalog.UNKNOWN_PROJECT,
            "counts": {"sessions": 2},
            "sources": ["manifest"],
            "exists": True,
            "pinned": False,
            "repository_count": 0,
            "warning_count": 0,
            "search_text": name.casefold(),
            "registry_digest": "9d2e59249fe21a3c",
        }
        if quiet:
            # Epoch seconds, which is how the Pi publishes a moment; the local
            # mode publishes ISO-8601 for the same plan. Both are read by one
            # window, and seeding them differently is what proves that.
            entry["last_activity_at"] = SHELL_FROZEN_NOW - SHELL_QUIET_SECONDS
            entry["last_activity_source"] = "run-finished"
        entries.append(entry)
    return entries


def seed_shell_history(store: WorkbenchStore, corpus: ShellCorpus) -> None:
    """Publish the endings the Mac imported, exactly as the connector would.

    The entries are not written by hand: they are the objects
    :func:`agent_session_loop.workbench_history.read_window` builds from the
    corpus's own ``events.jsonl``, which is the same projection the local
    history route answers with. Seeding the store from that projection is what
    makes "both modes present consistent history" a property of the code rather
    than of two hand-maintained copies agreeing.

    The window is read against the real clock for the same reason it is written
    against it -- a seven-day retention measured from ``time.time()`` would drop
    entries pinned to :data:`SHELL_FROZEN_NOW` as the fixture ages -- while the
    batch is *received* at the frozen moment, because that is when the Pi is
    pretending to have heard from the Mac.
    """
    view = workbench_history.read_window(now=time.time(), state_dir=corpus.state_dir)
    store.ingest_history(
        _shell_envelope(
            "history_batch",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "batch_id": "shell-fixture-history-1",
                "generated_at": SHELL_FROZEN_NOW,
                "window_start": view.window_start,
                "window_end": view.window_end,
                "complete": view.complete,
                "more": False,
                "entries": [entry.to_payload() for entry in view.entries],
                "questions": [],
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )


def seed_shell_store(
    store: WorkbenchStore,
    entries: list[dict[str, Any]],
    *,
    corpus: ShellCorpus | None = None,
) -> None:
    """Give the Pi something to have observed: a heartbeat, a catalog, history."""
    store.ingest_heartbeat(
        _shell_envelope(
            "heartbeat",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "bridge_alive": True,
                "bridge_protocol": 2,
                "asl_version": "0.2.0",
                "contract_set_id": SHELL_CONTRACT_SET_ID,
                "source_generation": 1,
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )
    runs = []
    for name, entry in zip(SHELL_PLAN_NAMES, entries, strict=True):
        run_id = SHELL_RUN_IDS.get(name)
        live = run_id is not None and name != "charlie"
        run: dict[str, Any] = {
            "run_key": entry["run_key"],
            "project": catalog.UNKNOWN_PROJECT,
            "plan_key": entry["plan_key"],
            # ``None`` where no run has started, which is the component the run
            # key above digests. Naming an id here would contradict it.
            "run_id": run_id,
            "primary_state": entry["primary_state"],
            "phase": entry["primary_state"],
            "urgency_group": entry["urgency_group"],
            # Only a live run permits anything, and only the Mac decides what.
            # The Pi repeats the list and the token that redeems it; a
            # submission naming an action not in this list is refused before it
            # is carried anywhere. A finished run permits nothing, which is why
            # ``charlie`` carries an id and an empty list rather than both.
            "actions": list(SHELL_PERMITTED_ACTIONS) if live else [],
            "last_activity_at": entry.get("last_activity_at", SHELL_FROZEN_NOW - 30.0),
        }
        if live:
            run["action_token"] = SHELL_ACTION_TOKEN
            run["session_id"] = "2"
        runs.append(run)
    material = json.dumps([SHELL_FROZEN_NOW, runs], sort_keys=True).encode()
    store.ingest_snapshot(
        _shell_envelope(
            "snapshot",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "digest": sha256(material).hexdigest(),
                "contract_set_id": SHELL_CONTRACT_SET_ID,
                "staleness_bound_seconds": 20.0,
                "source_generation": 1,
                "row_count": len(runs),
                "truncated": False,
                "runs": runs,
                "warnings": [],
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )
    store.ingest_catalog(
        _shell_envelope(
            "catalog_page",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "generation": "shell-fixture-1",
                "source_generation": 1,
                "complete": True,
                "final": True,
                "offset": 0,
                "total": len(entries),
                "entries": entries,
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )
    if corpus is not None:
        seed_shell_history(store, corpus)


@dataclass
class ReplayingDetailLane:
    """A read lane that answers out of a real local read, as the Mac would.

    The Pi never reads a file: it asks, and the Mac answers with material this
    package already knows how to produce. Replaying a real
    :mod:`agent_session_loop.workbench_remote_reads` answer keeps the fixture
    honest about the transport's own wrapper -- ``entries`` and ``content``
    rather than the local key names -- which is the difference the shared
    front end has to absorb.
    """

    responder: Any
    asks: list[dict[str, Any]] = field(default_factory=list)

    def fetch(self, **kwargs: Any) -> ReadAnswer:
        self.asks.append(dict(kwargs))
        target: dict[str, Any] = (
            {"archive": "list"}
            if kwargs["detail"] == "archive"
            else {"plan_key": kwargs["plan_key"]}
        )
        if kwargs.get("run_key"):
            target["run_key"] = kwargs["run_key"]
        request: dict[str, Any] = {"detail": kwargs["detail"], "target": target}
        for name in ("artifact", "stream", "offset", "limit", "max_bytes"):
            value = kwargs.get(name)
            if value is not None:
                request[name] = value
        payload = self.responder.answer(request, now=kwargs["now"])
        answered = dict(payload.get("target") or {})
        reason = payload.get("reason")
        return ReadAnswer(
            request_id=f"r{len(self.asks)}",
            # A refusal the Mac chose, not a link that dropped: the fixture is
            # paired, so the only unanswered reads are the ones ASL declined.
            outcome="refused" if reason else "answered",
            detail=str(payload.get("detail") or kwargs["detail"]),
            plan_key=str(answered.get("plan_key") or kwargs["plan_key"]),
            run_key=answered.get("run_key"),
            artifact=payload.get("artifact"),
            stream=payload.get("stream"),
            complete=str(payload.get("complete") or "unknown"),
            content=payload.get("content"),
            entries=tuple(payload.get("entries") or ()),
            offset=int(payload.get("offset") or 0),
            next_offset=payload.get("next_offset"),
            total=payload.get("total"),
            truncated=bool(payload.get("truncated")),
            reason=reason,
            note=str(reason or ""),
            warnings=tuple(payload.get("warnings") or ()),
            observed_at=payload.get("observed_at"),
            received_at=kwargs["now"],
        )


@dataclass
class RecordingSubmissionLane:
    """A mutation lane that writes the submission down and carries it nowhere.

    Which is the honest shape of a paired Pi: ``202`` means the service has
    the request and the Mac has not run it yet.
    """

    handed: list[dict[str, Any]] = field(default_factory=list)

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        del now
        self.handed.append(dict(payload))


class ShellFixtureServer(http.server.ThreadingHTTPServer):
    """A loopback transport for any :class:`Application`, for fixtures only.

    ``WorkbenchHTTPServer`` builds a ``LocalProvider`` in its constructor, so
    it cannot carry the remote one. Everything below the application is
    borrowed from the real handler rather than written again -- this class
    supplies the application and nothing else.
    """

    allow_reuse_address = True
    daemon_threads = True
    # Matches ``WorkbenchHTTPServer``: a browser fetches the whole module graph
    # in one burst, and the default backlog of five resets whatever does not
    # fit. See the comment there.
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        application: workbench_http.Application,
    ) -> None:
        super().__init__(address, _ShellFixtureHandler)
        self.application = application

    @property
    def bound_host(self) -> str:
        return str(self.server_address[0])

    @property
    def bound_port(self) -> int:
        return int(self.server_address[1])


class _ShellFixtureHandler(workbench_server.WorkbenchRequestHandler):
    """The real transport, pointed at this fixture's application."""

    def _serve(self, method: str) -> None:
        request = workbench_http.Request.build(
            method,
            self.path,
            self.headers.items(),
            read=self._read_exactly,
        )
        server = self.server
        assert isinstance(server, ShellFixtureServer)
        self._write(server.application.handle(request))


def start_remote_shell_server(
    corpus: ShellCorpus, *, host: str = "127.0.0.1", port: int = 0
) -> tuple[ShellFixtureServer, threading.Thread, WorkbenchStore]:
    """The Pi's deployment: the remote provider over a seeded observation store.

    The plan corpus underneath is the same one the local mode reads, and the
    catalog is published with the digests this package derives from it, so the
    two servers are two views of one set of plans rather than two fixtures
    that happen to look alike.
    """
    store = WorkbenchStore.open(
        corpus.root / "workbench.db",
        policy=ObservationPolicy(),
        created_at=SHELL_FROZEN_NOW,
    )
    seed_shell_store(store, shell_catalog_entries(corpus), corpus=corpus)
    responder = workbench_remote_reads.AslDetailResponder(
        plan_roots=corpus.plan_roots,
        state_dir=corpus.state_dir,
        registry_path=corpus.registry_path,
        include_default_dirs=False,
    )
    provider = RemoteProvider(
        store,
        clock=lambda: SHELL_FROZEN_NOW,
        connector_id=SHELL_CONNECTOR_ID,
        reads=ReplayingDetailLane(responder=responder),
        mutations=RecordingSubmissionLane(),
    )
    application = workbench_http.Application(
        provider,
        workbench_http.OwnerIdentityAuthenticator(
            CredentialStore(), owner=SHELL_OWNER_IDENTITY
        ),
        policy=workbench_http.REMOTE_POLICY,
        static=workbench_http.shell_bundle(),
    )
    server = ShellFixtureServer((host, port), application)
    thread = threading.Thread(
        target=server.serve_forever, name="workbench-shell-remote", daemon=True
    )
    thread.start()
    return server, thread, store


# -- Both at once -----------------------------------------------------------


@dataclass
class ShellDeployment:
    """One running deployment: where to reach it, and how to be let in."""

    mode: str
    server: Any
    thread: threading.Thread
    headers: dict[str, str]

    @property
    def base_url(self) -> str:
        return f"http://{self.server.bound_host}:{self.server.bound_port}"

    def url(self, path: str) -> str:
        return self.base_url + path

    def shutdown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


@dataclass
class ShellDeployments:
    """Both deployments, over one corpus.

    The Python suite and the Playwright harness both start from here, so a
    scenario that passes in one runner is not passing because that runner
    seeded something the other did not.
    """

    corpus: ShellCorpus
    local: ShellDeployment
    remote: ShellDeployment
    store: WorkbenchStore

    @property
    def both(self) -> tuple[ShellDeployment, ShellDeployment]:
        return (self.local, self.remote)

    def shutdown(self) -> None:
        for deployment in self.both:
            deployment.shutdown()
        self.store.close()


def start_shell_deployments(
    root: Path,
    *,
    host: str = "127.0.0.1",
    token: str = "shell-fixture-token",
    local_port: int = 0,
    remote_port: int = 0,
) -> ShellDeployments:
    """Seed one plan corpus and serve it through both providers at once.

    The local server is given a token and the remote one an identity header,
    because that is the difference between the deployments: the laptop holds a
    secret, and the Pi trusts what Tailscale Serve stamped. Everything above
    the front door is the same application over the same plans.

    Ports default to ``0`` so the Python suite never collides with a port some
    other process took. The browser harness passes real ones instead, because
    Playwright has to write the URLs into its config before anything binds.
    """
    corpus = seed_shell_plans(root)
    local_server, local_thread = start_local_shell_server(
        corpus, host=host, port=local_port, token=token
    )
    remote_server, remote_thread, store = start_remote_shell_server(
        corpus, host=host, port=remote_port
    )
    remote = ShellDeployment(
        mode="remote",
        server=remote_server,
        thread=remote_thread,
        headers={
            IDENTITY_HEADER: SHELL_OWNER_IDENTITY,
            "X-Workbench-Request": "1",
        },
    )
    # The remote policy checks the Origin against the Host, which is only known
    # once the socket is bound. A browser sends this for free on a same-origin
    # mutation; a scripted client has to say it, and saying the true one is
    # what keeps the check meaningful.
    remote.headers["Origin"] = remote.base_url
    return ShellDeployments(
        corpus=corpus,
        local=ShellDeployment(
            mode="local",
            server=local_server,
            thread=local_thread,
            headers={"Authorization": f"Bearer {token}"},
        ),
        remote=remote,
        store=store,
    )
