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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent_session_loop import (
    cli,
    events,
    plans,
    state,
    workbench,
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
SHELL_PLAN_NAMES: tuple[str, ...] = ("alpha", "bravo")

#: ``alpha`` is paused waiting for an operator, and ``bravo`` has never run.
#: Two states rather than one because the shell's two hardest jobs are telling
#: them apart in the queue and offering only the actions the run actually
#: permits -- neither of which a corpus of identical plans can exercise.
SHELL_RUN_IDS: Mapping[str, str | None] = {
    "alpha": "20260918T080000Z-alpha",
    "bravo": None,
}

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


def _seed_shell_runner_state(
    plan_dir: Path, state_dir: Path, run_id: str | None
) -> None:
    """Give a plan the runner state its published state claims it has.

    Written as a real ``state.json`` under the real per-plan state directory,
    so the local mode *derives* "paused for a reply" from the same file ASL
    would have written, rather than being told. A plan with no run id is left
    with no state at all, which is how a plan that has never run looks.
    """
    if run_id is None:
        return
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    plan_state.mkdir(parents=True, exist_ok=True)
    (plan_state / events.STATE_FILE_NAME).write_text(
        json.dumps(
            {
                "plan_dir": str(plan_dir),
                "run_id": run_id,
                "session_id": "2",
                "paused": True,
                "updated_at": SHELL_FROZEN_NOW - 60.0,
            },
            sort_keys=True,
        ),
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
        statuses = ("complete", "pending") if index == 0 else ("pending", "pending")
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
        _seed_shell_runner_state(canonical, state_dir, SHELL_RUN_IDS.get(name))
        written.append(canonical)
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
        paused = SHELL_RUN_IDS.get(name) is not None
        entries.append(
            {
                "plan_key": plan_key,
                "run_key": run_keys[name],
                "title": f"{name.title()} step 1",
                "display_label": name.title(),
                # What the Mac derived from this plan's state, repeated. The
                # local mode derives the same words from the same file, so the
                # two queues describe one plan rather than agreeing by luck.
                "primary_state": "paused-for-reply" if paused else "ready",
                "recommended_action": "reply" if paused else "run",
                "urgency_rank": index,
                "urgency_group": "needs-action" if paused else "ready",
                "activity_label": "Reply needed" if paused else "Ready to run",
                "projects": ["scripts"],
                "project_group": "scripts",
                "counts": {"sessions": 2},
                "sources": ["manifest"],
                "exists": True,
                "pinned": False,
                "repository_count": 1,
                "warning_count": 0,
                "search_text": f"{name} step 1".casefold(),
                "registry_digest": "9d2e59249fe21a3c",
            }
        )
    return entries


def seed_shell_store(store: WorkbenchStore, entries: list[dict[str, Any]]) -> None:
    """Give the Pi something to have observed: a heartbeat and one catalog."""
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
        run: dict[str, Any] = {
            "run_key": entry["run_key"],
            "project": "scripts",
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
            # is carried anywhere.
            "actions": list(SHELL_PERMITTED_ACTIONS) if run_id else [],
            "last_activity_at": SHELL_FROZEN_NOW - 30.0,
        }
        if run_id:
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
        target: dict[str, Any] = {"plan_key": kwargs["plan_key"]}
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
    seed_shell_store(store, shell_catalog_entries(corpus))
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
