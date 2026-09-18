"""The Mac provider behind ``asl workbench connect``.

:mod:`asl_workbench.connector` decides *when* a connector publishes and *what*
a frame may contain; this module is the half that knows what ASL is. It holds
three things:

* :class:`AslObservationSource` -- one pass over the corpus that produces the
  catalog generation, the active run rows, and the history window *together*,
  from one discovery. Nothing here spawns a process.
* :func:`project_run` -- the bridge's snapshot row translated into the closed
  run shape the transport declares.
* :class:`HttpsChannel` -- one bounded POST per frame to the Pi service's
  ``/v1/ingest``, mirroring the Companion's own ``PiClient``.

**No bridge subprocess.** The Companion's connector launched ``asl bridge`` and
turned its frames into observations, which meant a second ASL process, a second
discovery pass, and a projection that could only carry what the bridge's frame
format already carried. This calls
:func:`agent_session_loop.workbench_catalog.scan` and
:func:`agent_session_loop.workbench_history.project_history` in process
instead. One walk of the corpus now answers all three views, the catalog is
uncapped where the bridge's snapshot was capped at 200 rows, and the frames are
built from the shared services rather than re-derived from a child's output.

**Observation changes nothing.** Every read on this path is one the catalog
scan already promised: ``build_workbench_summary(reclaim=False)`` for
classification, :func:`agent_session_loop.state.inspect_lock_info` for locks,
and a history import that persists its checkpoint only when the connector says
the batch was delivered. No archive retention runs, no stale lock is reclaimed,
and no plan is repaired as a side effect of being described.

**No prose leaves.** :func:`agent_session_loop.bridge.project_row` already
refuses to copy a harness's words into a row, and :func:`project_run` narrows
further: it keeps a question's *id*, time and count and drops its body, and it
drops ``plan_dir`` and ``workspace_root`` entirely. A plan's identity on the
wire is its key -- a digest -- so a remote service can hold a full catalog
without ever learning the layout of this machine's disk.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import stat as stat_bits
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from asl_workbench import connector as portable_connector
from asl_workbench import history as portable_history
from asl_workbench.catalog import CatalogEntry
from asl_workbench.connector import (
    CATALOG_PAGE_LIMIT,
    MAX_PUBLISHED_RUNS,
    Backoff,
    ChannelError,
    ConnectorError,
    ObservationConnector,
    ObservationCycle,
    SingleInstanceLock,
)
from asl_workbench.operations import (
    METHOD_ABORT,
    METHOD_QUESTION_REPLY,
    METHOD_REDIRECT,
    METHOD_REPLY,
    METHOD_STOP_AFTER_CURRENT,
)
from asl_workbench.transport import MAX_FRAME_BYTES

from . import (
    bridge,
    responses,
    state,
    workbench_catalog,
    workbench_history,
    workbench_remote_reads,
)

COMMAND_NAME = "workbench connect"

#: The Pi service route that accepts an upward frame. One route for all five
#: envelope kinds: the kind is inside the envelope, and a service that routed
#: by URL would have two places to keep the vocabulary.
INGEST_PATH = "/v1/ingest"

#: How long one request may take. Short on purpose -- a connector that blocks
#: for a minute on a dead link stops heartbeating, which is the failure this
#: whole design exists to avoid. Matches the Companion's ``PiClient``.
REQUEST_TIMEOUT_SECONDS = 2.0

#: Statuses that mean the frame was taken. ``202`` is the service accepting a
#: frame it will apply behind its own transaction.
ACCEPTED_STATUSES: tuple[int, ...] = (200, 202)

#: How often the publication loop wakes. Fine-grained relative to the
#: heartbeat interval so that a shutdown signal is noticed promptly.
LOOP_INTERVAL_SECONDS = 0.2

#: States a run row is published for. A catalog carries every plan; a snapshot
#: carries the ones an operator might act on. Borrowed from the bridge rather
#: than restated so the two projections cannot disagree about what "active"
#: means.
ACTIVE_PRIMARY_STATES: tuple[str, ...] = bridge.ACTIVE_PRIMARY_STATES

#: Live work, which reserves snapshot space ahead of freshest-blocker order.
_LIVE_PRIMARY_STATES = frozenset({"running", "verifying"})

#: What the Pi may ask for, keyed by what ASL calls it locally.
#:
#: ``intervention``'s vocabulary is about a button on the Mac; ``operations``'
#: is about a mutation an admission gate can refuse. They are deliberately not
#: the same list, and two local actions have no remote form at all:
#: ``question.resolve`` has no operation method, and ``workbench.ensure`` opens
#: a browser on this machine, which is not a thing a remote service can want.
#: Publishing an action with no method would offer the operator a control the
#: far end cannot route.
REMOTE_ACTIONS: Mapping[str, str] = {
    "question.reply": METHOD_QUESTION_REPLY,
    "run.resume": METHOD_REPLY,
    "run.stop_after_current": METHOD_STOP_AFTER_CURRENT,
    "run.abort": METHOD_ABORT,
    "run.redirect": METHOD_REDIRECT,
}

#: The transport's identifier rule, and its complement. Restated here because
#: this module has to *produce* identifiers -- a connector id from a hostname,
#: a version from a release string -- and a producer that cannot state the rule
#: can only find out it broke it by being refused.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")


class ConnectorSetupError(ConnectorError):
    """The connector could not be built from what the operator supplied."""


# --------------------------------------------------------------------------
# Run rows
# --------------------------------------------------------------------------


def project_run(
    row: Mapping[str, Any],
    *,
    entry: CatalogEntry,
) -> dict[str, Any] | None:
    """Translate one bridge snapshot row into the transport's run shape.

    Returns ``None`` rather than a partial row when a required field has no
    value, which is the Companion's ``observation.project_row`` rule: a row
    missing its run id or its activity time would either be refused on arrival
    or, worse, accepted with a fabricated timestamp. The caller counts the
    refusal in ``failed_projections``, and the snapshot stops claiming to be
    whole.

    ``entry`` supplies the three identity fields the row has no way to know:
    the plan key, the run key, and the project name. They come from the catalog
    record built in the same scan, so a run and its catalog entry can never
    disagree about which plan they describe.

    Four of the bridge row's fields are dropped and not translated:
    ``plan_dir`` and ``workspace_root`` are this machine's layout, ``label`` is
    an operator-typed pin name, and ``question`` carries a body. The remote
    service gets keys and vocabulary words, and nothing it could not have
    inferred from them.
    """
    run_id = row.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    activity = portable_history.epoch(row.get("last_activity_at"))
    if activity is None:
        activity = portable_history.epoch(row.get("run_started_at"))
    if activity is None:
        return None
    question = row.get("question")
    question = question if isinstance(question, Mapping) else None
    published: dict[str, Any] = {
        "run_key": entry.run_key,
        "plan_key": entry.plan_key,
        "project": entry.project_group,
        "run_id": run_id,
        "primary_state": row.get("primary_state"),
        "phase": row.get("phase"),
        "urgency_group": row.get("urgency_group"),
        "actions": _remote_actions(row.get("actions")),
        "last_activity_at": activity,
        "action_token": row.get("action_token"),
        "session_id": portable_connector.session_id_word(row.get("session_id")),
        "harness": row.get("harness"),
        "run_started_at": portable_history.epoch(row.get("run_started_at")),
        "run_finished_at": portable_history.epoch(row.get("run_finished_at")),
        "pause_reason": portable_connector.pause_reason_word(row.get("pause_reason")),
        "warnings": list(row.get("warnings") or ()),
    }
    if question is not None:
        published["question_id"] = question.get("question_id")
        published["question_asked_at"] = portable_history.epoch(
            question.get("asked_at")
        )
        published["unanswered_count"] = question.get("unanswered_count")
    return published


def _remote_actions(actions: object) -> list[str]:
    """The published subset of a row's actions, in the transport's vocabulary."""
    if not isinstance(actions, Sequence) or isinstance(actions, str):
        return []
    return [
        REMOTE_ACTIONS[action]
        for action in actions
        if isinstance(action, str) and action in REMOTE_ACTIONS
    ]


def _selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Reserve bounded snapshot space for live work, then freshest blockers.

    The bridge's ``_snapshot_selection_key`` in intent, restated here because
    it decides which rows survive a cap and that decision has to be visible in
    the module that applies the cap.
    """
    activity = portable_history.epoch(
        row.get("last_activity_at")
    ) or portable_history.epoch(row.get("run_started_at"))
    return (
        row.get("primary_state") not in _LIVE_PRIMARY_STATES,
        activity is None,
        -(activity or 0.0),
        row.get("urgency_rank") or 0,
        str(row.get("plan_key") or ""),
    )


def _display_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Order the surviving rows the way the Workbench queue orders its view."""
    activity = portable_history.epoch(row.get("last_activity_at"))
    return (
        row.get("urgency_rank") or 0,
        activity is None,
        -(activity or 0.0),
        str(row.get("plan_key") or ""),
    )


# --------------------------------------------------------------------------
# The source
# --------------------------------------------------------------------------


class AslObservationSource:
    """One observation pass over this Mac's ASL corpus.

    The whole point of the class is that :meth:`observe` is *one* pass.
    :func:`agent_session_loop.workbench_catalog.scan` returns the portable
    catalog and the Mac-side summaries it was built from, in the same order, so
    the catalog generation and the run rows are cut from a single discovery.
    Scheduling them separately is how a dashboard ends up showing a run the
    catalog says does not exist.

    :meth:`commit` writes the history checkpoint and nothing else, and it is
    the connector -- not this class -- that decides when it has earned the
    right to be called.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        plan_roots: Iterable[str | Path] = (),
        include_default_dirs: bool = True,
        checkpoints: workbench_history.CheckpointStore | None = None,
        retention_seconds: float = workbench_history.DEFAULT_RETENTION_SECONDS,
        page_limit: int = CATALOG_PAGE_LIMIT,
    ) -> None:
        self.state_dir = state_dir
        self.plan_roots = tuple(plan_roots)
        self.include_default_dirs = include_default_dirs
        self.retention_seconds = float(retention_seconds)
        self.page_limit = max(1, int(page_limit))
        self._checkpoints = (
            checkpoints
            if checkpoints is not None
            else workbench_history.default_store(state_dir)
        )

    @property
    def checkpoints(self) -> workbench_history.CheckpointStore:
        """Where this source's history progress is recorded."""
        return self._checkpoints

    def observe(self, now: float) -> ObservationCycle:
        """Read the corpus once and assemble everything the connector publishes."""
        scan = workbench_catalog.scan(
            state_dir=self.state_dir,
            plan_roots=self.plan_roots,
            include_default_dirs=self.include_default_dirs,
        )
        rows, failed, truncated = self._runs(scan)
        projection = workbench_history.project_history(
            now=now,
            store=self._checkpoints,
            state_dir=self.state_dir,
            retention_seconds=self.retention_seconds,
        )
        return ObservationCycle(
            observed_at=now,
            runs=tuple(rows),
            catalog_pages=scan.catalog.pages(limit=self.page_limit),
            history_batches=projection.batches,
            history=projection.result,
            # The snapshot names the catalog generation its rows were cut from,
            # which is what lets a receiver line the two views up without
            # trusting that they arrived from one read.
            source_digest=scan.catalog.generation,
            truncated=truncated,
            failed_projections=failed,
            warnings=tuple(scan.catalog.warnings),
            degraded_reason=self._degraded(scan, projection),
        )

    def commit(self, cycle: ObservationCycle) -> None:
        """Persist the history checkpoint a fully delivered cycle earned."""
        result = cycle.history
        if isinstance(result, portable_history.ImportResult):
            workbench_history.commit(self._checkpoints, result)

    @staticmethod
    def _degraded(
        scan: workbench_catalog.CatalogScan,
        projection: workbench_history.HistoryProjection,
    ) -> str | None:
        """Why this pass saw less than the whole corpus, if it did.

        Reported on the heartbeat, so an operator watching a Mac that is up but
        half-blind sees the difference between "nothing is happening" and
        "nothing can be read". The two frames already carry the machine-
        readable form -- ``complete`` on the page and on the batch -- and this
        is the summary a human reads.
        """
        faults: list[str] = []
        if not scan.complete:
            faults.append(f"catalog incomplete ({len(scan.projection_warnings)} plans)")
        if not projection.result.complete:
            faults.append("history window incomplete")
        return "; ".join(faults) if faults else None

    def _runs(
        self, scan: workbench_catalog.CatalogScan
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """The publishable run rows, how many were lost, and whether capped."""
        projected: list[dict[str, Any]] = []
        failed = 0
        for summary, record in zip(scan.summaries, scan.catalog.records, strict=True):
            if summary.primary_state not in ACTIVE_PRIMARY_STATES:
                continue
            try:
                row = bridge.project_row(summary, state_dir=self.state_dir)
            except OSError, ValueError:
                # The plan was classified a moment ago and is unreadable now.
                # One lost row must not cost the operator the other rows, and
                # the count is what stops the snapshot claiming to be whole.
                failed += 1
                continue
            published = project_run(row, entry=record.entry)
            if published is None:
                failed += 1
                continue
            published["urgency_rank"] = row.get("urgency_rank")
            projected.append(published)
        projected.sort(key=_selection_key)
        truncated = len(projected) > MAX_PUBLISHED_RUNS
        kept = projected[:MAX_PUBLISHED_RUNS]
        kept.sort(key=_display_key)
        # ``urgency_rank`` exists to order this list and is not a field the run
        # shape declares, so it comes off before the rows are handed on.
        for row in kept:
            row.pop("urgency_rank", None)
        return kept, failed, truncated


# --------------------------------------------------------------------------
# The channel
# --------------------------------------------------------------------------


def read_connector_token(path: str | Path) -> str:
    """The connector's bearer token, from a file only this user may read.

    The same three refusals the Companion's ``runtime.private`` makes, for the
    same reason: a token readable by the group is a token the operator did not
    choose to share, a symlink is a way to make this read something else, and a
    file owned by somebody else is not this connector's secret to use.
    """
    target = Path(path)
    try:
        info = target.lstat()
    except OSError as error:
        raise ConnectorSetupError(
            f"could not read the connector token: {error}"
        ) from error
    if not stat_bits.S_ISREG(info.st_mode):
        raise ConnectorSetupError("the connector token file is not a regular file")
    if info.st_uid != os.getuid():
        raise ConnectorSetupError("the connector token file belongs to another user")
    if info.st_mode & 0o077:
        raise ConnectorSetupError(
            "the connector token file is readable beyond its owner"
        )
    try:
        document = json.loads(target.read_bytes())
    except OSError as error:
        raise ConnectorSetupError(
            f"could not read the connector token: {error}"
        ) from error
    except ValueError as error:
        raise ConnectorSetupError(
            f"the connector token file is not JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ConnectorSetupError("the connector token file is not an object")
    token = document.get("connector_token")
    if not isinstance(token, str) or not token.strip():
        raise ConnectorSetupError("the connector token file carries no connector_token")
    if not token.isprintable() or not token.isascii():
        raise ConnectorSetupError("the connector token is not printable ASCII")
    return token


class HttpsChannel:
    """One bounded POST per frame, and nothing clever.

    Byte-for-byte the Companion's ``PiClient`` in the ways that matter: no
    redirects, no proxy environment, no response spooled to disk, and no
    automatic retry. Retry is the connector's decision, not the transport's --
    a POST replayed by a layer that cannot see the queue is how one frame
    arrives twice.

    Every failure becomes :class:`asl_workbench.connector.ChannelError`, which
    is the only exception the runtime knows how to classify as "the link, not
    the code".
    """

    def __init__(
        self,
        *,
        origin: str,
        token: str,
        path: str = INGEST_PATH,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        connect: Callable[[], http.client.HTTPConnection] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ConnectorSetupError(
                "the connector origin must be an https URL naming a host"
            )
        self.origin = origin
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.path = path
        self.timeout = float(timeout)
        self._token = token
        self._connect = connect or self._default_connect

    def _default_connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPSConnection(
            self.host,
            self.port,
            timeout=self.timeout,
            context=ssl.create_default_context(),
        )

    def send(self, frame: bytes) -> bytes | None:
        """Deliver one encoded envelope and return any frame carried back.

        The response body is the downward half of the link. A service with a
        correlated read to ask puts a ``request`` frame in the acknowledgement
        of this upload; anything else -- an empty body, a status line, a JSON
        ``{"ok": true}`` -- comes back as itself and the connector refuses it as
        undecodable without treating the delivery as failed. The frame landed;
        that is what this method's success means.
        """
        try:
            connection = self._connect()
        except (OSError, ValueError) as error:
            raise ChannelError(f"could not reach {self.origin}: {error}") from error
        try:
            connection.request(
                "POST",
                self.path,
                body=frame,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(frame)),
                },
            )
            response = connection.getresponse()
            body = response.read(MAX_FRAME_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise ChannelError(f"delivery to {self.origin} failed: {error}") from error
        finally:
            with contextlib.suppress(OSError, http.client.HTTPException):
                connection.close()
        if response.status not in ACCEPTED_STATUSES:
            raise ChannelError(
                f"{self.origin} refused the frame with status {response.status}"
            )
        if len(body) > MAX_FRAME_BYTES:
            raise ChannelError(f"{self.origin} answered with an oversized body")
        return body or None


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
    """
    try:
        while not stop.is_set():
            connector.step(clock())
            stop.wait(interval)
    finally:
        connector.stop()


def build_connector(
    args: Any,
    *,
    channel: portable_connector.Channel | None = None,
    source: portable_connector.ObservationSource | None = None,
    responder: portable_connector.DetailResponder | None = None,
) -> ObservationConnector:
    """Assemble the connector this invocation asked for.

    Raises :class:`ConnectorSetupError` for anything the operator can fix, so
    the caller has one exception type to turn into one refusal payload.
    """
    target_id = getattr(args, "target_id", None)
    if not target_id:
        raise ConnectorSetupError("a connector needs --target-id")
    connector_id = getattr(args, "connector_id", None) or default_connector_id()
    resolved_channel = channel
    if resolved_channel is None:
        origin = getattr(args, "origin", None)
        token_file = getattr(args, "token_file", None)
        if not origin:
            raise ConnectorSetupError("a connector needs --origin")
        if not token_file:
            raise ConnectorSetupError("a connector needs --token-file")
        resolved_channel = HttpsChannel(
            origin=origin,
            token=read_connector_token(token_file),
        )
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
    heartbeat = float(
        getattr(args, "heartbeat_interval", None)
        or portable_connector.DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    )
    observation = float(
        getattr(args, "observation_interval", None)
        or portable_connector.DEFAULT_OBSERVATION_INTERVAL_SECONDS
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
    return state.state_root(state_dir) / portable_connector.LOCK_FILE_NAME


def workbench_connect_from_args(
    args: Any,
    *,
    channel: portable_connector.Channel | None = None,
    source: portable_connector.ObservationSource | None = None,
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
