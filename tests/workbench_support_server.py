"""Focused portion of tests/workbench_support.py."""

from asl_workbench.server import ApplicationServer

from .workbench_support_store import *


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


class ShellFixtureServer(ApplicationServer):
    """A loopback transport for any :class:`Application`, for fixtures only.

    ``WorkbenchHTTPServer`` builds a ``LocalProvider`` in its constructor, so
    it cannot carry the remote one. Everything below the application is
    borrowed from the real server and handler rather than written again --
    this class supplies the application and nothing else.
    """

    def __init__(
        self,
        address: tuple[str, int],
        application: workbench_http.Application,
    ) -> None:
        super().__init__(address, application, handler=_ShellFixtureHandler)


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


__all__ = ["argparse", "http", "json", "threading", "time", "Callable", "Mapping", "dataclass", "field", "UTC", "datetime", "sha256", "Path", "Any", "cli", "events", "plans_files", "state", "workbench_archive", "workbench_history", "workbench_registry_models", "workbench_remote_reads", "workbench_server", "catalog", "observation", "workbench_http", "IDENTITY_HEADER", "CredentialStore", "ObservationPolicy", "ReadAnswer", "RemoteProvider", "WorkbenchStore", "STATIC_ROUTE_KEYS", "CLI_WORKBENCH_COMMAND", "CLI_PLANS_COMMAND", "SurfaceReadError", "_subparser_choices", "workbench_cli_entry_points", "workbench_api_routes", "RecordingWorkbenchServer", "recording_server_factory", "WorkbenchAPIFixture", "SHELL_OWNER_IDENTITY", "SHELL_CONNECTOR_ID", "SHELL_FROZEN_NOW", "SHELL_CONTRACT_SET_ID", "SHELL_PLAN_NAMES", "SHELL_RUN_IDS", "SHELL_QUIET_SECONDS", "SHELL_HISTORY_RUNS", "SHELL_ARCHIVE_ENTRIES", "SHELL_ACTION_TOKEN", "SHELL_PERMITTED_ACTIONS", "ShellCorpus", "shell_iso", "_seed_shell_runner_state", "_seed_shell_history_events", "_seed_shell_archive", "seed_shell_plans", "start_local_shell_server", "_shell_envelope", "shell_catalog_entries", "seed_shell_history", "seed_shell_store", "ReplayingDetailLane", "RecordingSubmissionLane", "ShellFixtureServer", "_ShellFixtureHandler", "start_remote_shell_server", "ShellDeployment", "ShellDeployments", "start_shell_deployments"]  # fmt: skip
