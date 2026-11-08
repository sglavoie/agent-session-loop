"""Workbench HTTP remote provider."""

from __future__ import annotations

import threading
import time
from unittest import mock

from asl_workbench.operations import (
    OPERATION_DISPATCHED,
    OPERATION_SUPERSEDED,
    OPERATION_UNCONFIRMED,
    REASON_OPERATION_UNCERTAIN,
)
from asl_workbench.remote_reads import RemoteReadError
from asl_workbench.store_types import MAX_READ_LIMIT

from .support_workbench_http_base import (
    CAPABILITY_OPERATION,
    COMMAND_REDEEM_PATH,
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    MODE_REMOTE,
    NOW,
    OPERATION_PENDING,
    OPERATIONS,
    READ_OUTCOMES,
    REASON_OPERATION_CONFLICT,
    REMOTE_POLICY,
    ROLE_CONNECTOR,
    ROLE_OWNER,
    Any,
    Application,
    FakeBodyLane,
    FakeDetailLane,
    FakeSubmissionLane,
    FakeUploadLane,
    FixedAuthenticator,
    ObservationPolicy,
    Path,
    ReadAnswer,
    Response,
    TemporaryDirectory,
    WorkbenchStore,
    build,
    document,
    json,
    unittest,
    workbench_service,
)
from .support_workbench_store import (
    catalog_entry,
    catalog_payload,
    envelope,
    heartbeat_payload,
    run_row,
    snapshot_payload,
)


class RemoteProviderTestsBase(unittest.TestCase):
    """What a Pi can answer out of what it kept, and what it admits it cannot."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = WorkbenchStore.open(
            Path(self._temporary.name) / "workbench.db",
            policy=ObservationPolicy(),
            created_at=NOW,
        )
        self.addCleanup(self.store.close)
        self.reads = FakeDetailLane(
            ReadAnswer(
                request_id="r1",
                outcome="answered",
                detail="state",
                plan_key="plan-abc",
                content="{}",
                received_at=NOW,
            )
        )
        self.mutations = FakeSubmissionLane()
        self.uploads = FakeUploadLane()

    def provider(self, **kwargs: Any) -> workbench_service.RemoteProvider:
        defaults: dict[str, Any] = {
            "reads": self.reads,
            "mutations": self.mutations,
            "uploads": self.uploads,
        }
        defaults.update(kwargs)
        return workbench_service.RemoteProvider(
            self.store, clock=lambda: NOW, connector_id="mac", **defaults
        )

    def remote_app(self, **kwargs: Any) -> Application:
        return Application(
            self.provider(**kwargs),
            FixedAuthenticator(ROLE_OWNER),
            policy=REMOTE_POLICY,
        )

    def signed(self) -> dict[str, str]:
        return {
            "Origin": "http://127.0.0.1:4321",
            "X-Workbench-Request": "1",
            "Sec-Fetch-Site": "same-origin",
        }

    def seed_catalog_past_the_read_limit(self) -> int:
        """More plans than one read can hold, the quietest sorting last."""
        entries = [
            catalog_entry(
                f"busy{index:06d}",
                primary_state="running",
                urgency_rank=10,
            )
            for index in range(MAX_READ_LIMIT)
        ]
        entries.append(
            catalog_entry(
                "zzz-quiet",
                primary_state="complete",
                recommended_action="view",
                urgency_rank=90,
                urgency_group="complete",
            )
        )
        self.store.ingest_catalog(
            envelope(
                "catalog_page",
                catalog_payload(
                    observed_at=NOW - 10,
                    generation="gen-large",
                    entries=entries,
                ),
            ),
            received_at=NOW - 10,
        )
        return len(entries)

    def submit(self, operation_id: str, *, plan_key: str = "plan-abc") -> Response:
        body = json.dumps(
            {
                "operation_id": operation_id,
                "plan_key": plan_key,
                "run_key": "run-1",
                "bridge_token": "fresh",
            }
        ).encode("utf-8")
        return self.remote_app().handle(
            build(
                "POST",
                "/api/control/stop-after-current",
                headers=self.signed(),
                body=body,
            )
        )

    def observe_plan(self, *, primary_state: str) -> None:
        self.store.ingest_heartbeat(
            envelope(
                "heartbeat", heartbeat_payload(observed_at=NOW), connector_id="mac"
            ),
            received_at=NOW,
        )
        self.store.ingest_snapshot(
            envelope(
                "snapshot",
                snapshot_payload(
                    observed_at=NOW,
                    runs=[
                        run_row(
                            "run-1", plan_key="plan-abc", primary_state=primary_state
                        )
                    ],
                ),
                connector_id="mac",
            ),
            received_at=NOW,
        )

    def leave_unconfirmed(self, operation_id: str) -> None:
        self.submit(operation_id)
        self.store.advance_operation(operation_id, OPERATION_DISPATCHED, now=NOW)
        self.store.advance_operation(operation_id, OPERATION_UNCONFIRMED, now=NOW)


__all__ = [
    "Any",
    "Application",
    "CAPABILITY_OPERATION",
    "COMMAND_REDEEM_PATH",
    "FakeBodyLane",
    "FakeDetailLane",
    "FakeSubmissionLane",
    "FakeUploadLane",
    "FixedAuthenticator",
    "MAX_READ_LIMIT",
    "METHOD_QUESTION_REPLY",
    "METHOD_STOP_AFTER_CURRENT",
    "MODE_REMOTE",
    "NOW",
    "OPERATIONS",
    "OPERATION_DISPATCHED",
    "OPERATION_PENDING",
    "OPERATION_SUPERSEDED",
    "OPERATION_UNCONFIRMED",
    "ObservationPolicy",
    "Path",
    "READ_OUTCOMES",
    "REASON_OPERATION_CONFLICT",
    "REASON_OPERATION_UNCERTAIN",
    "REMOTE_POLICY",
    "ROLE_CONNECTOR",
    "ROLE_OWNER",
    "ReadAnswer",
    "RemoteProviderTestsBase",
    "RemoteReadError",
    "Response",
    "TemporaryDirectory",
    "WorkbenchStore",
    "build",
    "catalog_entry",
    "catalog_payload",
    "document",
    "envelope",
    "heartbeat_payload",
    "json",
    "mock",
    "run_row",
    "snapshot_payload",
    "threading",
    "time",
    "unittest",
    "workbench_service",
]
