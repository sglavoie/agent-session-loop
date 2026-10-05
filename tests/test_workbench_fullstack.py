"""The whole application at once: Mac corpus, connector, link, Pi, HTTP.

Every other Workbench module tests one layer against doubles standing in for
its neighbours, which is the right shape for a unit and the wrong shape for the
only question left in this consolidation: *does the assembled thing work*. A
connector that agrees with a fake channel, a store that agrees with a
hand-written envelope, and a provider that agrees with a stub lane can all pass
while the assembly of the three carries nothing.

So nothing here is stubbed that the deployment has. The corpus is a real ASL
plan tree. The observations come from
:class:`agent_session_loop.workbench_connector.AslObservationSource` and are
published by the real :class:`asl_workbench.connector.ObservationConnector`. The
:class:`Link` between them is the only piece written for this module, and it is
written as a *network* rather than as a double: it validates every frame through
:func:`asl_workbench.transport.validate_frame` and applies it through the store's
own ``ingest_*`` methods, so a frame this module carries is a frame the Pi would
have accepted. Above the store sit the real :class:`RemoteProvider`, the real
:class:`~asl_workbench.remote_reads.DetailReadClient`, the real
:class:`~agent_session_loop.workbench_remote_reads.AslDetailResponder`, the real
:class:`~agent_session_loop.workbench_remote_actions.AslMutationPerformer`, and
the real HTTP :class:`~asl_workbench.http.Application` on a loopback socket.

What that assembly makes it possible to ask, grouped the way the classes are:

* **the published corpus is the corpus.** :class:`PublishedStackTests` reads
  plans out of the Pi's HTTP surface and compares them against the plan tree on
  the Mac's disk -- not against the frames the connector built, which would only
  prove the connector agrees with itself.
* **the link is allowed to fail.** :class:`LinkFailureTests` takes the network
  away mid-cycle, watches the Pi's own freshness go stale, brings it back, and
  repeats the round trip ten times. One row per plan, one entry per catalog
  generation, bounded growth, and a heartbeat that recovers on its own.
* **a read crosses two processes.** :class:`CorrelatedReadTests` asks the Pi for
  an artifact it has never seen, carries the request down the link inside an
  acknowledgement, answers it on the Mac from real files, and carries the answer
  back up. Then it loses the answer and proves the read settles on its deadline
  rather than hanging.
* **a mutation is settled once.** :class:`MutationBoundaryTests` submits over
  HTTP, carries the command down, performs it against real ASL, and reports the
  settlement back into the Pi's journal. Then it drops the report at the
  dispatch boundary and proves a restarted connector closes the row out as
  unconfirmed instead of running it a second time.
* **identity and version are checked, not assumed.**
  :class:`IdentityAndCompatibilityTests` points a frame at the wrong Pi, offers
  a transport version nobody speaks, and redeems a token that has gone stale.
* **storage can fail.** :class:`StorageFailureTests` makes the Pi's database
  unwritable underneath a running service and asks what the operator sees.
* **input can be malformed.** :class:`MalformedInputTests` sends frames too
  large, numbers that are not finite, and bodies that are not JSON, and requires
  the next good request to still be answered.
* **nothing private is on the Pi's disk.** :class:`OnDiskPrivacyTests` plants
  prose in the corpus, runs the whole stack, takes a real backup, writes real
  logs, and then greps the database, its write-ahead log, its shared-memory
  file, the journal, the logs, and the backup for any of it.
* **the front end stores only what it declared.** :class:`BrowserStorageTests`
  reads the shipped bundle and requires every browser-storage write to name a
  key from ``constants.js``.
* **scale is bounded.** :class:`ResourceTests` publishes a thousand plans
  through the real pager, reads artifacts concurrently, fills the connector's
  queues against a dead link, and checks that the bounds hold and the row counts
  stop growing.
* **a launch spawns a real child.** :class:`InertLaunchTests` drives
  ``POST /api/launch`` through the real launcher with an inert interpreter, so
  the spawn, the session isolation, and the stdin reply path are the real ones
  while the child does nothing.
* **one behaviour has one owner.** :class:`ConsolidationTests` audits the
  capability matrix for duplicated behaviour and unowned surface.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    intervention,
    state,
    workbench,
    workbench_actions,
    workbench_archive,
    workbench_connector,
    workbench_operations,
    workbench_remote_actions,
    workbench_remote_reads,
)
from asl_workbench import (
    capabilities,
    catalog,
    integrations,
    migration,
    observation,
    operations,
    remote_reads,
    runtime,
    transport,
)
from asl_workbench import connector as portable
from asl_workbench import http as workbench_http
from asl_workbench.credentials import IDENTITY_HEADER, CredentialStore
from asl_workbench.installation import (
    DATABASE_PATH,
    LOGS_DIRECTORY,
    PRIVATE_FILE_MODE,
)
from asl_workbench.service import RemoteProvider
from asl_workbench.store import StoreError, WorkbenchStore
from tests.workbench_support import (
    SHELL_FROZEN_NOW,
    SHELL_OWNER_IDENTITY,
    ShellFixtureServer,
    seed_shell_plans,
    shell_iso,
    start_local_shell_server,
    workbench_api_routes,
    workbench_cli_entry_points,
)

#: The Mac and the Pi, named the way the transport requires.
CONNECTOR_ID = "mac-studio"
TARGET_ID = "pi-1"

#: One fixed moment. Every clock in this module is an argument, so the whole
#: stack can be advanced deliberately instead of waited on.
NOW = SHELL_FROZEN_NOW

#: Wide enough that no test here has to age a fixture by a week, and tight
#: enough that :class:`LinkFailureTests` can make a heartbeat stale in seconds.
POLICY = observation.ObservationPolicy(
    observation_stale_seconds=60.0,
    lost_observation_alert_seconds=120.0,
    unanswered_question_alert_seconds=120.0,
    summary_retention_days=7.0,
)

#: How many steps a cycle needs to drain. One frame leaves per step, and a
#: cycle over this corpus is a heartbeat, a snapshot, a catalog page and a
#: history batch, so this is several cycles' worth of headroom.
DRAIN_STEPS = 24

#: The heartbeat cadence the harness runs the connector at.
#:
#: Tighter than the shipped 15s default, because the heartbeat is also the
#: carrier a downward request rides on: at the default, every correlated read
#: here would wait most of its 20s deadline for a carrier and the module would
#: spend its time proving the arithmetic of a schedule that
#: ``tests/test_workbench_connector.py`` already covers. The deployment tightens
#: to :data:`~asl_workbench.connector.ACTIVE_READ_INTERVAL_SECONDS` -- one
#: second -- whenever a service is asking, so this is a cadence the real link
#: runs at and not one invented for the test.
HEARTBEAT_SECONDS = 2.0

#: How far the shared clock advances between two connector steps.
#:
#: Finer than the carrier cadence on purpose. Liveness outranks everything in
#: the connector's own selection order, so a step that advances the clock by a
#: full carrier interval finds a fresh heartbeat waiting every single time and
#: no answer is ever chosen -- the harness would starve the read lane by the
#: granularity of its own clock rather than by anything the deployment does. A
#: real link steps far more often than it heartbeats, and this is that ratio.
STEP_SECONDS = 0.25

#: How many steps one correlated read is given before its own deadline settles
#: it. At :data:`STEP_SECONDS` this is a handful of carrier intervals, which is
#: several chances at a carrier and still well inside the read's deadline.
READ_STEPS = 64


class Clock:
    """One moment, shared by the Mac, the link and the Pi.

    Every clock in the stack is an injected argument, so the alternative to
    sharing one is three that disagree -- and a Pi whose clock trails the Mac's
    turns every deadline in the transport into a race. Sharing it also makes the
    deadline arithmetic real: a read issued at ``now`` and answered twelve
    stepped seconds later has actually spent twelve seconds of its deadline.
    """

    def __init__(self, start: float) -> None:
        self.now = float(start)

    def tick(self, seconds: float = 1.0) -> float:
        self.now += float(seconds)
        return self.now

    def reading(self) -> float:
        return self.now


class Pumper:
    """Steps the connector once per round, for however many threads are waiting.

    A request thread that is waiting for an answer needs the connector to keep
    stepping, and several of them wait at once. Letting each take its own step
    would be wrong twice: it races inside the connector, which a deployment
    never does because its loop is one thread, and it advances the *shared*
    clock once per waiter, so six concurrent reads would burn every deadline six
    times as fast and the harness would time out reads the deployment answers.
    So a step is a round: whoever gets there first takes it, and everybody else
    waits for that round rather than taking one of their own.
    """

    def __init__(
        self,
        *,
        connector: portable.ObservationConnector,
        clock: Clock,
        step_seconds: float = 0.25,
    ) -> None:
        self.connector = connector
        self.clock = clock
        self.step_seconds = step_seconds
        self._cond = threading.Condition()
        self._stepping = False
        self._rounds = 0

    @property
    def rounds(self) -> int:
        with self._cond:
            return self._rounds

    def pump(self, seen: int) -> int:
        """Advance one round past ``seen``, and return the round now completed."""
        with self._cond:
            if self._rounds > seen:
                return self._rounds
            if self._stepping:
                self._cond.wait(timeout=5.0)
                return self._rounds
            self._stepping = True
        try:
            moment = self.clock.tick(self.step_seconds)
            # A down link is reported through the connector's state, not raised
            # at whoever happened to be stepping it.
            with contextlib.suppress(portable.ChannelError):
                self.connector.step(moment)
        finally:
            with self._cond:
                self._stepping = False
                self._rounds += 1
                self._cond.notify_all()
        return self.rounds


def inline(attempt: Any) -> None:
    """Run a connector worker on the calling thread.

    Every test here drives the clock, so a real thread would only add a join
    and a race. The isolation property those threads exist for is proven in
    ``tests/test_workbench_connector.py`` against an executor that never runs
    the pass at all; repeating it here would test the executor twice and the
    assembly never.
    """
    attempt()
    return None


# ---------------------------------------------------------------------------
# The network
# ---------------------------------------------------------------------------


class LinkDown(portable.ChannelError):
    """The link is not there. Raised as the channel contract requires."""


class Link:
    """The network between the Mac and the Pi, with faults it can be given.

    A :class:`~asl_workbench.connector.Channel`, and on the far side of it the
    Pi's real ingestion. Everything that crosses is validated by
    :func:`~asl_workbench.transport.validate_frame` in the direction it is
    travelling and then handed to the object that owns it -- the store for an
    observation, the read client for an answer, the journal for a settlement --
    so this class decides *whether* bytes arrive and never *what they mean*.

    The faults are the ones a tailnet actually has, and they are kept apart on
    purpose. ``offline`` is a link that is not there, which the connector must
    back off from. ``losing`` is a link that carried the frame and lost the
    acknowledgement, which is the case where the sender cannot tell delivery
    from loss and the receiver has already acted. Collapsing them into one flag
    is how a retry turns into a second mutation.
    """

    def __init__(
        self,
        store: WorkbenchStore,
        *,
        clock: Clock | None = None,
        target_id: str = TARGET_ID,
        reads: remote_reads.DetailReadClient | None = None,
    ) -> None:
        self.store = store
        self.clock = clock if clock is not None else Clock(NOW)
        self.target_id = target_id
        self.reads = reads
        #: Every upward envelope that landed, in order, as the Pi saw it.
        self.landed: list[dict[str, Any]] = []
        #: Every answer the read client settled, for assertions about lanes.
        self.answers: list[remote_reads.ReadAnswer] = []
        #: Frames to hand back inside the next acknowledgement.
        self.carry: deque[Mapping[str, Any]] = deque()
        self.attempts = 0
        self.offline = False
        self.losing = False
        self.refused: list[str] = []
        #: Every operation id this link actually handed down, in order.
        self.dispatched: list[str] = []
        #: Several request threads read :attr:`answers` while the stepping
        #: thread appends to it, which is exactly the shape a real service has.
        self.lock = threading.Lock()

    def take(self, request_id: str) -> remote_reads.ReadAnswer | None:
        """The settled answer to one request, removed, or ``None``."""
        with self.lock:
            for answer in self.answers:
                if answer.request_id == request_id:
                    self.answers.remove(answer)
                    return answer
        return None

    # -- faults ----------------------------------------------------------
    def down(self) -> None:
        self.offline = True

    def up(self) -> None:
        self.offline = False

    # -- the channel -----------------------------------------------------
    def send(self, frame: bytes) -> bytes | None:
        self.attempts += 1
        if self.offline:
            raise LinkDown("the link is down")
        checked = transport.validate_frame(
            frame, direction=transport.DIRECTION_UP, target_id=self.target_id
        )
        self.landed.append(checked)
        self.deliver(checked)
        if self.losing:
            # Delivered, and the sender will never know. The frame is gone
            # after the far end acted on it, which is the only ordering that
            # makes a duplicate possible.
            raise LinkDown("the acknowledgement was lost")
        if self.carry:
            handed = self.carry.popleft()
            self.dispatch(handed)
            return transport.encode_frame(handed)
        return None

    def dispatch(self, envelope: Mapping[str, Any]) -> None:
        """Mark a command dispatched at the moment it actually leaves.

        The journal's ``pending`` means "admitted and still here", so the row
        moves the instant the command is handed to an acknowledgement and not
        when it was queued: a submission that never found a carrier is one the
        operator may still cancel, and a submission that left is one whose
        outcome is now the Mac's to report. Doing it here also makes the
        dispatch boundary a real place -- a report lost after this point leaves
        a row that says dispatched and unsettled, which is the state the
        operator must never be shown as finished.
        """
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return
        if payload.get("request") != transport.REQUEST_MUTATE:
            return
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):
            return
        if self.store.operation(operation_id) is None:
            return
        self.store.advance_operation(
            operation_id,
            operations.OPERATION_DISPATCHED,
            now=self.clock.now,
            dispatched_at=self.clock.now,
        )
        self.dispatched.append(operation_id)

    # -- the far end -----------------------------------------------------
    def deliver(self, envelope: Mapping[str, Any]) -> None:
        """Apply one arrived frame the way the Pi's own service would."""
        kind = str(envelope["kind"])
        if kind == transport.KIND_OBSERVATION:
            self.ingest(envelope)
            return
        if kind == transport.KIND_OPERATION:
            self.settle(envelope)
            return
        if self.reads is not None:
            answer = self.reads.accept(envelope, now=self.clock.now)
            if answer is not None:
                with self.lock:
                    self.answers.append(answer)

    def ingest(self, envelope: Mapping[str, Any]) -> Any:
        """One observation, through the store's own path for its view."""
        view = str(envelope["payload"]["view"])
        applied = {
            transport.VIEW_HEARTBEAT: self.store.ingest_heartbeat,
            transport.VIEW_SNAPSHOT: self.store.ingest_snapshot,
            transport.VIEW_CATALOG_PAGE: self.store.ingest_catalog,
            transport.VIEW_HISTORY_BATCH: self.store.ingest_history,
        }[view]
        return applied(envelope, received_at=self.clock.now)

    def settle(self, envelope: Mapping[str, Any]) -> None:
        """One settlement, into the journal row the submission created.

        A report about an operation this Pi never admitted is dropped rather
        than inserted: the journal is the record of what *this* service handed
        down, and a row invented from an incoming frame would be a command
        nobody here can account for.
        """
        payload = envelope["payload"]
        operation_id = str(payload["operation_id"])
        if self.store.operation(operation_id) is None:
            self.refused.append(operation_id)
            return
        self.store.advance_operation(
            operation_id,
            str(payload["status"]),
            now=self.clock.now,
            detail_code=payload.get("detail_code"),
            dispatched_at=payload.get("dispatched_at"),
            settled_at=payload.get("settled_at"),
        )


class LinkedDetailLane:
    """The Pi's read lane, wired through the connector instead of past it.

    ``fetch`` is called on a request thread and must return an answer, so the
    waiting has to happen somewhere. Here it happens by pumping the connector:
    the request goes into the link's carry queue, the next acknowledgement hands
    it to the Mac, the Mac answers it inline, and the answer comes back up the
    same link into
    :meth:`~asl_workbench.remote_reads.DetailReadClient.accept`. A deployment
    spends that interval blocked on a socket; this spends it advancing a clock,
    and the path the frames take is identical.

    The pumping goes through a shared :class:`Pumper` rather than happening
    here, so that concurrent readers cooperate instead of each driving their own
    connector and their own clock.
    """

    def __init__(
        self,
        *,
        client: remote_reads.DetailReadClient,
        link: Link,
        pumper: Pumper,
        steps: int = READ_STEPS,
    ) -> None:
        self.client = client
        self.link = link
        self.pumper = pumper
        self.steps = steps
        self.lock = threading.Lock()
        self.asked = 0

    def next_id(self) -> str:
        with self.lock:
            self.asked += 1
            return f"read-{self.asked}"

    def fetch(self, **request: Any) -> remote_reads.ReadAnswer:
        request_id = self.next_id()
        now = float(request.pop("now"))
        ticket = self.client.read(request_id=request_id, now=now, **request)
        if ticket.answer is not None:
            return ticket.answer
        assert ticket.envelope is not None
        # A deque's append and popleft are each atomic, which is the whole of
        # what the carry queue needs: one thread hands a request in while
        # another takes the next one out.
        self.link.carry.append(ticket.envelope)
        self.client.sent(request_id, now=now)
        seen = self.pumper.rounds
        for _ in range(self.steps):
            seen = self.pumper.pump(seen)
            answer = self.link.take(request_id)
            if answer is not None:
                return answer
        # Nothing came back inside the window the caller was given. The client's
        # own deadline is what settles it, so the outcome is its word and not a
        # verdict invented here.
        self.client.sent(request_id, now=self.pumper.clock.now, timeout=0.001)
        settled = self.client.expire(self.pumper.clock.now + 1.0)
        for answer in settled:
            if answer.request_id == request_id:
                return answer
        answer = self.link.take(request_id)
        if answer is not None:
            return answer
        raise AssertionError(f"read {request_id} neither answered nor expired")


class LinkedSubmissionLane:
    """The Pi's mutation lane, carried down the link the connector holds.

    Deliberately *not* stepping the connector the way the read lane does. A
    submission is answered ``202`` the moment it is journalled, and the command
    leaves on a later acknowledgement -- so a lane that waited for the outcome
    would be inventing a synchronous mutation the deployment does not have, and
    would hide the very window :class:`MutationBoundaryTests` is about.
    """

    #: How long a carried command stays worth performing. The Pi's own
    #: ``deliver_by`` is the authority in the deployment; this is the same idea
    #: on the envelope, wide enough that a stepped clock cannot expire a command
    #: the test has not finished carrying.
    LIFETIME_SECONDS = 120.0

    def __init__(self, link: Link) -> None:
        self.link = link
        self.handed: list[dict[str, Any]] = []

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        self.handed.append(dict(payload))
        self.link.carry.append(
            transport.request_envelope(
                payload=payload,
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=now,
                request_id=f"submit-{len(self.handed)}",
                deadline=now + self.LIFETIME_SECONDS,
            )
        )


# ---------------------------------------------------------------------------
# The assembled stack
# ---------------------------------------------------------------------------


class FullStackCase(unittest.TestCase):
    """One corpus, one connector, one store, one HTTP service, one link.

    Built in the deployment's order -- the Mac reads, the link carries, the Pi
    retains, the service answers -- because each piece is handed the real
    previous one and there is nothing left to inject in the middle.
    """

    #: Whether to build the correlated read half. Off by default: the client
    #: holds a body cache and a deadline table, and a test that never asks for
    #: an artifact should not have to reason about either.
    reads = False
    #: Whether to build the mutation half, which is what makes the connector
    #: stop calling itself observation-only.
    mutations = False

    def setUp(self) -> None:
        self.clock = Clock(NOW)
        self.root = Path(tempfile.mkdtemp(prefix="workbench-fullstack-")).resolve()
        self.addCleanup(self.remove_tree, self.root)
        self.corpus = seed_shell_plans(self.root / "mac")
        self.promote_live_run("alpha")
        self.pi = self.root / "pi"
        self.pi.mkdir()

        self.store = WorkbenchStore.open(
            self.pi / "workbench.db", policy=POLICY, created_at=NOW
        )
        self.addCleanup(self.store.close)

        self.source = workbench_connector.AslObservationSource(
            state_dir=self.corpus.state_dir,
            plan_roots=self.corpus.plan_roots,
            include_default_dirs=False,
        )
        self.responder = workbench_remote_reads.AslDetailResponder(
            plan_roots=self.corpus.plan_roots,
            state_dir=self.corpus.state_dir,
            registry_path=self.corpus.registry_path,
            include_default_dirs=False,
        )
        self.performer: workbench_remote_actions.AslMutationPerformer | None = None
        if self.mutations:
            self.performer = workbench_remote_actions.AslMutationPerformer(
                state_dir=self.corpus.state_dir,
                plan_roots=self.corpus.plan_roots,
                include_default_dirs=False,
                registry_path=self.corpus.registry_path,
            )

        self.client: remote_reads.DetailReadClient | None = None
        if self.reads:
            self.client = remote_reads.DetailReadClient(
                connector_id=CONNECTOR_ID, target_id=TARGET_ID
            )
        self.link = Link(self.store, clock=self.clock, reads=self.client)
        self.connector = portable.ObservationConnector(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            source=self.source,
            channel=self.link,
            heartbeat_interval=HEARTBEAT_SECONDS,
            responder=self.responder,
            performer=self.performer,
            body_source=None,
            run_observation=inline,
            run_answer=inline,
            run_mutation=inline,
        )
        if self.client is not None:
            # The pairing the service would have recorded when the hello
            # landed. Taken from the connector's own capability list rather
            # than declared here, so a connector built without a performer
            # narrows what the Pi believes it may ask for.
            self.client.link_up(self.connector.capabilities, now=NOW)

        self.pumper = Pumper(
            connector=self.connector, clock=self.clock, step_seconds=STEP_SECONDS
        )
        self.lane = (
            LinkedDetailLane(client=self.client, link=self.link, pumper=self.pumper)
            if self.client is not None
            else None
        )
        self.submissions = LinkedSubmissionLane(self.link) if self.mutations else None
        self.provider = RemoteProvider(
            self.store,
            clock=self.clock.reading,
            connector_id=CONNECTOR_ID,
            reads=self.lane,
            mutations=self.submissions,
        )
        self.application = workbench_http.Application(
            self.provider,
            workbench_http.OwnerIdentityAuthenticator(
                CredentialStore(), owner=SHELL_OWNER_IDENTITY
            ),
            policy=workbench_http.REMOTE_POLICY,
            static=workbench_http.shell_bundle(),
        )
        self.server = ShellFixtureServer(("127.0.0.1", 0), self.application)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fullstack-pi", daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop_server)

    # -- the corpus ------------------------------------------------------
    def promote_live_run(self, name: str) -> Path:
        """Give one seeded plan the run timings a started run would have.

        ``seed_shell_plans`` writes a paused ``state.json`` with a run id but no
        ``run_started_at`` and no ``last_activity_at``, and
        :func:`~agent_session_loop.workbench_connector.project_run` refuses a row
        with neither rather than publishing a fabricated timestamp -- so over the
        corpus as seeded the connector publishes a catalog and *zero* run rows.
        That is correct of both of them and useless here: the run row is what
        carries the action token, and the action token is what a mutation is
        authorised by. Adding the two timings is the smallest change that makes
        the corpus contain a run that has actually started.

        Written back through the same ``state.json`` ASL writes, so the state is
        still derived rather than declared.
        """
        plan_dir = self.corpus.plan_dir(name)
        path = state.plan_state_dir(plan_dir, self.corpus.state_dir)
        path /= events.STATE_FILE_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["run_started_at"] = shell_iso(NOW - 3600.0)
        payload["last_activity_at"] = shell_iso(NOW - 120.0)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return plan_dir

    # -- lifecycle -------------------------------------------------------
    def stop_server(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @staticmethod
    def remove_tree(root: Path) -> None:
        """Delete a tree, restoring any mode a test took away first."""
        for path in sorted(root.rglob("*"), reverse=True):
            with contextlib.suppress(OSError):  # already gone is fine
                path.chmod(0o700 if path.is_dir() else 0o600)
        shutil.rmtree(root, ignore_errors=True)

    # -- driving ---------------------------------------------------------
    def publish(
        self, *, steps: int = DRAIN_STEPS, tick: float = STEP_SECONDS
    ) -> list[str]:
        """Step the connector on the shared clock, reporting what left.

        ``tick`` stays under the carrier interval for the reason
        :data:`STEP_SECONDS` records: at or above it, every step finds a fresh
        heartbeat due and liveness takes every frame, so nothing else -- no
        snapshot, no page, no settlement -- ever gets selected.
        """
        sent: list[str] = []
        for _ in range(steps):
            try:
                view = self.connector.step(self.clock.now)
            except portable.ChannelError:  # pragma: no cover - reported by state
                view = None
            if view is not None:
                sent.append(view)
            self.clock.tick(tick)
        return sent

    def idle(self, seconds: float) -> None:
        """Let time pass with nobody stepping, the way an outage does."""
        self.clock.tick(seconds)

    # -- the front door --------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.server.bound_host}:{self.server.bound_port}"

    def headers(self) -> dict[str, str]:
        return {
            IDENTITY_HEADER: SHELL_OWNER_IDENTITY,
            "X-Workbench-Request": "1",
            "Origin": self.base_url,
        }

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        raw: bytes | None = None,
        content_type: str = "application/json",
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        """One HTTP round trip against the running Pi, decoded."""
        payload = raw
        if payload is None and body is not None:
            payload = json.dumps(body).encode("utf-8")
        sent = dict(self.headers())
        if headers is not None:
            sent.update(headers)
        if payload is not None:
            sent["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path, data=payload, method=method, headers=sent
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, self.decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, self.decode(error.read())

    @staticmethod
    def decode(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            return raw

    def queue(self, query: str = "") -> Mapping[str, Any]:
        status, body = self.request("/api/queue" + query)
        assert status == 200, body
        return dict(body)

    def queue_rows(self, query: str = "") -> list[Mapping[str, Any]]:
        return list(self.queue(query)["plans"])

    def freshness(self) -> Mapping[str, Any]:
        status, body = self.request("/api/summary")
        assert status == 200, body
        return dict(body["freshness"])


# ---------------------------------------------------------------------------
# The corpus reaches the interface
# ---------------------------------------------------------------------------


class PublishedStackTests(FullStackCase):
    """What the operator's browser is shown is what is on the Mac's disk."""

    reads = True

    def test_every_seeded_plan_arrives_addressed_as_the_mac_addresses_it(self) -> None:
        self.publish()
        rows = self.queue_rows()
        served = {str(row["plan_key"]) for row in rows}
        # The comparison is against the corpus's own digests, computed by
        # `catalog.plan_key` from the directories on disk -- so a link that
        # renumbered a plan fails here rather than looking plausible.
        self.assertEqual(set(self.corpus.plan_keys.values()), served)
        self.assertEqual(len(self.corpus.plan_dirs), len(rows))

    def test_the_states_the_corpus_is_in_are_the_states_that_are_served(self) -> None:
        self.publish()
        by_key = {str(row["plan_key"]): row for row in self.queue_rows()}
        keys = self.corpus.plan_keys
        # `alpha` is paused for a reply, `bravo` has never run, `charlie`
        # finished and went quiet. Three different answers out of one publish.
        self.assertEqual("paused-for-reply", by_key[keys["alpha"]]["primary_state"])
        self.assertEqual("ready", by_key[keys["bravo"]]["primary_state"])
        self.assertIn(
            by_key[keys["charlie"]]["primary_state"], {"ready", "recent", "complete"}
        )

    def test_a_published_stack_reports_itself_fresh_and_complete(self) -> None:
        self.publish()
        freshness = self.freshness()
        self.assertFalse(freshness["stale"])
        self.assertTrue(freshness["complete"])
        self.assertEqual(NOW, freshness["observed_at"])
        self.assertEqual(len(self.corpus.plan_dirs), freshness["total"])

    def test_the_static_bundle_is_served_from_the_same_origin_as_the_api(self) -> None:
        # The front end is one deployment with the API, not a second service:
        # a shell fetched from elsewhere would be refused by the remote policy
        # it is supposed to satisfy.
        status, body = self.request("/")
        self.assertEqual(200, status)
        self.assertIn(b"<!doctype html>", bytes(body).lower())

    def test_an_artifact_the_pi_never_saw_is_read_through_the_connector(self) -> None:
        self.publish()
        status, body = self.request(
            f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}"
        )
        self.assertEqual(200, status)
        # The answer came from the Mac's disk: the request was carried down
        # inside an acknowledgement and the answer came back up as a frame.
        assert self.lane is not None
        self.assertEqual(1, self.lane.asked)
        self.assertTrue(
            any(frame["kind"] == transport.KIND_DETAIL for frame in self.link.landed),
            "no detail frame crossed the link",
        )


# ---------------------------------------------------------------------------
# The link is allowed to fail
# ---------------------------------------------------------------------------


class LinkFailureTests(FullStackCase):
    """Network loss, recovery, and the ten reconnects after it."""

    def test_a_link_that_goes_away_leaves_the_connector_backing_off(self) -> None:
        self.link.down()
        self.publish()
        self.assertEqual(portable.STATE_BACKOFF, self.connector.state)
        # Nothing was committed, so the next pass re-reads the same window
        # rather than skipping over an undelivered one.
        self.assertEqual([], self.link.landed)

    def test_an_unreachable_pi_reports_its_own_staleness_rather_than_silence(
        self,
    ) -> None:
        self.publish()
        self.assertFalse(self.freshness()["stale"])
        self.link.down()
        # Far past `observation_stale_seconds`, which is what the Pi measures
        # its own confidence against. It has no way to know the Mac is alive,
        # so "I have not heard anything" is the only honest answer -- and the
        # answer it has to volunteer rather than serve the last good one.
        self.idle(3600.0)
        statuses = self.store.connector_statuses(now=self.clock.now)
        self.assertTrue(statuses)
        self.assertTrue(all(status.degraded for status in statuses))
        self.assertTrue(self.freshness()["stale"])

    def test_a_recovered_link_announces_a_new_generation(self) -> None:
        self.publish()
        first = self.connector.generation
        self.link.down()
        self.idle(100.0)
        self.publish()
        self.link.up()
        self.idle(100.0)
        self.publish()
        self.assertEqual(portable.STATE_READY, self.connector.state)
        self.assertGreater(self.connector.generation, first)

    def test_ten_reconnects_leave_one_row_and_one_entry_per_plan(self) -> None:
        for round_index in range(10):
            self.link.down()
            self.publish()
            self.link.up()
            # A recovered link is not a recovered connector: it is in backoff
            # and will not try again until its retry falls due, which is the
            # behaviour that keeps a flapping tailnet from being hammered. Time
            # has to pass before the reconnect can happen at all.
            self.idle(portable.BACKOFF_CAP_SECONDS)
            self.publish()
            self.assertEqual(
                len(self.corpus.plan_dirs),
                len(self.queue_rows()),
                f"the catalog duplicated on reconnect {round_index}",
            )
        rows = self.store.runs(now=self.clock.now, include_absent=True)
        self.assertTrue(rows, "ten reconnects published no run rows at all")
        keys = [str(row["run_key"]) for row in rows]
        self.assertEqual(len(set(keys)), len(keys))

    def test_repeated_publishing_stops_growing_the_database(self) -> None:
        self.publish()
        counted = self.row_counts()
        for _ in range(6):
            self.publish()
        self.assertEqual(counted, self.row_counts())

    def row_counts(self) -> dict[str, int]:
        """Every table's row count, which is what unbounded growth shows up in."""
        with self.store.reading() as connection:
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                name: int(
                    connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                )
                for name in names
            }


# ---------------------------------------------------------------------------
# A read crosses two processes
# ---------------------------------------------------------------------------


class CorrelatedReadTests(FullStackCase):
    """Artifacts the Pi has never seen, and the deadline that bounds them."""

    reads = True

    def test_remote_archive_listing_is_a_read_with_no_mac_paths(self) -> None:
        self.publish()
        status, body = self.request("/api/archive")
        self.assertEqual(200, status, body)
        self.assertEqual(2, len(body["entries"]))
        self.assertEqual(1, body["archive_cleanup"]["candidate_count"])
        self.assertIn("archive_retention_days", body)
        self.assertNotIn(str(self.corpus.plan_dirs[0]), json.dumps(body))

    def test_an_answer_carries_the_macs_own_material_not_a_translation(self) -> None:
        self.publish()
        status, body = self.request(
            f"/api/events?plan_key={self.corpus.plan_keys['charlie']}"
        )
        self.assertEqual(200, status)
        # `charlie` is the plan with a real `events.jsonl`, so an answer with
        # no entries would mean the read reached nothing. The entries are the
        # Mac's own records, unaltered: the Pi has never read this file.
        self.assertTrue(body.get("entries"))
        self.assertEqual("events", body["detail"])

    def test_a_lost_answer_settles_on_the_deadline_instead_of_hanging(self) -> None:
        self.publish()
        assert self.lane is not None
        # The Mac answers and the answer never arrives. The read has to end,
        # and it has to end as a timeout rather than as an empty artifact.
        self.link.reads = None
        status, body = self.request(
            f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}"
        )
        self.assertIn(status, {200, 502, 503, 504})
        if status != 200:
            self.assertIn("reason", body)

    def test_a_read_for_a_plan_nobody_published_is_refused_not_invented(self) -> None:
        self.publish()
        status, body = self.request("/api/progress?plan_key=" + "0" * 16)
        self.assertNotEqual(200, status)
        self.assertIn("reason", body)

    def test_concurrent_reads_of_one_artifact_all_get_an_answer(self) -> None:
        self.publish()
        path = f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}"
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.request(path), range(6)))
        for status, body in results:
            self.assertEqual(200, status, body)


# ---------------------------------------------------------------------------
# A mutation is settled once
# ---------------------------------------------------------------------------


class MutationBoundaryTests(FullStackCase):
    """Submission, dispatch, settlement -- and the restart in the middle."""

    mutations = True

    def bridge_token(self) -> str:
        """The freshness proof ASL issues for alpha, minted on the Mac.

        Minted here rather than read back off the Pi because the Pi has no copy
        to read: the retained schema deliberately has no column for a freshness
        proof, so the token the Mac published in the snapshot is dropped on
        ingest and the store's ``command_target`` carries identifiers only. The
        proof reaches the operator from the machine that issued it and travels
        back up with the submission, and that is the direction this exercises --
        ASL is the only end that can say whether the token still matches the run
        it was issued against, and it is the end that checks.
        """
        facts = intervention.inspect_run(
            self.corpus.plan_dir("alpha"), state_dir=self.corpus.state_dir
        )
        return intervention.action_token(facts)

    def submit(self, **overrides: Any) -> tuple[int, Any]:
        """One ``stop-after-current`` over HTTP, journalled and handed down."""
        body: dict[str, Any] = {
            "operation_id": "op-stop-1",
            "plan_key": self.corpus.plan_keys["alpha"],
            "run_key": self.corpus.run_keys["alpha"],
            "bridge_token": self.bridge_token(),
        }
        body.update(overrides)
        return self.request("/api/control/stop-after-current", method="POST", body=body)

    def test_a_submission_is_journalled_before_it_is_handed_down(self) -> None:
        self.publish()
        status, body = self.submit()
        self.assertEqual(202, status, body)
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertEqual(operations.OPERATION_PENDING, record["status"])
        assert self.submissions is not None
        self.assertEqual(1, len(self.submissions.handed))

    def test_a_resubmitted_id_is_one_command_rather_than_two(self) -> None:
        self.publish()
        self.submit()
        status, body = self.submit()
        self.assertEqual(202, status, body)
        assert self.submissions is not None
        # Handed down once. The second call is the first row read back.
        self.assertEqual(1, len(self.submissions.handed))

    def test_the_settlement_asl_reached_arrives_back_in_the_journal(self) -> None:
        self.publish()
        self.submit()
        self.idle(60.0)
        self.publish()
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertIn(
            record["status"],
            {
                operations.OPERATION_CONSUMED,
                operations.OPERATION_ACCEPTED,
                operations.OPERATION_REJECTED,
                operations.OPERATION_EXPIRED,
            },
        )
        self.assertNotEqual(operations.OPERATION_PENDING, record["status"])

    def test_a_stale_proof_is_admitted_by_the_pi_and_refused_by_asl(self) -> None:
        # The split the two ends are built around. The Pi cannot tell a stale
        # proof from a current one -- it holds no copy -- so it admits the
        # command and journals it. ASL redeems the proof against the run as it
        # is now, refuses, and the refusal comes back as the settlement. The
        # operator is told no by the only end that could have known.
        self.publish()
        status, body = self.submit(bridge_token="stale" * 8)
        self.assertEqual(202, status, body)
        self.idle(60.0)
        self.publish()
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertEqual(operations.OPERATION_REJECTED, record["status"])

    def test_a_report_lost_at_the_dispatch_boundary_leaves_it_unsettled(self) -> None:
        self.publish()
        self.submit()
        self.link.losing = True
        self.idle(60.0)
        self.publish()
        self.link.losing = False
        record = self.store.operation("op-stop-1")
        assert record is not None
        # The Mac carried the command out and the report did not arrive, which
        # is the one state the operator must not be told is finished.
        self.assertIn(
            record["status"],
            {operations.OPERATION_PENDING, operations.OPERATION_DISPATCHED},
        )

    def test_a_restarted_connector_closes_the_row_out_without_rerunning_it(
        self,
    ) -> None:
        self.publish()
        self.submit()
        self.link.losing = True
        self.idle(60.0)
        self.publish()
        self.link.losing = False
        assert self.performer is not None
        before = self.performed()

        # A new connector over the same corpus and the same link: the process
        # the operator restarted. `recover` reads the journal the performer
        # wrote and reports, and it re-attempts nothing.
        revived = portable.ObservationConnector(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            source=self.source,
            channel=self.link,
            heartbeat_interval=HEARTBEAT_SECONDS,
            responder=self.responder,
            performer=self.performer,
            run_observation=inline,
            run_answer=inline,
            run_mutation=inline,
        )
        self.idle(120.0)
        for _ in range(DRAIN_STEPS):
            revived.step(self.clock.tick())
        self.assertEqual(before, self.performed())
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertNotEqual(operations.OPERATION_PENDING, record["status"])

    def performed(self) -> int:
        """How many journal rows the performer has written for this corpus."""
        found = 0
        for plan_dir in self.corpus.plan_dirs:
            rows = workbench_operations.read_operations(
                plan_dir, state_dir=self.corpus.state_dir
            )
            found += len(rows)
        return found


class ArchiveMutationTests(FullStackCase):
    """Plan and archive actions settle against the Mac's synthetic corpus."""

    reads = True
    mutations = True

    def settle(self, operation_id: str) -> dict[str, Any]:
        self.idle(60.0)
        self.publish()
        record = self.store.operation(operation_id)
        assert record is not None
        return record

    def test_plan_archive_moves_an_idle_plan_and_can_be_read_back(self) -> None:
        self.publish()
        key = self.corpus.plan_keys["bravo"]
        status, body = self.request(
            "/api/plan/archive",
            method="POST",
            body={
                "operation_id": "op-archive-bravo",
                "plan_key": key,
                "confirm_plan_key": key,
            },
        )
        self.assertEqual(202, status, body)
        self.assertNotIn("run_key", body["operation"])
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-archive-bravo")["status"]
        )
        self.assertFalse(self.corpus.plan_dir("bravo").exists())
        status, archive = self.request("/api/archive")
        self.assertEqual(200, status, archive)
        self.assertEqual(3, len(archive["entries"]))

    def test_remote_launch_uses_the_mac_default_profile(self) -> None:
        self.publish()
        key = self.corpus.plan_keys["bravo"]
        status, body = self.request(
            "/api/launch",
            method="POST",
            body={"operation_id": "op-launch-bravo", "plan_key": key},
        )
        self.assertEqual(202, status, body)
        fake = mock.Mock(
            status=operations.OPERATION_CONSUMED,
            operation={},
            facts=mock.Mock(run_id=None),
        )
        with mock.patch.object(
            workbench_actions, "launch_run", return_value=fake
        ) as launch:
            self.assertEqual(
                operations.OPERATION_CONSUMED,
                self.settle("op-launch-bravo")["status"],
            )
        self.assertEqual(self.corpus.plan_dir("bravo"), launch.call_args.args[0])
        self.assertIsNone(launch.call_args.kwargs["profile"])

        status, refusal = self.request(
            "/api/launch",
            method="POST",
            body={
                "operation_id": "op-launch-override",
                "plan_key": key,
                "profile": {"cwd": "/tmp"},
            },
        )
        self.assertEqual(400, status, refusal)

    def test_restore_purge_and_retention_are_receipted(self) -> None:
        self.publish()
        archive_id = "20260731T090000Z-delta"
        status, body = self.request(
            "/api/archive/restore",
            method="POST",
            body={"operation_id": "op-restore-delta", "archive_id": archive_id},
        )
        self.assertEqual(202, status, body)
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-restore-delta")["status"]
        )
        status, receipt = self.request("/api/archive?operation_id=op-restore-delta")
        self.assertEqual(200, status, receipt)
        self.assertEqual("consumed", receipt["operation"]["status"])

        archive_id = "20260916T090000Z-echo"
        status, body = self.request(
            "/api/archive/delete",
            method="POST",
            body={
                "operation_id": "op-purge-echo",
                "archive_ids": [archive_id],
                "confirm_count": 1,
            },
        )
        self.assertEqual(202, status, body)
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-purge-echo")["status"]
        )
        self.assertFalse(
            (
                workbench_archive.archive_root(self.corpus.state_dir) / archive_id
            ).exists()
        )

        status, body = self.request(
            "/api/archive/settings",
            method="POST",
            body={"operation_id": "op-retention", "archive_retention_days": 45},
        )
        self.assertEqual(202, status, body)
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-retention")["status"]
        )
        status, archive = self.request("/api/archive")
        self.assertEqual(200, status, archive)
        self.assertEqual(45, archive["archive_retention_days"])


# ---------------------------------------------------------------------------
# Identity, and the version on both ends
# ---------------------------------------------------------------------------


class IdentityAndCompatibilityTests(FullStackCase):
    """Who sent it, to whom, and whether either end can read the other."""

    #: A submission has to be reachable for a refusal about its *token* to mean
    #: anything: without a lane the service declines before it looks at one.
    mutations = True

    def test_a_frame_addressed_to_another_pi_is_refused_at_the_wire(self) -> None:
        envelope = transport.observation_envelope(
            payload=portable.heartbeat_payload(
                observed_at=NOW, bridge_alive=True, source_generation=1
            ),
            connector_id=CONNECTOR_ID,
            target_id="pi-2",
            sent_at=NOW,
        )
        with self.assertRaises(transport.TransportError):
            transport.validate_envelope(
                envelope, direction=transport.DIRECTION_UP, target_id=TARGET_ID
            )

    def test_a_transport_version_nobody_speaks_grants_nothing(self) -> None:
        self.assertIsNone(transport.negotiate_protocol([0, 99]))
        hello = portable.connector_hello(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            capabilities=self.connector.capabilities,
        )
        verdict = transport.evaluate_hello(
            dict(hello, protocol=99, protocols=[99]),
            policy=POLICY,
            target_id=TARGET_ID,
        )
        self.assertFalse(verdict.compatible)
        self.assertFalse(verdict.reads_allowed)
        self.assertFalse(verdict.mutations_allowed)
        self.assertIn(transport.ERROR_INCOMPATIBLE_PROTOCOL, verdict.reasons)

    def test_an_unknown_capability_narrows_the_pairing_without_killing_it(
        self,
    ) -> None:
        hello = portable.connector_hello(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            capabilities=(*self.connector.capabilities, "invented.capability"),
        )
        verdict = transport.evaluate_hello(hello, policy=POLICY, target_id=TARGET_ID)
        self.assertTrue(verdict.compatible)
        self.assertNotIn("invented.capability", verdict.granted)
        # Named rather than dropped, because "there is a capability here I do
        # not understand" is what tells an operator their two ends are different
        # releases -- and it must not be mistaken for a capability in force.
        self.assertIn("invented.capability", verdict.unknown)

    def test_a_reply_without_the_freshness_proof_is_refused_unsubmitted(self) -> None:
        # The Pi decides nothing about *which* token is current -- only ASL can
        # say that -- but it refuses a submission that carries no proof at all,
        # and it refuses it before the command is journalled. Acting on a live
        # run with no proof is acting on a guess about what that run is doing.
        self.publish()
        status, body = self.request(
            "/api/reply",
            method="POST",
            body={
                "operation_id": "op-reply-proofless",
                "plan_key": self.corpus.plan_keys["alpha"],
                "run_key": self.corpus.run_keys["alpha"],
                "reply_ref": "ref-1",
                "action_token": "redeem" * 4,
            },
        )
        self.assertEqual(400, status, body)
        self.assertEqual("invalid-submission", body["reason"])
        self.assertIsNone(self.store.operation("op-reply-proofless"))


# ---------------------------------------------------------------------------
# Storage can fail
# ---------------------------------------------------------------------------


class StorageFailureTests(FullStackCase):
    """What the operator is told when the Pi's own disk stops cooperating."""

    def readonly(self) -> None:
        """Make this connection's next write fail, the way a full disk does.

        ``PRAGMA query_only`` rather than a ``chmod``: the connection is already
        open with its write-ahead log already created, so taking the mode off the
        directory underneath it changes nothing SQLite needs to ask for, and the
        write would succeed while the test believed it had broken the disk. This
        makes the engine itself refuse, which is the error the Pi's service has
        to survive whatever the cause on the hardware was.
        """
        self.store.connection.execute("PRAGMA query_only = ON")
        self.addCleanup(self.store.connection.execute, "PRAGMA query_only = OFF")

    def heartbeat(self, at: float) -> Mapping[str, Any]:
        return transport.observation_envelope(
            payload=portable.heartbeat_payload(
                observed_at=at, bridge_alive=True, source_generation=2
            ),
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            sent_at=at,
        )

    def test_an_unwritable_database_refuses_ingestion_rather_than_lying(self) -> None:
        self.publish()
        self.readonly()
        with self.assertRaises((StoreError, sqlite3.Error)):
            self.store.ingest_heartbeat(
                self.heartbeat(self.clock.now), received_at=self.clock.now
            )

    def test_an_unwritable_database_still_serves_what_it_already_holds(self) -> None:
        # The retained observations are the point of retaining them: an operator
        # whose Pi cannot write must still be able to read what it last heard,
        # because that is the state they are deciding about.
        self.publish()
        rows = len(self.queue_rows())
        self.readonly()
        self.assertEqual(rows, len(self.queue_rows()))

    def test_a_service_over_a_failing_store_still_answers_its_probe(self) -> None:
        self.publish()
        with mock.patch.object(
            WorkbenchStore, "healthy", return_value=False, autospec=True
        ):
            status, body = self.request("/api/health")
        self.assertEqual(503, status)
        self.assertEqual("unavailable", body["status"])

    def test_a_read_that_raises_becomes_a_refusal_and_not_a_crash(self) -> None:
        self.publish()
        with mock.patch.object(
            WorkbenchStore,
            "runs",
            side_effect=StoreError("the database is gone"),
            autospec=True,
        ):
            status, body = self.request("/api/summary")
        self.assertIn(status, {500, 502, 503})
        self.assertIsInstance(body, dict)
        self.assertIn("reason", body)
        # The service is still there. A failing read must not take the port.
        status, _ = self.request("/api/health")
        self.assertEqual(200, status)

    def test_writability_returning_lets_the_next_observation_land(self) -> None:
        self.publish()
        landed = len(self.link.landed)
        self.store.connection.execute("PRAGMA query_only = ON")
        with self.assertRaises((StoreError, sqlite3.Error)):
            self.store.ingest_heartbeat(
                self.heartbeat(self.clock.now), received_at=self.clock.now
            )
        self.store.connection.execute("PRAGMA query_only = OFF")
        self.idle(120.0)
        self.publish()
        # Nothing had to be told the disk came back: the next observation is a
        # whole observation, so recovery is the ordinary path running again.
        self.assertGreater(len(self.link.landed), landed)
        self.assertEqual(len(self.corpus.plan_dirs), len(self.queue_rows()))
        self.assertFalse(self.freshness()["stale"])


# ---------------------------------------------------------------------------
# Input can be malformed
# ---------------------------------------------------------------------------


class MalformedInputTests(FullStackCase):
    """Every refusal here is followed by a good request, on purpose."""

    #: With no mutation lane the service declines a control POST before it reads
    #: the body, so a test about malformed bodies would be testing the lane's
    #: absence. The lane is here to make the refusal be about the input.
    mutations = True

    def test_a_frame_over_the_size_limit_is_refused_before_it_is_parsed(self) -> None:
        oversized = b"{" + b" " * (transport.MAX_FRAME_BYTES + 1) + b"}"
        with self.assertRaises(transport.TransportError):
            transport.validate_frame(
                oversized, direction=transport.DIRECTION_UP, target_id=TARGET_ID
            )

    def test_a_non_finite_number_never_becomes_a_timestamp(self) -> None:
        with self.assertRaises(transport.TransportError):
            transport.validate_frame(
                b'{"protocol":1,"kind":"observation","capability":"observation.read",'
                b'"connector_id":"mac-studio","target_id":"pi-1","sent_at":NaN,'
                b'"payload":{}}',
                direction=transport.DIRECTION_UP,
                target_id=TARGET_ID,
            )

    def test_a_body_that_is_not_json_is_refused_with_a_reason(self) -> None:
        status, body = self.request(
            "/api/control/abort", method="POST", raw=b"not json at all"
        )
        self.assertEqual(400, status)
        self.assertIn("reason", body)

    def test_an_unknown_route_is_a_refusal_rather_than_a_traceback(self) -> None:
        status, body = self.request("/api/invented")
        self.assertEqual(404, status)
        self.assertIn("reason", body)

    def test_a_service_that_refused_malformed_input_still_answers(self) -> None:
        self.publish()
        self.request("/api/control/abort", method="POST", raw=b"\x00\x01\x02")
        self.request("/api/queue?limit=-1")
        self.request("/api/plan")
        status, _ = self.request("/api/summary")
        self.assertEqual(200, status)
        self.assertEqual(len(self.corpus.plan_dirs), len(self.queue_rows()))


# ---------------------------------------------------------------------------
# Nothing private reaches the Pi's disk
# ---------------------------------------------------------------------------

#: Prose planted in the corpus before anything is published. Each string is
#: something only the Mac may hold: an operator's own words, a model's answer,
#: or the layout of a disk a remote service has no business learning.
SENTINELS: tuple[str, ...] = (
    "Should we drop the v1 column before the migration lands",
    "Here is the model's long answer about the v1 column",
    "operator private note about the kiln rollout",
)


class OnDiskPrivacyTests(FullStackCase):
    """Databases, journals, logs and backups, swept for what they may not hold."""

    reads = True

    def setUp(self) -> None:
        super().setUp()
        self.plant()

    def plant(self) -> None:
        """Put each sentinel where the stack will read it, not beside it.

        The manifest is what the catalog scans and the last message is what a
        correlated read carries, so both sentinels are on the path the material
        actually takes. A string dropped in an unread file would prove only that
        the sweep can pass.
        """
        alpha = self.corpus.plan_dir("alpha")
        (alpha / "manifest.md").write_text(
            f"# Alpha\n\n{SENTINELS[2]}\n", encoding="utf-8"
        )
        plan_state = state.plan_state_dir(alpha, self.corpus.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / "last-message.txt").write_text(
            f"{SENTINELS[0]}\n\n{SENTINELS[1]}\n", encoding="utf-8"
        )

    def prohibited(self) -> tuple[str, ...]:
        """Everything the Pi's disk may not contain, in one list.

        The paths are in here with the prose because they are the same kind of
        leak: a remote service may hold the whole catalog and must not learn
        the layout of the disk it describes.
        """
        return (
            *SENTINELS,
            str(self.corpus.plans_root),
            *(str(plan) for plan in self.corpus.plan_dirs),
        )

    def sweep(self, paths: Sequence[Path]) -> None:
        """Fail naming the file and the string, which is what a sweep is for."""
        prohibited = self.prohibited()
        searched = 0
        for path in paths:
            if not path.is_file():
                continue
            searched += 1
            raw = path.read_bytes()
            for needle in prohibited:
                self.assertNotIn(
                    needle.encode("utf-8"),
                    raw,
                    f"{path.name} holds {needle!r}",
                )
        self.assertGreater(searched, 0, "the sweep found nothing to search")

    def database_files(self) -> list[Path]:
        """The database and every sibling SQLite keeps beside it."""
        database = self.pi / "workbench.db"
        return [
            database,
            *(
                database.with_name(database.name + suffix)
                for suffix in ("-wal", "-shm", "-journal")
            ),
        ]

    def test_the_retained_database_holds_no_prose_and_no_path(self) -> None:
        self.publish()
        # An artifact read as well as a publish, because the read carries the
        # very prose the sentinels are: it must pass through and not be kept.
        self.request(f"/api/last-message?plan_key={self.corpus.plan_keys['alpha']}")
        self.request(f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}")
        self.store.connection.execute("PRAGMA wal_checkpoint(FULL)")
        self.sweep(self.database_files())

    def test_a_runtime_log_of_the_whole_session_holds_neither(self) -> None:
        self.publish()
        prefix = self.pi / "installed"
        prefix.mkdir(mode=0o700)
        logger, handler = runtime.build_logger(prefix)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(handler.close)
        # Log what the service logs: the counts and the identities, never the
        # material. If that separation ever breaks, this is where it shows.
        for row in self.queue_rows():
            logger.info("row %s state=%s", row["plan_key"], row["primary_state"])
        logger.info("freshness %s", json.dumps(dict(self.freshness())))
        handler.flush()
        self.sweep(sorted((prefix / LOGS_DIRECTORY).glob("*")))

    def test_a_real_backup_of_the_prefix_carries_nothing_prohibited(self) -> None:
        self.publish()
        prefix = self.pi / "prefix"
        integrations.provision(prefix, SHELL_OWNER_IDENTITY, CONNECTOR_ID)
        target = prefix / DATABASE_PATH
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with WorkbenchStore.open(target, policy=POLICY, created_at=NOW) as installed:
            # Everything the link carried, replayed into a store that lives
            # where a provisioned prefix puts one, so the backup is taken over
            # the real layout rather than over a database in a scratch path.
            replay = Link(installed, clock=self.clock)
            for envelope in self.link.landed:
                if envelope["kind"] == transport.KIND_OBSERVATION:
                    replay.ingest(envelope)
        target.chmod(PRIVATE_FILE_MODE)
        migration.backup(prefix, self.pi / "backups" / "one")
        self.sweep(sorted((self.pi / "backups" / "one").rglob("*")))

    def test_the_journal_a_mutation_writes_names_keys_and_not_prose(self) -> None:
        # The Pi's operation journal is a database table, so this is the same
        # sweep aimed at the row a submission creates: a `reply_ref` is a
        # handle and the prose it redeems stays on the Mac.
        self.publish()
        self.store.record_operation(
            {
                "operation_id": "op-privacy-1",
                "method": "asl.reply",
                "plan_key": self.corpus.plan_keys["alpha"],
                "run_key": self.corpus.run_keys["alpha"],
                "status": operations.OPERATION_PENDING,
                "requested_at": NOW,
                "deliver_by": NOW + 120.0,
                "command_id": "command-1",
            },
            connector_id=CONNECTOR_ID,
            received_at=NOW,
        )
        self.store.connection.execute("PRAGMA wal_checkpoint(FULL)")
        self.sweep(self.database_files())


# ---------------------------------------------------------------------------
# The front end stores only what it declared
# ---------------------------------------------------------------------------

#: Every browser-storage write in the shipped bundle, as it is written.
_STORAGE_WRITE = re.compile(
    r"(?:sessionStorage|localStorage)\.(?:setItem|removeItem)"
    r"\(\s*([A-Za-z0-9_.\[\]\"' ]+?)\s*[,)]"
)
#: The declared key constants, which are the only names a write may use.
_STORAGE_CONSTANT = re.compile(r"export const (\w*STORAGE_KEY\w*) = \"([\w.]+)\";")
#: A name a write may use that is not itself a constant: a parameter a helper
#: was handed one through. ``persistFacet(key, value)`` is the shape.
_THREADED_NAME = re.compile(r"[a-z][A-Za-z0-9]*")


class BrowserStorageTests(unittest.TestCase):
    """What the shell may leave in a browser, and under which names."""

    static: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.static = Path(workbench_http.__file__).resolve().parent / "static"

    def modules(self) -> Iterator[tuple[Path, str]]:
        for path in sorted(self.static.rglob("*.js")):
            yield path, path.read_text(encoding="utf-8")

    def declared(self) -> dict[str, str]:
        source = (self.static / "modules" / "constants.js").read_text(encoding="utf-8")
        return dict(_STORAGE_CONSTANT.findall(source))

    def test_every_storage_key_is_a_declared_constant(self) -> None:
        declared = self.declared()
        self.assertTrue(declared, "constants.js declares no storage keys")
        for path, source in self.modules():
            for name in _STORAGE_WRITE.findall(source):
                expression = name.strip()
                if expression.startswith(("'", '"')):
                    self.fail(f"{path.name} writes a literal storage key: {name}")
                root = expression.split("[")[0].split(".")[0]
                if _THREADED_NAME.fullmatch(root):
                    # A helper writing the key it was passed. The constant is at
                    # the call site, and the literal sweep below is what keeps
                    # that call site from having invented one.
                    continue
                self.assertIn(
                    root,
                    declared,
                    f"{path.name} writes storage key {expression}, "
                    "which constants.js does not declare",
                )

    def test_only_constants_js_spells_a_storage_key_out(self) -> None:
        # The one-place rule the indirection above depends on. A module that
        # spelled `"wb.something"` itself would be a key nothing declared, which
        # is a key nobody can find when an operator asks what the shell keeps.
        for path, source in self.modules():
            if path.name == "constants.js":
                continue
            self.assertNotIn('"wb.', source, f"{path.name} spells out a storage key")

    def test_every_declared_key_lives_under_one_namespace(self) -> None:
        # One prefix, so an operator clearing the shell's storage clears all of
        # it and a key added later cannot collide with another app's.
        for name, value in self.declared().items():
            self.assertTrue(value.startswith("wb."), f"{name} = {value!r}")

    def test_no_module_reaches_for_a_cookie_or_a_database(self) -> None:
        for path, source in self.modules():
            for forbidden in ("document.cookie", "indexedDB", "openDatabase"):
                self.assertNotIn(forbidden, source, f"{path.name} uses {forbidden}")


# ---------------------------------------------------------------------------
# Scale is bounded
# ---------------------------------------------------------------------------


class ResourceTests(FullStackCase):
    """A thousand plans, concurrent readers, and queues with a ceiling."""

    reads = True

    #: Past every page limit the transport declares, so paging rather than the
    #: fixture size is what the assertions are about.
    PLANS = 1000

    def large_catalog(self) -> catalog.PlanCatalog:
        records = tuple(
            catalog.record_from_summary(
                {
                    **{name: None for name in catalog.PRIVATE_FIELDS},
                    **{name: None for name in catalog.CACHE_SAFE_FIELDS},
                    "plan_dir": f"/tmp/agent-plans/plan-{index:05d}",
                    "exists": True,
                    "primary_state": "ready",
                    "recommended_action": "run",
                    "urgency_rank": 80,
                    "urgency_group": "ready",
                    "activity_label": "Ready to run",
                    "counts": {"pending": 1, "complete": 0, "empty": 0, "unknown": 0},
                    "sources": ["state"],
                    "repositories": [],
                    "repository_count": 0,
                    "warnings": [],
                }
            )
            for index in range(self.PLANS)
        )
        return catalog.build_catalog(records)

    def publish_large_catalog(self) -> catalog.PlanCatalog:
        """Page a thousand plans into the store through the real builders."""
        catalogue = self.large_catalog()
        pages = catalogue.pages(limit=transport.MAX_CATALOG_ENTRIES_PER_PAGE)
        self.assertGreater(len(pages), 1, "a single page proves nothing about paging")
        for page in pages:
            self.link.deliver(
                transport.observation_envelope(
                    payload=portable.catalog_page_payload(
                        page, observed_at=self.clock.now, source_generation=2
                    ),
                    connector_id=CONNECTOR_ID,
                    target_id=TARGET_ID,
                    sent_at=self.clock.now,
                )
            )
        return catalogue

    def test_a_thousand_plan_catalog_arrives_whole_and_is_served_in_pages(
        self,
    ) -> None:
        self.publish()
        self.publish_large_catalog()
        body = self.queue("?limit=100")
        self.assertEqual(self.PLANS, body["catalog_total"])
        self.assertEqual(self.PLANS, body["catalog_matched"])
        self.assertLessEqual(len(body["plans"]), 100)

    def test_paging_the_whole_catalog_covers_it_without_overlap(self) -> None:
        self.publish()
        self.publish_large_catalog()
        seen: list[str] = []
        offset = 0
        while offset < self.PLANS:
            rows = self.queue_rows(f"?limit=200&offset={offset}")
            if not rows:
                break
            seen.extend(str(row["plan_key"]) for row in rows)
            offset += len(rows)
        self.assertEqual(self.PLANS, len(seen))
        self.assertEqual(self.PLANS, len(set(seen)))

    def test_a_catalog_that_size_leaves_the_heartbeat_fresh(self) -> None:
        self.publish()
        self.publish_large_catalog()
        self.link.deliver(
            transport.observation_envelope(
                payload=portable.heartbeat_payload(
                    observed_at=self.clock.now,
                    bridge_alive=True,
                    source_generation=2,
                ),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=self.clock.now,
            )
        )
        statuses = self.store.connector_statuses(now=self.clock.tick(5.0))
        self.assertTrue(statuses)
        self.assertFalse(any(status.degraded for status in statuses))

    def test_concurrent_summaries_over_a_large_catalog_all_answer(self) -> None:
        self.publish()
        self.publish_large_catalog()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request("/api/summary"), range(16)))
        for status, body in results:
            self.assertEqual(200, status, body)
            self.assertEqual(self.PLANS, body["counts"]["plans"])

    def test_a_dead_link_fills_the_queues_to_their_ceiling_and_no_further(
        self,
    ) -> None:
        self.link.down()
        for _ in range(50):
            self.publish(steps=4, tick=150.0)
        # Bounded by construction, not by how long the link stays down.
        self.assertLessEqual(
            self.connector.pending_reads(), portable.MAX_PENDING_REQUESTS
        )
        self.assertLessEqual(
            self.connector.pending(),
            portable.MAX_PENDING_REQUESTS + portable.MAX_QUEUED_ANSWERS + 4,
        )

    def test_reads_past_the_pending_ceiling_are_refused_rather_than_queued(
        self,
    ) -> None:
        assert self.client is not None
        admitted = 0
        for index in range(remote_reads.MAX_PENDING_READS + 8):
            ticket = self.client.read(
                request_id=f"probe-{index}",
                detail="progress",
                plan_key=self.corpus.plan_keys["alpha"],
                now=NOW,
            )
            if ticket.outstanding:
                admitted += 1
        self.assertEqual(remote_reads.MAX_PENDING_READS, admitted)


# ---------------------------------------------------------------------------
# A launch spawns a real child
# ---------------------------------------------------------------------------

#: An interpreter that starts, drains whatever was written to it, and stops.
#: The launcher's argv is passed to it and ignored, so the spawn, the new
#: session, the environment and the stdin write are all the real ones while the
#: child does nothing to the plan.
INERT_INTERPRETER = "#!/bin/sh\nexec cat >/dev/null 2>&1\n"


class InertLaunchTests(unittest.TestCase):
    """``POST /api/launch`` against the real launcher and a real child."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-launch-")).resolve()
        self.addCleanup(FullStackCase.remove_tree, self.root)
        self.corpus = seed_shell_plans(self.root / "mac")
        self.interpreter = self.root / "inert-python"
        self.interpreter.write_text(INERT_INTERPRETER, encoding="utf-8")
        self.interpreter.chmod(0o700)
        self.token = "fullstack-launch-token"
        self.server, self.thread = start_local_shell_server(
            self.corpus, token=self.token
        )
        self.addCleanup(self.stop)

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, path: str, body: Any) -> tuple[int, Any]:
        raw = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://{self.server.bound_host}:{self.server.bound_port}{path}",
            data=raw,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def inert(self) -> Any:
        """Patch only the interpreter, leaving every other launcher decision."""
        real = workbench.launch_plan

        def launch(plan_dir: Any, **kwargs: Any) -> Any:
            kwargs["python_executable"] = str(self.interpreter)
            return real(plan_dir, **kwargs)

        return mock.patch.object(workbench, "launch_plan", launch)

    def reap(self, pid: int) -> None:
        """End the child and collect it, so no test leaves a process behind.

        Signalled rather than waited for: the inert interpreter drains its stdin
        and the launcher leaves that pipe open for the reply path, so a child
        that is behaving correctly is a child that is still running. Killing it
        is the test taking back what it started.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(250):
            try:
                found, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if found == pid:
                return
            threading.Event().wait(0.02)

    def test_a_launch_spawns_a_child_in_its_own_session(self) -> None:
        plan_dir = self.corpus.plan_dir("bravo")
        with self.inert():
            status, body = self.request("/api/launch", {"plan_dir": str(plan_dir)})
        self.assertEqual(200, status, body)
        pid = int(body["launch"]["pid"])
        self.addCleanup(self.reap, pid)
        self.assertNotEqual(os.getpid(), pid)
        # The argv the real builder produced, pointed at the inert interpreter.
        self.assertEqual(str(self.interpreter), body["launch"]["argv"][0])
        self.assertIn("agent_session_loop", body["launch"]["argv"])

    def test_the_child_is_not_in_the_servers_process_group(self) -> None:
        plan_dir = self.corpus.plan_dir("bravo")
        with self.inert():
            status, body = self.request("/api/launch", {"plan_dir": str(plan_dir)})
        self.assertEqual(200, status, body)
        pid = int(body["launch"]["pid"])
        self.addCleanup(self.reap, pid)
        # A Ctrl-C in the launcher's terminal reaches a process group. The run
        # outliving its launcher is the whole reason for `start_new_session`.
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:  # pragma: no cover - the child already exited
            self.skipTest("the inert child exited before it could be inspected")
        self.assertNotEqual(os.getpgrp(), group)

    def test_a_plan_that_does_not_exist_is_refused_before_any_spawn(self) -> None:
        with self.inert():
            status, body = self.request(
                "/api/launch", {"plan_dir": str(self.root / "nowhere")}
            )
        self.assertEqual(400, status)
        self.assertEqual(workbench.LAUNCH_REASON_INVALID_PLAN, body["reason"])

    def test_a_repeated_launch_id_replays_rather_than_spawning_again(self) -> None:
        plan_dir = self.corpus.plan_dir("bravo")
        with self.inert():
            first_status, first = self.request(
                "/api/launch", {"plan_dir": str(plan_dir), "launch_id": "once"}
            )
            second_status, second = self.request(
                "/api/launch", {"plan_dir": str(plan_dir), "launch_id": "once"}
            )
        self.assertEqual(200, first_status, first)
        self.assertEqual(200, second_status, second)
        self.addCleanup(self.reap, int(first["launch"]["pid"]))
        self.assertEqual(first["launch"]["pid"], second["launch"]["pid"])


# ---------------------------------------------------------------------------
# One behaviour, one owner
# ---------------------------------------------------------------------------


class ConsolidationTests(unittest.TestCase):
    """The audit for the duplication this consolidation is meant to end.

    Deliberately *not* re-checking what
    ``tests/test_workbench_foundations.py::CapabilityMatrixTests`` already owns
    -- that every route has a row, that every owner comes from the closed
    vocabulary, that coverage is either named or declared missing. Those are one
    behaviour with one test, and adding a second would be the thing this class
    exists to find. What is here is the other direction and the pairs: a row
    with no behaviour behind it, two rows behind one behaviour, and two
    deployments that have quietly stopped serving the same application.
    """

    rows: tuple[capabilities.Capability, ...]

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = tuple(capabilities.CAPABILITIES)

    def normalized(self, summary: str) -> str:
        return " ".join(summary.split()).casefold()

    def test_no_two_rows_describe_the_same_behaviour(self) -> None:
        # One summary on two rows is the signature of the duplication this
        # consolidation removes: the same behaviour reached two ways, each
        # maintained by somebody who believes theirs is the only one.
        seen: dict[str, str] = {}
        for row in self.rows:
            summary = self.normalized(row.summary)
            first = seen.get(summary)
            self.assertIsNone(first, f"{row.key} repeats the summary of {first}")
            seen[summary] = row.key

    def test_every_row_is_addressed_exactly_once(self) -> None:
        keys = [row.key for row in self.rows]
        self.assertEqual(sorted(set(keys)), sorted(keys))

    def test_no_row_claims_a_route_the_application_does_not_serve(self) -> None:
        # The coverage direction is checked in the foundations module; this is
        # the orphan direction. A row that outlived its route is a behaviour
        # somebody will look for, find described, and reimplement.
        served = {
            f"{capabilities.SURFACE_WORKBENCH_HTTP}:{route.method} {route.path}"
            for route in workbench_http.ROUTES
        }
        claimed = {
            row.key
            for row in self.rows
            if row.surface == capabilities.SURFACE_WORKBENCH_HTTP
        }
        self.assertEqual(set(), claimed - served)

    def test_both_deployments_serve_the_same_route_set(self) -> None:
        # The laptop and the Pi are one application in two places. The moment
        # one grows a route the other lacks, the matrix's single row for that
        # behaviour is describing two different behaviours.
        remote = {f"{route.method} {route.path}" for route in workbench_http.ROUTES}
        self.assertEqual(set(workbench_api_routes()), remote)

    def test_every_cli_entry_point_has_exactly_one_row(self) -> None:
        rows = [
            row
            for row in self.rows
            if row.surface == capabilities.SURFACE_WORKBENCH_CLI
        ]
        named = [row.key.split(":", 1)[1] for row in rows]
        self.assertEqual(sorted(set(named)), sorted(named))
        self.assertEqual(set(workbench_cli_entry_points()), set(named))

    def test_a_declared_gap_is_one_of_the_two_the_matrix_knows(self) -> None:
        # A gap is an excuse, and an excuse spelled freehand is one nobody can
        # search for or ever close. There are two, they are named constants, and
        # a row wanting a third has to add it here first.
        known = {capabilities.GAP_DEPLOYED_HOST, capabilities.GAP_WORKBENCH_BROWSER}
        for row in self.rows:
            if row.coverage_gap is None:
                continue
            with self.subTest(row.key):
                self.assertIn(row.coverage_gap, known)

    def test_a_browser_gap_still_names_coverage_somewhere(self) -> None:
        # `GAP_WORKBENCH_BROWSER` means "no spec drives this in a real browser",
        # which is not the same as "nothing tests this at all". A row that
        # declared the gap and named nothing would be behaviour with no coverage
        # of any kind, reading as merely unspecced.
        for row in self.rows:
            if row.coverage_gap != capabilities.GAP_WORKBENCH_BROWSER:
                continue
            with self.subTest(row.key):
                self.assertTrue(
                    row.regression_coverage,
                    "a browser gap excuses the spec, not the coverage",
                )


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
