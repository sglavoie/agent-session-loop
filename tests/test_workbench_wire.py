"""The one hop the rest of the suite supplies for itself: the actual link.

``tests/test_workbench_fullstack.py`` assembles the whole deployment except the
network, and says so -- its ``Link`` "is the only piece written for this
module". That seam is exactly where the release pinned for the deployed staging
rehearsal turned out to be hollow: every lane had coverage, the store was
filled, the browser was served, and nothing in the package carried a frame over
a socket. The rehearsal found it on the real hosts, which is the most expensive
place to find it.

So this module owns the hop and nothing else. One Mac-side connector with its
real channel, one Pi-side service on a real
:class:`~asl_workbench.server.ApplicationServer`, and a bearer token the Pi
actually issued. A request the operator makes on the Pi goes down inside the
response body of the connector's next upload, and the answer comes back up as
the next one -- over TCP, through the real route table, past the real
authenticator.

Three things follow from that and shape everything below:

**The clock is real.** Everywhere else in the suite the clock is an argument,
because a test should assert about a decision rather than race one. Here the
two ends are two threads talking over a socket and the wait is the behaviour,
so time passes on its own and every wait is bounded by a deadline that fails
the test rather than hanging the suite.

**The corpus is sealed.** ``include_default_dirs=False`` everywhere: this
module runs on a developer's machine and on the Mac that is also the deployed
connector, and a responder that fell back to the default state directory would
read somebody's actual plans.

**TLS is the deployment's, not this module's.** The channel is built on its
real origin and handed a plain-HTTP connector factory, so the bytes, the
headers, the statuses and the body are the shipped code's and only the
certificate ceremony is skipped. What terminates TLS in the deployment is
Tailscale Serve, which is not this package's to test.
"""

from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    intervention,
    state,
    workbench_connector,
    workbench_remote_actions,
    workbench_remote_reads,
)
from asl_workbench import connector as portable
from asl_workbench import http as workbench_http
from asl_workbench import observation, operations
from asl_workbench.credentials import (
    IDENTITY_HEADER,
    ROLE_CONNECTOR,
    CredentialStore,
    issue,
)
from asl_workbench.server import ApplicationServer
from asl_workbench.service import RemoteProvider
from asl_workbench.store import WorkbenchStore
from asl_workbench.uplink import Uplink
from tests.workbench_support import (
    SHELL_OWNER_IDENTITY,
    seed_shell_plans,
    shell_iso,
)

#: The two ends, named the way the transport requires. The target id is an
#: authority because that is what
#: :attr:`~asl_workbench.integrations.IntegrationSite.target_id` derives.
CONNECTOR_ID = "mac-studio"
TARGET_ID = "pi.example.ts.net:9443"

#: The cadence the connector uploads at here. Tighter than the shipped 15s
#: default for the reason the full-stack harness tightens it too: the upload is
#: the carrier a downward request rides on, so at the default every assertion
#: in this file would be an assertion about waiting. The deployment tightens to
#: one second by itself whenever a service is asking, so this is a cadence the
#: real link runs at rather than one invented here.
UPLOAD_INTERVAL_SECONDS = 0.5

#: How long a bounded wait for something to cross the link is given. Long
#: enough to be several upload intervals on a loaded machine, short enough that
#: a regression is a failure rather than a hung suite.
DEADLINE_SECONDS = 20.0

#: The slice a bounded wait sleeps between checks.
SLICE_SECONDS = 0.05

#: What it looks like for the Mac to have answered for a command. Any of the
#: three is a round trip: ``rejected`` is ASL declining to stop a run that is
#: not in a state to be stopped, which is an outcome that crossed the link and
#: not a link that failed.
SETTLED_STATUSES = frozenset(
    {
        operations.OPERATION_ACCEPTED,
        operations.OPERATION_CONSUMED,
        operations.OPERATION_REJECTED,
    }
)

#: Wide enough that nothing here has to age a fixture, tight enough to stay
#: the policy an actual deployment runs under.
POLICY = observation.ObservationPolicy(
    observation_stale_seconds=60.0,
    lost_observation_alert_seconds=120.0,
    unanswered_question_alert_seconds=120.0,
    summary_retention_days=7.0,
)


class WireCase(unittest.TestCase):
    """One connector, one service, one socket between them.

    Built in the deployment's order, and started in it too: the service is
    listening before the connector's first upload, because a connector whose
    first frame is refused backs off and the first assertion would be about the
    backoff.
    """

    #: Whether the Mac is allowed to act. Off by default, which is what an
    #: installed connector announces unless its prefix says otherwise.
    mutations = False

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-wire-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.corpus = seed_shell_plans(self.root / "mac")
        self.promote_live_run("alpha")

        pi = self.root / "pi"
        pi.mkdir()
        self.store = WorkbenchStore.open(
            pi / "workbench.db", policy=POLICY, created_at=time.time()
        )
        self.addCleanup(self.store.close)

        # The credential the Pi issued, and the token it handed out once. This
        # is the pairing: the connector's role is what the *service* recorded,
        # never what an upload claims.
        credential, self.token = issue(
            ROLE_CONNECTOR, created_at=time.time(), subject=CONNECTOR_ID
        )
        self.uplink = Uplink(
            self.store,
            clock=time.time,
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            **self.uplink_options(),
        )
        self.application = workbench_http.Application(
            RemoteProvider(
                self.store,
                clock=time.time,
                connector_id=CONNECTOR_ID,
                reads=self.uplink,
                mutations=self.uplink if self.mutations else None,
                uploads=self.uplink,
            ),
            workbench_http.OwnerIdentityAuthenticator(
                CredentialStore([credential]), owner=SHELL_OWNER_IDENTITY
            ),
            policy=workbench_http.REMOTE_POLICY,
            static=workbench_http.shell_bundle(),
        )
        self.server = ApplicationServer(("127.0.0.1", 0), self.application)
        self.serving = threading.Thread(
            target=self.server.serve_forever, name="wire-pi", daemon=True
        )
        self.serving.start()
        self.addCleanup(self.stop_server)

        self.connector = portable.ObservationConnector(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            source=workbench_connector.AslObservationSource(
                state_dir=self.corpus.state_dir,
                plan_roots=self.corpus.plan_roots,
                include_default_dirs=False,
            ),
            channel=self.channel(),
            heartbeat_interval=UPLOAD_INTERVAL_SECONDS,
            responder=workbench_remote_reads.AslDetailResponder(
                state_dir=self.corpus.state_dir,
                plan_roots=self.corpus.plan_roots,
                registry_path=self.corpus.registry_path,
                include_default_dirs=False,
            ),
            performer=(
                workbench_remote_actions.AslMutationPerformer(
                    state_dir=self.corpus.state_dir,
                    plan_roots=self.corpus.plan_roots,
                    registry_path=self.corpus.registry_path,
                    include_default_dirs=False,
                )
                if self.mutations
                else None
            ),
            body_source=None,
        )
        self.stop = threading.Event()
        self.publishing = threading.Thread(
            target=workbench_connector.publish_forever,
            args=(self.connector,),
            kwargs={"stop": self.stop, "interval": SLICE_SECONDS},
            name="wire-mac",
            daemon=True,
        )
        self.addCleanup(self.stop_connector)

    # -- the pieces a subclass varies --------------------------------------

    def uplink_options(self) -> dict[str, Any]:
        """Anything a subclass needs the uplink built differently for."""
        return {}

    def channel(self) -> workbench_connector.HttpsChannel:
        """The shipped channel, pointed at the loopback service.

        The origin is the one the deployment publishes and stays an ``https``
        URL, because the channel refuses anything else and that refusal is
        worth keeping. Only the connection is substituted, which is the one
        thing TLS would have changed about it.
        """
        host, port = self.server.bound_host, self.server.bound_port
        return workbench_connector.HttpsChannel(
            origin=f"https://{TARGET_ID}",
            token=self.token,
            connect=lambda: http.client.HTTPConnection(host, port, timeout=10.0),
        )

    # -- the corpus --------------------------------------------------------

    def promote_live_run(self, name: str) -> None:
        """Give one seeded plan the timings a started run would have.

        ``seed_shell_plans`` writes a paused state with a run id but neither
        ``run_started_at`` nor ``last_activity_at``, and the connector refuses
        to publish a row with neither rather than inventing a timestamp -- so
        the corpus as seeded carries a catalog and no runs at all. The run row
        is what a control is addressed to, so a control needs this.
        """
        path = state.plan_state_dir(self.corpus.plan_dir(name), self.corpus.state_dir)
        path /= events.STATE_FILE_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["run_started_at"] = shell_iso(time.time() - 3600.0)
        payload["last_activity_at"] = shell_iso(time.time() - 120.0)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    # -- lifecycle ---------------------------------------------------------

    def stop_server(self) -> None:
        self.server.shutdown()
        self.serving.join(timeout=10.0)
        self.server.server_close()

    def stop_connector(self) -> None:
        self.stop.set()
        self.publishing.join(timeout=10.0)
        self.assertFalse(self.publishing.is_alive(), "the connector never stopped")

    # -- driving -----------------------------------------------------------

    def start_uploading(self) -> None:
        """Start the Mac and wait until the Pi calls the link live.

        Live rather than merely present. A connector row appears on the first
        frame that lands, which is before any observation has: waiting on the
        row would let a read start against a link the service itself would
        still describe as ``lost``, and then the flake would be in the
        precondition rather than in the behaviour.
        """
        self.publishing.start()
        self.until(self.live, "the link never came up")

    def status_row(self) -> Any:
        """The Pi's own row for this connector, or ``None``."""
        rows = self.store.connector_statuses(now=time.time())
        return next((row for row in rows if row.connector_id == CONNECTOR_ID), None)

    def live(self) -> bool:
        row = self.status_row()
        return row is not None and row.freshness.label == "live"

    def until(self, ready: Any, message: str) -> None:
        """Wait for a condition, bounded, failing rather than hanging."""
        deadline = time.time() + DEADLINE_SECONDS
        while time.time() < deadline:
            if ready():
                return
            time.sleep(SLICE_SECONDS)
        self.fail(message)

    # -- the front door ----------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.server.bound_host}:{self.server.bound_port}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        """One owner round trip against the running service, decoded."""
        payload = None if body is None else json.dumps(body).encode("utf-8")
        sent = {
            IDENTITY_HEADER: SHELL_OWNER_IDENTITY,
            "X-Workbench-Request": "1",
            "Origin": self.base_url,
        }
        if payload is not None:
            sent["Content-Type"] = "application/json"
        if headers is not None:
            sent.update(headers)
        request = urllib.request.Request(
            self.base_url + path, data=payload, method=method, headers=sent
        )
        try:
            with urllib.request.urlopen(request, timeout=DEADLINE_SECONDS) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:  # a refusal is an answer here
            with error:  # closed explicitly: an error is a response too
                raw = error.read()
                status = error.code
        return status, json.loads(raw) if raw else None

    def upload(self, frame: bytes, *, token: str | None = None) -> tuple[int, bytes]:
        """One raw POST to the uplink, the way a connector makes it."""
        connection = http.client.HTTPConnection(
            self.server.bound_host, self.server.bound_port, timeout=10.0
        )
        try:
            connection.request(
                "POST",
                workbench_http.UPLINK_PATH,
                body=frame,
                headers={
                    "Authorization": f"Bearer {self.token if token is None else token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(frame)),
                },
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()


class LinkTests(WireCase):
    """What arrives, and what the Pi then knows it has."""

    def test_an_upload_over_http_is_what_brings_the_link_up(self) -> None:
        """What the Pi derived, from frames that arrived over a socket.

        Reaching ``live`` at all is asserted by the wait in
        :meth:`start_uploading`, which fails rather than returns. What is left
        to state here is that both halves of that verdict are real: a
        heartbeat landed on the route *and* an observation pass crossed, which
        are separately sourced and separately able to be missing.
        """
        self.start_uploading()
        row = self.status_row()
        assert row is not None
        self.assertTrue(row.freshness.transport_fresh)
        self.assertTrue(row.freshness.projection_fresh)
        self.assertTrue(row.compatible)
        self.assertIsNotNone(row.last_heartbeat_at)
        self.assertIsNotNone(row.last_snapshot_at)

    def test_the_corpus_the_mac_publishes_is_the_corpus_the_pi_serves(self) -> None:
        self.start_uploading()
        expected = set(self.corpus.plan_keys.values())
        self.until(
            lambda: expected <= self.served_plan_keys(),
            "the catalog never crossed the link",
        )

    def served_plan_keys(self) -> set[str]:
        """Every plan key the owner's queue names, or nothing yet."""
        status, body = self.request("/api/queue")
        if status != 200 or not isinstance(body, dict):
            return set()
        return {
            str(plan.get("plan_key"))
            for plan in body.get("plans") or ()
            if isinstance(plan, dict)
        }

    def test_a_stranger_cannot_upload_and_a_frame_is_not_how_you_pair(
        self,
    ) -> None:
        """The bearer is the pairing, and the route is not a way around it.

        Worth stating over a real socket rather than against the provider,
        because the refusal has to happen in front of the lane: an upload that
        reached :meth:`Uplink.ingest` at all would have been an unpaired
        machine writing into the store's observation tables.

        The answer is the one an unknown path gets, not a 403, because the
        remote policy conceals a route the caller may not have -- on a surface
        reachable from a tailnet, confirming that an ingest path exists is
        itself worth withholding.
        """
        self.start_uploading()
        status, _ = self.upload(b'{"kind": "observation"}', token="x" * 43)
        self.assertEqual(404, status)

    def test_a_body_that_is_not_a_frame_is_refused_and_the_link_survives(
        self,
    ) -> None:
        self.start_uploading()
        row = self.status_row()
        assert row is not None
        before = row.last_heartbeat_at
        status, _ = self.upload(b"not json")
        self.assertEqual(400, status)
        # One bad frame is a refused request, not a broken link: the next real
        # heartbeat still moves the row the service keeps for this connector.
        self.until(
            lambda: (self.status_row().last_heartbeat_at or 0) > (before or 0),
            "no upload landed after a malformed one was refused",
        )


class ReadTests(WireCase):
    """A question asked on the Pi, answered from the Mac's own disk."""

    def test_a_detail_read_rides_down_on_an_upload_and_the_answer_rides_up(
        self,
    ) -> None:
        """The whole point of the module, in one round trip.

        ``charlie`` is the seeded plan with a real ``events.jsonl``. The Pi has
        never read that file and has no column to keep it in, so entries in
        this response cannot have come from anywhere but the Mac -- carried
        down as a ``request`` in the body of an upload's acknowledgement and
        back up as a ``detail`` on the next one.
        """
        self.start_uploading()
        status, body = self.request(
            f"/api/events?plan_key={self.corpus.plan_keys['charlie']}"
        )
        self.assertEqual(200, status, body)
        self.assertEqual("events", body["detail"])
        self.assertTrue(body.get("entries"))

    def test_a_read_for_a_plan_the_mac_never_published_is_refused(self) -> None:
        self.start_uploading()
        status, body = self.request("/api/progress?plan_key=" + "0" * 16)
        self.assertNotEqual(200, status)
        self.assertIn("reason", body)


class SilenceTests(WireCase):
    """A link nobody is holding open has to be noticed without a request."""

    def uplink_options(self) -> dict[str, Any]:
        # The deployment's three minutes, compressed. The number is a
        # constructor argument precisely so a test does not have to sit
        # through the production one to prove the rule.
        return {"silence_seconds": 1.0, "read_wait_seconds": 1.0}

    def test_a_connector_that_stops_uploading_goes_offline_on_a_sweep(self) -> None:
        self.start_uploading()
        self.stop.set()
        self.publishing.join(timeout=10.0)
        time.sleep(1.5)
        # What the runtime's poll does, on the schedule it does it: nothing
        # else in a deployed process runs on a timer.
        self.uplink.sweep(time.time())
        self.assertFalse(self.uplink.linked)
        status, body = self.request(
            f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}"
        )
        self.assertNotEqual(200, status)
        self.assertIn("reason", body)


class ControlTests(WireCase):
    """A command submitted on the Pi, carried out on the Mac."""

    mutations = True

    def test_a_control_crosses_the_link_and_its_outcome_comes_back(self) -> None:
        """Submitted over HTTP, dispatched on an upload, settled by ASL.

        The action token is minted on the Mac, because the Mac is the only end
        that can issue one: the retained schema keeps identifiers and has no
        column for a freshness proof. It travels up with the submission and is
        checked by ASL against the run it was issued for, which is the
        direction this exercises.
        """
        self.start_uploading()
        facts = intervention.inspect_run(
            self.corpus.plan_dir("alpha"), state_dir=self.corpus.state_dir
        )
        status, body = self.request(
            "/api/control/stop-after-current",
            method="POST",
            body={
                "operation_id": "op-wire-1",
                "plan_key": self.corpus.plan_keys["alpha"],
                "run_key": self.corpus.run_keys["alpha"],
                "bridge_token": intervention.action_token(facts),
            },
        )
        self.assertEqual(202, status, body)
        self.until(self.settled, "the command never left or never came back")
        record = self.store.operation("op-wire-1")
        assert record is not None
        self.assertIn(record["status"], SETTLED_STATUSES, record)

    def settled(self) -> bool:
        """Whether the Mac has reported an outcome, not merely received one.

        ``dispatched`` is deliberately not in the set. It is the status the Pi
        writes when it hands the command to an acknowledgement, so a wait that
        accepted it would be satisfied by the Pi's own bookkeeping and would
        assert nothing about the Mac at all -- which is the entire round trip
        this test exists for.
        """
        record = self.store.operation("op-wire-1")
        return bool(record) and record["status"] in SETTLED_STATUSES


if __name__ == "__main__":  # pragma: no cover - the runner's entry point
    unittest.main()
