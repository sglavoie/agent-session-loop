"""The assembled stack as a base case: corpus, connector, link, store, HTTP."""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    state,
    workbench_connector,
    workbench_remote_actions,
    workbench_remote_reads,
)
from asl_workbench import connector_runtime as portable_runtime
from asl_workbench import connector_support as portable_support
from asl_workbench import http as workbench_http
from asl_workbench import (
    remote_reads,
)
from asl_workbench.credentials import IDENTITY_HEADER, CredentialStore
from asl_workbench.service import RemoteProvider
from asl_workbench.store_workbench import WorkbenchStore
from tests.support_workbench_fullstack import (
    CONNECTOR_ID,
    DRAIN_STEPS,
    HEARTBEAT_SECONDS,
    NOW,
    POLICY,
    STEP_SECONDS,
    TARGET_ID,
    Clock,
    Link,
    LinkedDetailLane,
    LinkedSubmissionLane,
    Pumper,
    inline,
)
from tests.workbench_support import (
    SHELL_OWNER_IDENTITY,
    ShellFixtureServer,
    seed_shell_plans,
    shell_iso,
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
        self.connector = portable_runtime.ObservationConnector(
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
            except (
                portable_support.ChannelError
            ):  # pragma: no cover - reported by state
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
