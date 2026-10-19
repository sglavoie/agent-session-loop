# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Connector lifecycle and CLI assembly for the Workbench connector."""

from __future__ import annotations

from . import workbench_connector_http as _http
from . import workbench_connector_observation as _observation
from . import workbench_connector_projection as _projection

for _module in (_projection, _observation, _http):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )
del _http, _module, _observation, _projection


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def default_connector_id(hostname: str | None = None) -> str:
    """A connector id derived from this machine's name.

    The id is the receiver's primary key for everything this Mac reports, so it
    has to be stable across restarts -- which rules out anything random -- and
    it has to be a transport identifier, which rules out most hostnames as
    typed. A name that survives neither becomes ``mac``, and the operator names
    it properly with ``--connector-id``.
    """
    raw = hostname if hostname is not None else socket.gethostname()
    cleaned = _UNSAFE_ID_CHARS.sub("-", raw or "").strip("-")
    while cleaned and not cleaned[0].isalnum():
        cleaned = cleaned[1:]
    return cleaned[:128] or "mac"


def install_shutdown_handlers(stop: threading.Event) -> Callable[[], None]:
    """Make SIGTERM and SIGINT set ``stop``, and return the undo.

    A signal sets an event rather than raising, so a frame mid-flight finishes
    and the loop exits between steps. Restoring the previous handlers is what
    lets a test install these without leaving the interpreter changed.
    """
    previous: dict[int, Any] = {}

    def request_stop(*_: object) -> None:
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, request_stop)
        except ValueError, OSError:
            continue

    def restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except ValueError, OSError:
                continue

    return restore


def publish_forever(
    connector: ObservationConnector,
    *,
    stop: threading.Event,
    clock: Callable[[], float] = time.time,
    interval: float = LOOP_INTERVAL_SECONDS,
) -> None:
    """Step the connector until ``stop`` is set, then shut it down.

    One step per wake, and the wake is short relative to the heartbeat
    interval: what decides when a frame goes out is the connector's schedule,
    and what decides how fast a shutdown is noticed is this interval. Keeping
    them separate is why a slow observation pass cannot delay either.

    Recovery runs once, before the first step, and from inside the same
    ``try`` as the loop so that a connector is shut down even if it never got
    as far as stepping. Any operation a previous connector journalled and did
    not finish is settled and reported here -- never re-attempted -- so the
    service learns the outcome of a command it stopped hearing about instead of
    waiting on a process that is gone.
    """
    try:
        connector.recover(clock())
        while not stop.is_set():
            connector.step(clock())
            stop.wait(interval)
    finally:
        connector.stop()


def build_connector(
    args: Any,
    *,
    channel: portable_protocols.Channel | None = None,
    source: portable_protocols.ObservationSource | None = None,
    responder: portable_protocols.DetailResponder | None = None,
    performer: portable_protocols.MutationPerformer | None = None,
    body_source: portable_protocols.MutationBodySource | None = None,
) -> ObservationConnector:
    """Assemble the connector this invocation asked for.

    Raises :class:`ConnectorSetupError` for anything the operator can fix, so
    the caller has one exception type to turn into one refusal payload.

    Observation and detail are wired by default; acting is not. A performer is
    built only when ``--allow-remote-actions`` was passed, and the connector
    derives its announced capabilities from what it was given -- so the default
    invocation announces itself observation-only and the service refuses a
    mutating frame from it by construction rather than by configuration.
    """
    target_id = getattr(args, "target_id", None)
    if not target_id:
        raise ConnectorSetupError("a connector needs --target-id")
    connector_id = getattr(args, "connector_id", None) or default_connector_id()
    allow_actions = bool(getattr(args, "allow_remote_actions", False))
    resolved_channel = channel
    resolved_body_source = body_source
    # One token read for both routes. The body route carries the same bearer
    # as the frame route because it is the same trust relationship; what makes
    # a redemption single-use is the operation's action token, not this one.
    if resolved_channel is None or (allow_actions and resolved_body_source is None):
        origin = getattr(args, "origin", None)
        token_file = getattr(args, "token_file", None)
        if not origin:
            raise ConnectorSetupError("a connector needs --origin")
        if not token_file:
            raise ConnectorSetupError("a connector needs --token-file")
        token = read_connector_token(token_file)
        if resolved_channel is None:
            resolved_channel = HttpsChannel(origin=origin, token=token)
        if allow_actions and resolved_body_source is None:
            resolved_body_source = HttpsBodySource(origin=origin, token=token)
    state_dir = getattr(args, "state_dir", None)
    plan_roots = tuple(getattr(args, "plan_root", None) or ())
    resolved_source = source
    if resolved_source is None:
        resolved_source = AslObservationSource(
            state_dir=state_dir,
            plan_roots=plan_roots,
        )
    resolved_responder = responder
    if resolved_responder is None:
        # Wired by default, and scoped to exactly the corpus the source
        # publishes. A connector that observes a set of plans and cannot answer
        # a question about one of them is a cockpit that lists a run and then
        # says nothing about it.
        resolved_responder = workbench_remote_reads.AslDetailResponder(
            state_dir=state_dir,
            plan_roots=plan_roots,
            registry_path=getattr(args, "registry_path", None),
        )
    resolved_performer = performer
    if resolved_performer is None and allow_actions:
        # Opt-in, and scoped to the same corpus as the reads. A performer that
        # could reach a plan the connector never publishes would let a remote
        # act on something it was never shown.
        resolved_performer = workbench_remote_actions.AslMutationPerformer(
            state_dir=state_dir,
            plan_roots=plan_roots,
            registry_path=getattr(args, "registry_path", None),
        )
    heartbeat = float(
        getattr(args, "heartbeat_interval", None)
        or portable_support.DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    )
    observation = float(
        getattr(args, "observation_interval", None)
        or portable_support.DEFAULT_OBSERVATION_INTERVAL_SECONDS
    )
    try:
        return ObservationConnector(
            connector_id=connector_id,
            target_id=target_id,
            source=resolved_source,
            channel=resolved_channel,
            heartbeat_interval=heartbeat,
            observation_interval=observation,
            backoff=Backoff(),
            asl_version=_asl_version(),
            responder=resolved_responder,
            performer=resolved_performer,
            body_source=resolved_body_source,
        )
    except ConnectorError as error:
        raise ConnectorSetupError(str(error)) from error


def _asl_version() -> str | None:
    """This ASL's version, if it is spellable as a transport identifier.

    A version string is developer-chosen and the transport's identifier rule is
    narrow, so a release named in a way the wire cannot carry publishes no
    version rather than refusing every frame.
    """
    from . import __version__

    return __version__ if _IDENTIFIER.fullmatch(__version__) else None


def connector_lock_path(state_dir: str | Path | None = None) -> Path:
    """Where the single-instance pid file lives for a state root."""
    return state.state_root(state_dir) / portable_support.LOCK_FILE_NAME


def workbench_connect_from_args(
    args: Any,
    *,
    channel: portable_protocols.Channel | None = None,
    source: portable_protocols.ObservationSource | None = None,
    publish: Callable[[ObservationConnector, threading.Event], None] | None = None,
    install_signal_handlers: Callable[[threading.Event], Callable[[], None]]
    | None = None,
) -> int:
    """Run ``asl workbench connect`` from parsed CLI arguments.

    The keyword hooks mirror ``workbench_serve_from_args``: a test supplies a
    channel, a source, a publication loop, and a signal installer, and exercises
    the whole command without a socket, a corpus, or a real signal.

    The lock is taken before anything is built and released in ``finally``, so
    a start that fails for any reason leaves no pid file for the next one to
    have to steal.
    """
    import sys

    publish_impl = publish or (lambda conn, stop: publish_forever(conn, stop=stop))
    install_handlers = install_signal_handlers or install_shutdown_handlers
    as_json = bool(getattr(args, "json", False))

    def refuse(reason: str, error: str) -> int:
        if as_json:
            responses.print_json(
                responses.error_payload(
                    COMMAND_NAME, reason=reason, error=error, paused=False
                )
            )
        else:
            print(f"agent-session-loop workbench connect: {error}", file=sys.stderr)
        return 1

    lock = SingleInstanceLock(connector_lock_path(getattr(args, "state_dir", None)))
    try:
        lock.acquire()
    except ConnectorError as error:
        return refuse("connector-locked", str(error))
    try:
        try:
            connector = build_connector(args, channel=channel, source=source)
        except ConnectorError as error:
            return refuse("setup-failed", str(error))

        if as_json:
            responses.print_json(
                responses.success_payload(
                    COMMAND_NAME,
                    reason="connecting",
                    connector_id=connector.connector_id,
                    target_id=connector.target_id,
                    # The instance's own list, not the module default: a
                    # connector built without a responder offers less, and the
                    # line an operator reads has to say what was actually
                    # announced.
                    capabilities=list(connector.capabilities),
                    lock=str(lock.path),
                )
            )
        else:
            print(
                f"Workbench connector {connector.connector_id} "
                f"publishing to {connector.target_id}"
            )

        stop = threading.Event()
        restore_signals = install_handlers(stop)
        try:
            publish_impl(connector, stop)
        except KeyboardInterrupt:
            # Mirrors ``workbench serve``: SIGINT during the loop is a clean
            # exit, not an error.
            pass
        finally:
            try:
                restore_signals()
            finally:
                connector.stop()
    finally:
        lock.release()
    return 0


__all__ = [
    "ACCEPTED_STATUSES",
    "ACTIVE_PRIMARY_STATES",
    "COMMAND_NAME",
    "INGEST_PATH",
    "REMOTE_ACTIONS",
    "REQUEST_TIMEOUT_SECONDS",
    "AslObservationSource",
    "ConnectorSetupError",
    "HttpsChannel",
    "build_connector",
    "connector_lock_path",
    "default_connector_id",
    "install_shutdown_handlers",
    "project_run",
    "publish_forever",
    "read_connector_token",
    "workbench_connect_from_args",
]
