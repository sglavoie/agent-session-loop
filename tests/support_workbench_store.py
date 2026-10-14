"""Shared fixtures, payload builders and store base cases for workbench store tests."""

from __future__ import annotations

import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from asl_workbench import (
    incidents,
    observation,
    store_migrations,
    store_types,
    store_workbench,
)

NOW = 1789574400.0
CONNECTOR_ID = "mac-studio"
CONTRACT_SET_ID = observation.SUPPORTED_CONTRACT_SET_IDS[0]
PACKAGE = Path(__file__).resolve().parent.parent / "asl_workbench"

#: Loose enough that a test can name its own thresholds and tight enough that a
#: test never has to advance a clock by a week to see a timer fire.
POLICY = observation.ObservationPolicy(
    observation_stale_seconds=60.0,
    lost_observation_alert_seconds=120.0,
    unanswered_question_alert_seconds=120.0,
    summary_retention_days=7.0,
)
RETENTION = POLICY.summary_retention_seconds


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def envelope(
    kind: str, payload: dict[str, Any], *, connector_id: str = CONNECTOR_ID
) -> dict[str, Any]:
    return {
        "protocol": 1,
        "kind": kind,
        "connector_id": connector_id,
        "sent_at": payload.get("observed_at", payload.get("generated_at", 0.0)),
        "payload": payload,
    }


def run_row(run_key: str, **overrides: Any) -> dict[str, Any]:
    """One projected run, carrying the fields a snapshot actually sends.

    ``action_token`` and ``actions`` are here on purpose: they are what the
    live cockpit needs and what the store must refuse to write down, so every
    test that ingests a run is also a test that they were dropped.
    """
    row: dict[str, Any] = {
        "run_key": run_key,
        "project": "pi-monitoring",
        "plan_key": f"plan-{run_key}",
        "run_id": f"20260619T140000Z-{run_key}",
        "primary_state": "running",
        "phase": "running",
        "urgency_group": "in-progress",
        "action_token": "37db468d7f42a93b7e29a1f71551663e",
        "actions": [],
        "last_activity_at": NOW - 10.0,
    }
    row.update(overrides)
    return row


def heartbeat_payload(*, observed_at: float, generation: int = 1, **overrides: Any):
    payload: dict[str, Any] = {
        "observed_at": observed_at,
        "bridge_alive": True,
        "bridge_protocol": 2,
        "asl_version": "0.2.0",
        "contract_set_id": CONTRACT_SET_ID,
        "source_generation": generation,
    }
    payload.update(overrides)
    return payload


def snapshot_payload(
    *,
    observed_at: float,
    runs: list[dict[str, Any]] | None = None,
    truncated: bool = False,
    generation: int = 1,
    digest: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    rows = list(runs or [])
    material = json.dumps([observed_at, rows], sort_keys=True).encode()
    payload: dict[str, Any] = {
        "observed_at": observed_at,
        "digest": digest or sha256(material).hexdigest(),
        "contract_set_id": CONTRACT_SET_ID,
        "staleness_bound_seconds": 20.0,
        "source_generation": generation,
        "row_count": len(rows),
        "truncated": truncated,
        "runs": rows,
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def history_entry(entry_id: str, *, recorded_at: float, **overrides: Any):
    entry: dict[str, Any] = {
        "entry_id": entry_id,
        "source_key": "a41d0e8b6c93f752",
        "plan_key": f"plan-{entry_id}",
        "run_key": f"run-{entry_id}",
        "project": "pi-monitoring",
        "outcome": "completed",
        "recorded_at": recorded_at,
        "run_started_at": recorded_at - 600.0,
        "run_finished_at": recorded_at - 10.0,
        "duration_seconds": 590.0,
        "event_schema_version": 2,
    }
    entry.update(overrides)
    return entry


def history_payload(
    *,
    generated_at: float,
    entries: list[dict[str, Any]] | None = None,
    complete: bool = True,
    batch_id: str = "8f21a0c4d7b95e63",
    **overrides: Any,
) -> dict[str, Any]:
    rows = list(entries or [])
    payload: dict[str, Any] = {
        "batch_id": batch_id,
        "generated_at": generated_at,
        "window_start": generated_at - RETENTION,
        "window_end": generated_at,
        "entry_count": len(rows),
        "complete": complete,
        "entries": rows,
    }
    payload.update(overrides)
    return payload


def catalog_entry(plan_key: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "plan_key": plan_key,
        "run_key": f"run-{plan_key}",
        "title": f"Title for {plan_key}",
        "display_label": plan_key,
        "primary_state": "running",
        "recommended_action": "watch",
        "urgency_rank": 10,
        "urgency_group": "in-progress",
        "activity_label": "active",
        "projects": ["pi-monitoring"],
        "project_group": "pi-monitoring",
        "counts": {"sessions": 3},
        "sources": ["manifest"],
        "exists": True,
        "pinned": False,
        "repository_count": 1,
        "warning_count": 0,
        "search_text": f"{plan_key} title for {plan_key}".casefold(),
        "registry_digest": "9d2e59249fe21a3c",
    }
    entry.update(overrides)
    return entry


def catalog_payload(
    *,
    observed_at: float,
    generation: str,
    entries: list[dict[str, Any]] | None = None,
    complete: bool = True,
    final: bool = True,
    offset: int = 0,
    source_generation: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    rows = list(entries or [])
    payload: dict[str, Any] = {
        "observed_at": observed_at,
        "generation": generation,
        "source_generation": source_generation,
        "complete": complete,
        "final": final,
        "offset": offset,
        "total": len(rows) + offset,
        "entries": rows,
    }
    payload.update(overrides)
    return payload


class FakePublisher:
    """A publisher that records what it was asked to send and says what it likes."""

    def __init__(self, *results: incidents.PublishResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        #: Runs before the send gate is consulted, so a test can make the
        #: incident stop being worth sending while the publisher holds it.
        self.before_gate: Any = None

    def publish(self, *, before_send: Any = None, **values: Any):
        if self.before_gate is not None:
            self.before_gate()
        if before_send is not None and not before_send():
            # Nothing left the process, and the publisher has to say so rather
            # than leaving the outcome unknown.
            return incidents.PublishResult(incidents.DELIVERY_RETRY)
        self.calls.append(values)
        if self.results:
            return self.results.pop(0)
        return incidents.PublishResult(
            incidents.DELIVERY_PUBLISHED, f"receipt-{len(self.calls)}"
        )


class StoreCase(unittest.TestCase):
    """One store on disk, with helpers for the uploads every test makes."""

    policy = POLICY

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workbench.sqlite3"
        self.store = self.open_store(created_at=NOW - RETENTION)
        self.wall = NOW

    def open_store(self, *, created_at: float = NOW) -> store_workbench.WorkbenchStore:
        opened = store_workbench.WorkbenchStore.open(
            self.path, policy=self.policy, created_at=created_at
        )
        self.addCleanup(opened.close)
        return opened

    # -- uploads ---------------------------------------------------------
    def send_heartbeat(self, *, observed_at: float, **kwargs: Any):
        return self.store.ingest_heartbeat(
            envelope(
                "heartbeat",
                heartbeat_payload(observed_at=observed_at, **kwargs),
                connector_id=kwargs.pop("connector_id", CONNECTOR_ID),
            ),
            received_at=observed_at,
        )

    def send_snapshot(
        self,
        *,
        observed_at: float,
        runs: list[dict[str, Any]] | None = None,
        connector_id: str = CONNECTOR_ID,
        received_at: float | None = None,
        **kwargs: Any,
    ):
        frame = envelope(
            "snapshot",
            snapshot_payload(observed_at=observed_at, runs=runs, **kwargs),
            connector_id=connector_id,
        )
        return self.store.ingest_snapshot(
            frame, received_at=observed_at if received_at is None else received_at
        )

    def send_history(
        self,
        *,
        generated_at: float,
        entries: list[dict[str, Any]] | None = None,
        connector_id: str = CONNECTOR_ID,
        received_at: float | None = None,
        **kwargs: Any,
    ):
        frame = envelope(
            "history_batch",
            history_payload(generated_at=generated_at, entries=entries, **kwargs),
            connector_id=connector_id,
        )
        return self.store.ingest_history(
            frame, received_at=generated_at if received_at is None else received_at
        )

    def send_catalog(
        self,
        *,
        observed_at: float,
        generation: str,
        entries: list[dict[str, Any]] | None = None,
        connector_id: str = CONNECTOR_ID,
        received_at: float | None = None,
        **kwargs: Any,
    ):
        frame = envelope(
            "catalog_page",
            catalog_payload(
                observed_at=observed_at,
                generation=generation,
                entries=entries,
                **kwargs,
            ),
            connector_id=connector_id,
        )
        return self.store.ingest_catalog(
            frame, received_at=observed_at if received_at is None else received_at
        )

    # -- reads -----------------------------------------------------------
    def run_keys(self, **kwargs: Any) -> set[str]:
        return {row["run_key"] for row in self.store.runs(now=NOW, **kwargs)}

    def rows_by_key(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return {row["run_key"]: row for row in self.store.runs(now=NOW, **kwargs)}

    def plans_by_key(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return {row["plan_key"]: row for row in self.store.catalog(now=NOW, **kwargs)}


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def companion_database(path: Path, *, created_at: float) -> None:
    """Build the database an installed Companion leaves behind: schema 3.

    Written out through this package's own migrations 1 to 3, which is only a
    valid stand-in because those tuples are byte-identical to the Companion's.
    ``test_the_inherited_migrations_are_unchanged`` is what holds that.
    """
    connection = store_migrations.connect(path)
    try:
        for version, statements in store_migrations.MIGRATIONS:
            if version > store_types.COMPANION_SCHEMA_VERSION:
                break
            for statement in statements:
                connection.execute(statement)
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (store_types.META_CREATED_AT, json.dumps(created_at)),
        )
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (
                store_types.META_PROJECT_CATALOG_PREFIX + CONNECTOR_ID,
                json.dumps(
                    {
                        "plan-alpha": {
                            "plan": "Alpha plan",
                            "title": "Ship the alpha",
                            "projects": ["scripts", "pi-monitoring"],
                        }
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO connector (connector_id, first_seen_at, "
            "snapshot_observed_at, snapshot_generation, snapshot_row_count, "
            "snapshot_truncated, contract_set_id, bridge_alive, "
            "heartbeat_observed_at) VALUES (?, ?, ?, ?, ?, 0, ?, 1, ?)",
            (CONNECTOR_ID, created_at, NOW - 30, 4, 1, CONTRACT_SET_ID, NOW - 30),
        )
        connection.execute(
            "INSERT INTO run_summary (connector_id, run_key, project, plan_key, "
            "run_id, primary_state, urgency_group, observed_at, received_at, "
            "source_generation, present) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                CONNECTOR_ID,
                "alpha",
                "plans-alpha-1234abcd5678",
                "plan-alpha",
                "20260619T140000Z-alpha",
                "running",
                "in-progress",
                NOW - 30,
                NOW - 30,
                4,
            ),
        )
        connection.execute(
            "INSERT INTO history_entry (connector_id, entry_id, source_key, "
            "plan_key, run_key, project, outcome, recorded_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                CONNECTOR_ID,
                "kept-entry",
                "a41d0e8b6c93f752",
                "plan-alpha",
                "alpha-previous",
                "plans-alpha-1234abcd5678",
                "completed",
                NOW - 600,
                NOW - 600,
            ),
        )
        connection.execute(
            "INSERT INTO operation (operation_id, connector_id, command_id, "
            "method, status, plan_key, run_key, requested_at, deliver_by, "
            "received_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "op-kept",
                CONNECTOR_ID,
                "cmd-kept",
                "question.reply",
                "pending",
                "plan-alpha",
                "alpha",
                NOW - 60,
                NOW + 60,
                NOW - 60,
                NOW - 60,
            ),
        )
        connection.execute(
            "INSERT INTO incident (incident_id, connector_id, run_key, plan_key, "
            "project, kind, detected_at, evidence_at, next_attempt_at, "
            "delivery_state, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "incident-kept",
                CONNECTOR_ID,
                "alpha",
                "plan-alpha",
                "pi-monitoring",
                incidents.INCIDENT_TERMINAL_FAILURE,
                NOW - 300,
                NOW - 300,
                NOW - 300,
                incidents.DELIVERY_PUBLISHED,
                NOW - 200,
            ),
        )
        connection.execute(
            f"PRAGMA user_version={store_types.COMPANION_SCHEMA_VERSION}"
        )
        connection.commit()
    finally:
        connection.close()


class IncidentCase(StoreCase):
    """A store, a fake publisher, and a clock the test moves by hand."""

    def setUp(self) -> None:
        super().setUp()
        self.publisher = FakePublisher()
        self.engine = incidents.IncidentEngine(
            store=self.store, clock=lambda: self.wall, publisher=self.publisher
        )

    def at(self, offset: float) -> None:
        self.wall = NOW + offset

    def question_row(
        self, question_id: str = "q-1", *, run_key: str = "run-1", **overrides: Any
    ):
        return run_row(
            run_key,
            primary_state="paused-for-reply",
            phase="awaiting-reply",
            question_id=question_id,
            question_asked_at=NOW - 3600,
            unanswered_count=1,
            **overrides,
        )

    def observe(self, runs: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        now = self.wall
        self.send_heartbeat(observed_at=now)
        self.send_snapshot(
            observed_at=kwargs.pop("observed_at", now),
            runs=[self.question_row()] if runs is None else runs,
            received_at=now,
            **kwargs,
        )

    def restart(self) -> None:
        self.store.close()
        self.store = self.open_store(created_at=self.wall)
        self.engine = incidents.IncidentEngine(
            store=self.store, clock=lambda: self.wall, publisher=self.publisher
        )

    def diagnostics(self) -> list[dict[str, Any]]:
        return self.engine.diagnostics()
