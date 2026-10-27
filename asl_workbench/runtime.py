"""The installed service's entry point: one process, one lock, one socket.

This is what a service manager starts. ``python3 -m asl_workbench.runtime
--prefix PREFIX`` reads the installation prefix, opens the observation store,
serves the shared HTTP application over the shared transport, and publishes a
readiness note the installer can wait on. Nothing here is Pi-specific beyond
the site it defaults to: it is the portable package's own ``main``, which is
why the packaged artifact is a thing that runs rather than a library somebody
still has to write a server around.

Four rules, and each one exists because of a way a supervised service fails:

* **One process owns the state.** The runtime holds ``state/runtime.lock`` for
  its whole life and refuses rather than waits. A restart that overlapped its
  predecessor would have two writers on one SQLite file and two connectors'
  worth of readiness notes.
* **Readiness is a fact, not a delay.** ``state/ready.json`` is written *after*
  the socket is listening and removed when the loop ends, and it names the pid,
  the release directory it was started from, and the address it bound. An
  installer that waited a fixed number of seconds would call a crashed release
  healthy; :meth:`asl_workbench.lifecycle.Installation.wait_ready` reads this
  instead.
* **The state is checked before it is served.** ``--check`` runs the same
  validation and exits, so the installer can ask "can this release read this
  state?" of a *copy*, before the upgrade touches the real one, and can ask the
  outgoing release the same question about the same copy. A schema the previous
  release cannot read is refused by
  :func:`asl_workbench.store_migrations.migrate` rather than discovered after
  the rollback path has been closed.
* **SIGTERM is an answer.** Both native managers stop a service by sending it
  and escalate to SIGKILL if nothing happens. The handler shuts the server down
  and the process exits zero, so a stop is a stop rather than a kill.

The configuration it reads is the one :func:`asl_workbench.integrations.provision`
wrote, through the same reader the integrations use. There is no second
configuration parser and no runtime-only field: a service whose endpoint does
not match its site refuses to start for exactly the reason an integration run
against it would refuse.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import incidents, notifications
from .credentials import ROLE_CONNECTOR, Credential
from .http import (
    MODE_REMOTE,
    REMOTE_POLICY,
    Application,
    OwnerIdentityAuthenticator,
    shell_bundle,
)
from .installation import (
    CONFIG_PATH,
    DATABASE_FILE,
    LOGS_DIRECTORY,
    PRIVATE_DIRECTORY_MODE,
    READY_FILE,
    RUNTIME_LOCK_FILE,
    SECRETS_PATH,
    SHARED_MODE_BITS,
    STATE_DIRECTORY,
    InstallationError,
    lock,
    private,
    safe_prefix,
    write_json,
)
from .integrations import PI_SITE, IntegrationSite, service_endpoint, service_secrets
from .observation import ObservationPolicy
from .server import ApplicationServer, install_shutdown_handlers
from .service import RemoteProvider
from .store_types import StoreError
from .store_workbench import WorkbenchStore
from .uplink import Uplink

#: The runtime log. Event words only -- ``runtime-started``, ``runtime-stopped``
#: -- because the deployed host persists no prose, and a request line on this
#: service is a private URL plus a plan name.
LOG_FILE = "runtime.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 3
LOGGER_NAME = "asl-workbench-runtime"

#: How often the supervising loop wakes to notice a stop or a dead server.
POLL_SECONDS = 0.2

#: The incident engine owns its own timer because publishing may block for the
#: transport timeout and must never delay HTTP acceptance or uplink liveness.
#: Kept at the legacy service's cadence so the unified runtime preserves the
#: alert threshold without adding a second timing policy.
INCIDENT_POLL_SECONDS = 0.5


class RuntimeRefusal(InstallationError):
    """The installed runtime refused to start, or to keep running.

    An :class:`~asl_workbench.installation.InstallationError` because that is
    what it is: every refusal below is about the prefix this process was handed
    rather than about a request somebody made of it.
    """


def release_name() -> str:
    """The directory this module was imported from.

    In an installation that is the artifact digest, because releases are
    unpacked into ``releases/<sha256>/``. Read from ``__file__`` rather than
    from ``deployment.json`` on purpose: the point of the readiness note is to
    say which release is *running*, and a release that reported the deployment
    record's digest would report the installer's intention instead.
    """
    return Path(__file__).resolve().parents[1].name


def policy_for(observation_only: bool) -> ObservationPolicy:
    """The policy an installed service runs under.

    ``observation_only`` is the configured setting and ``mutations_permitted``
    is derived from it rather than configured beside it: two flags that could
    disagree would leave "may this deployment be acted on?" with two answers.
    """
    return ObservationPolicy(
        # The production phone procedure promises the normal two-minute alert
        # threshold.  Leaving this at ObservationPolicy's conservative
        # five-minute default consumes nearly all of a 6–7 minute locked-phone
        # readiness window before the notification is even published.
        unanswered_question_alert_seconds=120.0,
        observation_only=observation_only,
        mutations_permitted=not observation_only,
    )


def connector_subject(credentials: Sequence[Credential]) -> str:
    """The connector id this installation issued its connector credential to.

    The service keys everything it stores by connector id, so it has to be the
    one the pairing was provisioned for rather than one chosen at start-up. A
    prefix with no enabled connector credential is not "a service that has not
    paired yet" -- provisioning always issues one -- so it is refused.
    """
    for credential in credentials:
        if (
            credential.role == ROLE_CONNECTOR
            and not credential.disabled
            and credential.subject
        ):
            return credential.subject
    raise RuntimeRefusal("installation has no connector credential to serve")


def check_state(state: Path, policy: ObservationPolicy) -> None:
    """Prove *this release* can read the state in ``state``.

    Run by each release against a copy during an upgrade, in both directions,
    which is what makes an incompatible schema a refusal before the switch
    rather than a rollback that cannot read what the new release wrote.
    Opening the store migrates it;
    :func:`asl_workbench.store_migrations.migrate` refuses a database newer
    than the code, so the outgoing release fails here exactly when a downgrade
    would have been unsafe.
    """
    private(state, directory=True)
    database = state / DATABASE_FILE
    try:
        with WorkbenchStore.open(
            database, policy=policy, created_at=time.time()
        ) as store:
            if not store.healthy():
                raise RuntimeRefusal("observation store is unhealthy")
    except StoreError as error:
        raise RuntimeRefusal("observation store refused this release") from error
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeRefusal("observation store failed its integrity check")


def build_logger(prefix: Path) -> tuple[logging.Logger, logging.Handler]:
    """A bounded, private log carrying start and stop and nothing else.

    The directory is created here rather than required. An installed prefix
    already has one, but a service's own log directory is the service's to
    make, and refusing to start over a missing empty directory would turn a
    recoverable nothing into an outage.
    """
    logs = prefix / LOGS_DIRECTORY
    logs.mkdir(mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
    private(logs, directory=True)
    logger = logging.getLogger(LOGGER_NAME)
    handler = RotatingFileHandler(
        logs / LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger, handler


def run(
    prefix: Path,
    state: Path | None = None,
    *,
    check: bool = False,
    site: IntegrationSite = PI_SITE,
    serve: Callable[[ApplicationServer], None] | None = None,
) -> None:
    """Serve one installation until it is asked to stop.

    ``serve`` exists so a caller can supply the blocking loop; the default is
    the server's own. Everything else is read from the prefix, because a
    service manager starts this with two arguments and no environment worth
    trusting.
    """
    os.umask(SHARED_MODE_BITS)
    safe_prefix(prefix)
    endpoint = service_endpoint(prefix / CONFIG_PATH, site)
    credentials, publisher_token = service_secrets(prefix / SECRETS_PATH)
    connector_id = connector_subject(credentials.credentials())
    policy = policy_for(endpoint.observation_only)
    state = state or prefix / STATE_DIRECTORY
    private(state, directory=True)
    if check:
        check_state(state, policy)
        return

    with lock(state / RUNTIME_LOCK_FILE):
        ready = state / READY_FILE
        ready.unlink(missing_ok=True)
        check_state(state, policy)
        logger, handler = build_logger(prefix)
        logger.info("runtime-started")
        try:
            with WorkbenchStore.open(
                state / DATABASE_FILE,
                policy=policy,
                created_at=time.time(),
            ) as store:
                publisher = notifications.select_publisher(
                    mode=MODE_REMOTE,
                    deployment=notifications.DEPLOYMENT_PRODUCTION,
                    token=publisher_token,
                    navigation_version=release_name(),
                )
                incident_engine = incidents.IncidentEngine(
                    store=store,
                    clock=time.time,
                    publisher=publisher,
                )
                # Wall time stamps what is stored and sent; the link's silence
                # and a read's wait are durations, measured on the monotonic
                # clock so the NTP step a Pi without an RTC takes after boot
                # cannot expire them. The incident engine and the provider stay
                # on wall time: every moment they compare against is a stored
                # or connector-reported wall timestamp.
                uplink = Uplink(
                    store,
                    clock=time.time,
                    monotonic=time.monotonic,
                    connector_id=connector_id,
                    target_id=site.target_id,
                )
                server = ApplicationServer(
                    (endpoint.bind_host, endpoint.bind_port),
                    Application(
                        RemoteProvider(
                            store,
                            clock=time.time,
                            connector_id=connector_id,
                            reads=uplink,
                            mutations=uplink,
                            bodies=uplink,
                            uploads=uplink,
                        ),
                        OwnerIdentityAuthenticator(
                            credentials, owner=endpoint.owner_identity
                        ),
                        # The shell is served over TLS by something in front
                        # of this socket, so the Origin a browser sends is the
                        # site's published ``https`` one, never the ``http``
                        # loopback form the policy builds from the Host. A
                        # service that did not say so here would refuse every
                        # owner control from its own page, and refuse it as
                        # "cross-site", which names the wrong cause.
                        policy=replace(
                            REMOTE_POLICY,
                            allowed_origins=(endpoint.public_origin,),
                        ),
                        static=shell_bundle(),
                    ),
                )
                # Before the server starts: no submission can be between its
                # journal write and its hand-down yet, so every open row a
                # previous process left is one nothing will carry.
                uplink.recover(time.time())
                restore_signals = install_shutdown_handlers(server)
                worker = threading.Thread(
                    target=serve or (lambda instance: instance.serve_forever()),
                    args=(server,),
                    name="workbench-runtime",
                )
                stop_incidents = threading.Event()

                def incidents_loop() -> None:
                    while not stop_incidents.wait(INCIDENT_POLL_SECONDS):
                        try:
                            incident_engine.tick()
                        except sqlite3.Error:
                            # A transient storage failure must not permanently
                            # end observation timers. Publishing intent is
                            # durable before the network call begins.
                            continue

                incident_worker = threading.Thread(
                    target=incidents_loop,
                    name="workbench-incidents",
                )
                worker.start()
                incident_worker.start()
                try:
                    write_json(
                        ready,
                        {
                            "pid": os.getpid(),
                            "artifact": release_name(),
                            "host": server.bound_host,
                            "port": server.bound_port,
                            "started_at": time.time(),
                        },
                    )
                    # Polled rather than joined outright so a signal is handled
                    # promptly on the main thread whatever the platform does
                    # with an interrupted lock acquisition. The uplink's own
                    # deadlines are swept from the same poll: nothing else in a
                    # deployed process runs on a timer, and a link that went
                    # quiet has to be noticed without a request arriving to
                    # notice it.
                    while worker.is_alive():
                        worker.join(POLL_SECONDS)
                        # The moment stamps what the sweep settles; how long
                        # the link has been quiet is the uplink's own
                        # monotonic measurement.
                        uplink.sweep(time.time())
                finally:
                    try:
                        stop_incidents.set()
                        incident_worker.join()
                        server.shutdown()
                        worker.join()
                        server.server_close()
                    finally:
                        restore_signals()
        finally:
            ready.unlink(missing_ok=True)
            logger.info("runtime-stopped")
            handler.close()
            logger.removeHandler(handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asl_workbench.runtime",
        description="Serve one installed Workbench prefix.",
    )
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument(
        "--state",
        type=Path,
        help="a copy of the state to validate instead of the installed one",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the state for this release and exit without serving",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args.prefix, args.state, check=args.check)
    except Exception:  # noqa: BLE001 - a diagnostic here would carry state
        # Never print the exception: it can quote a configuration value, a
        # path inside the prefix, or a row the store refused.
        print(
            "workbench runtime failed; check the prefix, its permissions, "
            "and state compatibility",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = [
    "LOGGER_NAME",
    "LOG_BACKUP_COUNT",
    "LOG_FILE",
    "LOG_MAX_BYTES",
    "POLL_SECONDS",
    "RuntimeRefusal",
    "build_logger",
    "build_parser",
    "check_state",
    "connector_subject",
    "main",
    "policy_for",
    "release_name",
    "run",
]


if __name__ == "__main__":  # pragma: no cover - service manager entry point
    raise SystemExit(main())
