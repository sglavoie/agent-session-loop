"""The portable store: migration, ordered ingestion, omission, incidents, retention.

These are the Companion's persistence and incident scenarios, re-stated against
``asl_workbench`` rather than against ``companion``. The Pi's suite proves them
through a live HTTP service; this one proves them against the store directly,
because the point of the move is that the behavior is no longer the service's.

The tests are grouped by the claim they hold rather than by the method they
call, because every claim here is worth more than the code path under it:

* an installation that already has a ``companion.sqlite3`` keeps every row and
  every identifier in it, and gains catalog storage without losing the plan
  labels the old blob held;
* what is written down is bounded by the persistence allowlists, so a widened
  upload shape cannot widen what is kept;
* observation time orders ingestion and the generation counter only breaks
  ties, so a restarted source is never locked out;
* omission is evidence only from a whole view, and a catalog generation has to
  be both complete *and* finished arriving before it may retire anything;
* an incident is deduplicated by identity, is persisted before it is published,
  and survives a superseded run without losing its history; and
* retention ages out settled work and never an open uncertainty.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from asl_workbench import freshness, incidents, observation, store
from asl_workbench.operations import (
    FORBIDDEN_PERSISTED_KEYS,
    OPERATION_PERSIST_FIELDS,
    OperationError,
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

    def open_store(self, *, created_at: float = NOW) -> store.WorkbenchStore:
        opened = store.WorkbenchStore.open(
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
    connection = store.connect(path)
    try:
        for version, statements in store.MIGRATIONS:
            if version > store.COMPANION_SCHEMA_VERSION:
                break
            for statement in statements:
                connection.execute(statement)
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (store.META_CREATED_AT, json.dumps(created_at)),
        )
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (
                store.META_PROJECT_CATALOG_PREFIX + CONNECTOR_ID,
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
        connection.execute(f"PRAGMA user_version={store.COMPANION_SCHEMA_VERSION}")
        connection.commit()
    finally:
        connection.close()


class MigrationTests(unittest.TestCase):
    """The ledger is one integer, and every upgrade keeps what came before."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workbench.sqlite3"

    def test_a_fresh_database_arrives_at_the_declared_version(self) -> None:
        with store.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ) as opened:
            self.assertEqual(opened.schema_version, store.SCHEMA_VERSION)
            self.assertEqual(opened.created_at, NOW)
            self.assertTrue(opened.healthy())

    def test_reopening_migrates_nothing_and_keeps_the_first_creation_time(
        self,
    ) -> None:
        """Restarting is safe because a current database is a no-op."""
        with store.WorkbenchStore.open(self.path, policy=POLICY, created_at=NOW):
            pass
        with store.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW + 5000
        ) as second:
            self.assertEqual(second.schema_version, store.SCHEMA_VERSION)
            self.assertEqual(second.created_at, NOW)

    def test_a_newer_database_is_refused_rather_than_written_through(self) -> None:
        with store.WorkbenchStore.open(self.path, policy=POLICY, created_at=NOW):
            pass
        connection = store.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
        with self.assertRaises(store.StoreError):
            store.migrate(connection)

    def test_a_failed_migration_leaves_the_previous_version_intact(self) -> None:
        """Each step commits with its own version bump, so a failure rolls back."""
        connection = store.connect(self.path)
        self.addCleanup(connection.close)
        broken = (*store.MIGRATIONS[:1], (2, ("CREATE TABLE service_meta (x)",)))
        with (
            mock.patch.object(store, "MIGRATIONS", broken),
            self.assertRaises(sqlite3.OperationalError),
        ):
            store.migrate(connection, created_at=NOW)
        self.assertEqual(store.schema_version(connection), 1)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM connector").fetchone()[0], 0
        )

    def test_the_inherited_migrations_are_unchanged(self) -> None:
        """Migrations 1 to 3 are the Companion's, so an existing file skips them."""
        versions = [version for version, _ in store.MIGRATIONS]
        self.assertEqual(versions, [1, 2, 3, 4])
        self.assertEqual(store.MIGRATIONS[2][1], incidents.MIGRATION)
        self.assertEqual(store.SCHEMA_VERSION, versions[-1])
        self.assertEqual(store.COMPANION_SCHEMA_VERSION, 3)

    def test_an_installed_companion_database_keeps_every_row_and_identifier(
        self,
    ) -> None:
        companion_database(self.path, created_at=NOW - RETENTION)
        with store.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ) as opened:
            self.assertEqual(opened.schema_version, store.SCHEMA_VERSION)
            # Creation time is the installation's, not this upgrade's.
            self.assertEqual(opened.created_at, NOW - RETENTION)
            self.assertEqual(
                [row["run_key"] for row in opened.runs(now=NOW)], ["alpha"]
            )
            self.assertEqual(
                [row["entry_id"] for row in opened.history(now=NOW)], ["kept-entry"]
            )
            self.assertEqual(opened.operation("op-kept")["command_id"], "cmd-kept")
            with opened.reading() as connection:
                kept = connection.execute(
                    "SELECT incident_id, delivery_state FROM incident"
                ).fetchall()
            self.assertEqual(
                [(row["incident_id"], row["delivery_state"]) for row in kept],
                [("incident-kept", incidents.DELIVERY_PUBLISHED)],
            )

    def test_the_old_project_blob_becomes_catalog_labels_rather_than_being_lost(
        self,
    ) -> None:
        """Every label an installation had accumulated survives the upgrade.

        Without this, the first start after the upgrade would re-label months of
        retained history from its plan directory name.
        """
        companion_database(self.path, created_at=NOW - RETENTION)
        with store.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ) as opened:
            row = opened.runs(now=NOW)[0]
            self.assertEqual(row["project"], "scripts + pi-monitoring")
            self.assertEqual(row["plan_label"], "Alpha plan")
            self.assertEqual(row["plan_title"], "Ship the alpha")
            self.assertEqual(opened.projects(now=NOW), ["pi-monitoring", "scripts"])
            # A carried label is not an observation, so it is not a catalog
            # entry: it can name a plan and it cannot claim one exists.
            self.assertEqual(opened.catalog(now=NOW), [])
            self.assertEqual(opened.counts(now=NOW).plans, 0)

    def test_a_carried_label_is_retired_by_the_first_complete_generation(
        self,
    ) -> None:
        """A plan the Mac no longer enumerates is genuinely gone."""
        companion_database(self.path, created_at=NOW - RETENTION)
        opened = store.WorkbenchStore.open(self.path, policy=POLICY, created_at=NOW)
        self.addCleanup(opened.close)
        opened.ingest_catalog(
            envelope(
                "catalog_page",
                catalog_payload(
                    observed_at=NOW - 5,
                    generation="gen-1",
                    entries=[catalog_entry("plan-beta")],
                ),
            ),
            received_at=NOW - 5,
        )
        with opened.reading() as connection:
            legacy = connection.execute(
                "SELECT present FROM catalog_entry WHERE plan_key = ?",
                ("plan-alpha",),
            ).fetchone()
        self.assertEqual(legacy["present"], 0)
        self.assertEqual(
            [row["plan_key"] for row in opened.catalog(now=NOW)], ["plan-beta"]
        )


# --------------------------------------------------------------------------
# What is written down
# --------------------------------------------------------------------------


class SchemaBoundaryTests(StoreCase):
    """The schema is the allowlist, plus bookkeeping the source never sent."""

    ALLOWLISTS = {
        "run_summary": observation.RUN_SUMMARY_PERSIST_FIELDS,
        "history_entry": observation.HISTORY_PERSIST_FIELDS,
        "operation": OPERATION_PERSIST_FIELDS,
        "catalog_entry": observation.CATALOG_PERSIST_FIELDS,
    }

    def test_every_stored_column_is_an_allowed_field_or_bookkeeping(self) -> None:
        for table, allowed in self.ALLOWLISTS.items():
            with self.subTest(table=table):
                bookkeeping = store.SCHEMA_BOOKKEEPING[table]
                unexplained = set(self.store.columns(table)) - allowed - bookkeeping
                self.assertEqual(unexplained, set())

    def test_no_table_carries_a_column_that_could_hold_prose(self) -> None:
        with self.store.reading() as connection:
            tables = [
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ]
        self.assertGreater(len(tables), 5)
        for table in tables:
            with self.subTest(table=table):
                columns = set(self.store.columns(table))
                self.assertEqual(columns & FORBIDDEN_PERSISTED_KEYS, set())

    def test_a_run_reaches_disk_without_the_fields_it_was_not_allowed_to_keep(
        self,
    ) -> None:
        self.send_heartbeat(observed_at=NOW - 10)
        self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        with self.store.reading() as connection:
            row = dict(connection.execute("SELECT * FROM run_summary").fetchone())
        self.assertNotIn("action_token", row)
        self.assertNotIn("actions", row)
        self.assertEqual(row["run_id"], "20260619T140000Z-alpha")

    def test_an_exported_row_never_carries_a_body_field(self) -> None:
        self.send_heartbeat(observed_at=NOW - 10)
        self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        self.send_history(
            generated_at=NOW - 20, entries=[history_entry("e1", recorded_at=NOW - 20)]
        )
        self.send_catalog(
            observed_at=NOW - 10, generation="gen-1", entries=[catalog_entry("plan-a")]
        )
        exported = (
            self.store.runs(now=NOW)
            + self.store.history(now=NOW)
            + self.store.catalog(now=NOW)
        )
        self.assertEqual(len(exported), 3)
        for row in exported:
            self.assertEqual(set(row) & FORBIDDEN_PERSISTED_KEYS, set())

    def test_a_run_smuggling_prose_under_an_allowed_key_is_refused(self) -> None:
        """The allowlist drops declared fields; the nested scan catches the rest."""
        self.send_heartbeat(observed_at=NOW - 20)
        with self.assertRaises(OperationError) as refusal:
            self.send_snapshot(
                observed_at=NOW - 10,
                runs=[run_row("alpha", outcome={"reply_text": "the whole answer"})],
            )
        self.assertEqual(refusal.exception.reason, "operation-body-leak")
        self.assertEqual(self.store.runs(now=NOW), [])


class PortabilityTests(unittest.TestCase):
    """The portable half may not reach into the worker, at any remove.

    This is the property the whole move exists for: the Pi installs the
    package and not the runner, so an import of the runner anywhere in the
    reachable graph is a deployment that cannot start rather than a style
    complaint. Checking the graph rather than the four files is what stops the
    dependency from arriving through a helper two hops away.
    """

    ROOTS = ("store", "incidents", "freshness", "observation")

    def imports_of(self, module: str) -> tuple[set[str], set[str]]:
        """The absolute and in-package names one module imports."""
        tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
        absolute: set[str] = set()
        local: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = node.module
                    if base is None:
                        local.update(alias.name for alias in node.names)
                    else:
                        local.add(base.split(".")[0])
                elif node.module:
                    absolute.add(node.module)
        return absolute, local

    def reachable(self) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        pending = list(self.ROOTS)
        while pending:
            module = pending.pop()
            if module in found or not (PACKAGE / f"{module}.py").exists():
                continue
            absolute, local = self.imports_of(module)
            found[module] = absolute
            pending.extend(local)
        return found

    def test_nothing_the_store_reaches_imports_the_runner(self) -> None:
        graph = self.reachable()
        self.assertGreaterEqual(len(graph), len(self.ROOTS))
        for module, absolute in graph.items():
            with self.subTest(module=module):
                runner = {
                    name
                    for name in absolute
                    if name == "agent_session_loop"
                    or name.startswith("agent_session_loop.")
                }
                self.assertEqual(runner, set())

    def test_nothing_the_store_reaches_needs_a_dependency(self) -> None:
        """Stdlib only, so the Pi installs a directory rather than a wheel."""
        for module, absolute in self.reachable().items():
            with self.subTest(module=module):
                for name in absolute:
                    root = name.split(".")[0]
                    self.assertIn(
                        root,
                        sys.stdlib_module_names,
                        f"{module} imports non-stdlib {name}",
                    )


# --------------------------------------------------------------------------
# Ordered ingestion
# --------------------------------------------------------------------------


class OrderedIngestionTests(StoreCase):
    """Time orders; the generation counter only breaks ties."""

    def test_a_snapshot_stores_every_run_it_carries(self) -> None:
        result = self.send_snapshot(
            observed_at=NOW - 1,
            runs=[run_row("alpha"), run_row("beta", project="scripts")],
        )
        self.assertEqual(result.stored, 2)
        self.assertFalse(result.duplicate)
        self.assertEqual(self.run_keys(), {"alpha", "beta"})

    def test_an_identical_redelivery_is_a_duplicate_and_changes_nothing(self) -> None:
        payload = snapshot_payload(observed_at=NOW - 1, runs=[run_row("alpha")])
        first = self.store.ingest_snapshot(
            envelope("snapshot", payload), received_at=NOW - 1
        )
        second = self.store.ingest_snapshot(
            envelope("snapshot", payload), received_at=NOW - 1
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(self.store.runs(now=NOW)), 1)

    def test_a_repeated_heartbeat_instant_is_a_duplicate(self) -> None:
        self.send_heartbeat(observed_at=NOW - 10)
        self.assertTrue(self.send_heartbeat(observed_at=NOW - 10).duplicate)

    def test_an_older_observation_is_refused_as_stale(self) -> None:
        self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        with self.assertRaises(store.IngestRefused) as refusal:
            self.send_snapshot(observed_at=NOW - 20, runs=[run_row("beta")])
        self.assertEqual(refusal.exception.code, store.REFUSED_STALE_OBSERVATION)
        self.assertEqual(self.run_keys(), {"alpha"})

    def test_a_superseded_bridge_session_is_refused_as_a_stale_generation(
        self,
    ) -> None:
        """Same instant, older bridge: the generation is what breaks the tie."""
        self.send_snapshot(observed_at=NOW - 10, generation=5, runs=[run_row("alpha")])
        with self.assertRaises(store.IngestRefused) as refusal:
            self.send_snapshot(
                observed_at=NOW - 10, generation=3, runs=[run_row("beta")]
            )
        self.assertEqual(refusal.exception.code, store.REFUSED_STALE_GENERATION)
        self.assertEqual(self.run_keys(), {"alpha"})

    def test_a_restarted_connector_is_not_locked_out_by_its_reset_counter(
        self,
    ) -> None:
        """Ordering on the counter would refuse a restarted Mac forever."""
        self.send_snapshot(observed_at=NOW - 10, generation=7, runs=[run_row("alpha")])
        self.send_snapshot(observed_at=NOW - 5, generation=1, runs=[run_row("beta")])
        self.assertEqual(self.run_keys(), {"beta"})

    def test_an_old_heartbeat_cannot_overwrite_a_newer_one(self) -> None:
        self.send_heartbeat(observed_at=NOW - 10, generation=4)
        with self.assertRaises(store.IngestRefused) as refusal:
            self.send_heartbeat(observed_at=NOW - 20, generation=4)
        self.assertEqual(refusal.exception.code, store.REFUSED_STALE_OBSERVATION)
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.last_heartbeat_at, NOW - 10)

    def test_a_superseded_heartbeat_at_the_same_instant_changes_nothing(self) -> None:
        """Two bridges at one instant: the second is a retry, not a rewrite.

        Unlike a snapshot, a heartbeat carries no rows, so the older generation
        has nothing to overwrite and is absorbed as the duplicate it is.
        """
        self.send_heartbeat(observed_at=NOW - 10, generation=4)
        again = self.send_heartbeat(observed_at=NOW - 10, generation=2)
        self.assertTrue(again.duplicate)
        with self.store.reading() as connection:
            row = connection.execute("SELECT * FROM connector").fetchone()
        self.assertEqual(row["heartbeat_generation"], 4)

    def test_a_snapshot_that_repeats_a_run_is_refused_without_partial_writes(
        self,
    ) -> None:
        """Atomicity proven on the rows: the refused snapshot leaves no trace.

        The repeat is only detectable part-way through the row loop, so rows
        have already been written when it is found. What the reader sees
        afterwards is the previous snapshot, entire.
        """
        self.send_snapshot(
            observed_at=NOW - 20, runs=[run_row("alpha"), run_row("beta")]
        )
        before = self.store.connector_statuses(now=NOW)[0]
        with self.assertRaises(store.IngestInvalid):
            self.send_snapshot(
                observed_at=NOW - 10,
                runs=[run_row("gamma"), run_row("delta"), run_row("gamma")],
            )
        after = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(self.run_keys(include_absent=True), {"alpha", "beta"})
        self.assertEqual(after.last_snapshot_at, before.last_snapshot_at)
        self.assertEqual(after.row_count, 2)

    def test_an_unknown_refusal_code_cannot_be_invented(self) -> None:
        with self.assertRaises(AssertionError):
            raise store.IngestRefused("made-up", "detail")


# --------------------------------------------------------------------------
# Omission
# --------------------------------------------------------------------------


class OmissionTests(StoreCase):
    """Omission is evidence only from a view that claimed to be whole."""

    def test_a_complete_snapshot_marks_the_runs_it_omits_absent(self) -> None:
        self.send_snapshot(
            observed_at=NOW - 20, runs=[run_row("alpha"), run_row("beta")]
        )
        result = self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        self.assertEqual(result.absent, 1)
        self.assertEqual(self.run_keys(), {"alpha"})
        self.assertEqual(self.run_keys(include_absent=True), {"alpha", "beta"})

    def test_a_truncated_snapshot_never_proves_a_run_disappeared(self) -> None:
        """The omitted rows are unknown, not gone. Nothing is marked absent."""
        self.send_heartbeat(observed_at=NOW - 20)
        self.send_snapshot(
            observed_at=NOW - 20, runs=[run_row("alpha"), run_row("beta")]
        )
        result = self.send_snapshot(
            observed_at=NOW - 10, runs=[run_row("alpha")], truncated=True
        )
        self.assertEqual(result.absent, 0)
        rows = self.rows_by_key()
        self.assertEqual(set(rows), {"alpha", "beta"})
        self.assertEqual(rows["alpha"]["observation_status"], "current")
        self.assertEqual(rows["beta"]["observation_status"], "unconfirmed")
        self.assertEqual(rows["beta"]["last_observed_at"], NOW - 20)
        # An unconfirmed row is not a command target: nobody can see it now.
        self.assertIsNone(self.store.command_target("beta"))
        counts = self.store.counts(now=NOW)
        self.assertEqual(counts.unconfirmed_runs, 1)
        self.assertEqual(counts.running, 1)
        self.send_heartbeat(observed_at=NOW - 5)
        self.assertEqual(self.store.counts(now=NOW).unconfirmed_runs, 1)
        self.send_snapshot(
            observed_at=NOW,
            runs=[run_row("alpha"), run_row("beta")],
            truncated=True,
        )
        self.assertEqual(self.store.counts(now=NOW).unconfirmed_runs, 0)
        self.assertIsNotNone(self.store.command_target("beta"))

    def test_partial_replacement_retires_only_positively_superseded_runs(
        self,
    ) -> None:
        old = run_row("old", plan_key="shared-plan", run_started_at=NOW - 1000)
        unrelated = run_row(
            "unrelated",
            urgency_group="needs-action",
            primary_state="paused-for-reply",
            question_id="q-unrelated",
            unanswered_count=1,
        )
        self.send_snapshot(observed_at=NOW - 20, runs=[old, unrelated])
        replacement = run_row("new", plan_key="shared-plan", run_started_at=NOW - 10)
        result = self.send_snapshot(
            observed_at=NOW - 5, runs=[replacement], truncated=True
        )
        self.assertEqual(result.absent, 1)
        rows = self.rows_by_key(include_absent=True)
        self.assertEqual(rows["old"]["observation_status"], "absent")
        self.assertEqual(rows["new"]["observation_status"], "current")
        self.assertEqual(rows["unrelated"]["observation_status"], "unconfirmed")
        self.assertEqual(rows["unrelated"]["question_id"], "q-unrelated")
        self.assertIsNone(self.store.command_target("old"))
        self.assertEqual(self.store.counts(now=NOW).needs_attention, 0)
        self.assertEqual(self.store.counts(now=NOW).unconfirmed_runs, 1)

    def test_older_or_unknown_run_start_is_not_positive_replacement_evidence(
        self,
    ) -> None:
        self.send_snapshot(
            observed_at=NOW - 20,
            runs=[run_row("new", plan_key="shared-plan", run_started_at=NOW - 100)],
        )
        self.send_snapshot(
            observed_at=NOW - 10,
            runs=[run_row("older", plan_key="shared-plan", run_started_at=NOW - 200)],
            truncated=True,
        )
        self.assertEqual(self.rows_by_key()["new"]["observation_status"], "unconfirmed")
        self.send_snapshot(
            observed_at=NOW,
            runs=[run_row("unknown", plan_key="shared-plan")],
            truncated=True,
        )
        self.assertEqual(len(self.store.runs(now=NOW)), 3)

    def test_replacement_preserves_terminal_history_and_separates_connectors(
        self,
    ) -> None:
        old = run_row("old", plan_key="shared-plan", run_started_at=NOW - 1000)
        self.send_snapshot(observed_at=NOW - 20, runs=[old])
        self.send_history(
            generated_at=NOW - 15,
            entries=[
                history_entry(
                    "old-history",
                    run_key="old",
                    plan_key="shared-plan",
                    recorded_at=NOW - 15,
                )
            ],
        )
        self.send_heartbeat(observed_at=NOW - 20, connector_id="another-mac")
        self.send_snapshot(observed_at=NOW - 20, runs=[old], connector_id="another-mac")
        self.send_snapshot(
            observed_at=NOW - 5,
            runs=[run_row("new", plan_key="shared-plan", run_started_at=NOW - 10)],
            truncated=True,
        )
        self.assertEqual(self.store.history(now=NOW)[0]["entry_id"], "old-history")
        self.assertEqual(
            self.store.command_target("old")["connector_id"], "another-mac"
        )

    def test_completeness_is_reported_per_source_rather_than_as_one_flag(
        self,
    ) -> None:
        self.send_heartbeat(observed_at=NOW - 5)
        self.send_snapshot(observed_at=NOW - 5, runs=[run_row("alpha")], truncated=True)
        self.send_history(generated_at=NOW - 5, entries=[], complete=False)
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.runs.completeness, freshness.COMPLETENESS_PARTIAL)
        self.assertEqual(status.history.completeness, freshness.COMPLETENESS_PARTIAL)
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_UNKNOWN)
        self.assertEqual(status.completeness, freshness.COMPLETENESS_PARTIAL)
        self.assertTrue(status.degraded)
        self.assertFalse(status.freshness.absence_proves_removal)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


class HistoryTests(StoreCase):
    """An ending does not change, so a batch upserts rather than duplicating."""

    def test_history_upserts_rather_than_duplicating(self) -> None:
        entries = [
            history_entry("e1", recorded_at=NOW - 100),
            history_entry("e2", recorded_at=NOW - 200, outcome="failed"),
        ]
        first = self.send_history(generated_at=NOW - 50, entries=entries)
        self.assertEqual(first.stored, 2)
        second = self.send_history(
            generated_at=NOW - 40, entries=entries, batch_id="second"
        )
        self.assertEqual((second.stored, second.updated), (0, 2))
        self.assertTrue(second.duplicate)
        self.assertEqual(len(self.store.history(now=NOW)), 2)

    def test_a_late_batch_cannot_overwrite_a_newer_completeness_claim(self) -> None:
        self.send_history(generated_at=NOW - 10, entries=[], complete=True)
        self.send_history(
            generated_at=NOW - 60, entries=[], complete=False, batch_id="late"
        )
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.history.completeness, freshness.COMPLETENESS_COMPLETE)

    def test_a_stored_ending_keeps_its_lists_and_flags(self) -> None:
        self.send_history(
            generated_at=NOW - 10,
            entries=[
                history_entry(
                    "e1",
                    recorded_at=NOW - 10,
                    completed_session_ids=["s1", "s2"],
                    review_completed=True,
                    reconstructed=False,
                )
            ],
        )
        row = self.store.history(now=NOW)[0]
        self.assertEqual(row["completed_session_ids"], ["s1", "s2"])
        self.assertIs(row["review_completed"], True)
        self.assertIs(row["reconstructed"], False)

    def test_history_filters_by_outcome_and_harness(self) -> None:
        self.send_history(
            generated_at=NOW - 10,
            entries=[
                history_entry("good", recorded_at=NOW - 10, harness="claude"),
                history_entry(
                    "bad", recorded_at=NOW - 20, outcome="failed", harness="codex"
                ),
            ],
        )
        self.assertEqual(
            [row["entry_id"] for row in self.store.history(now=NOW, outcome="failed")],
            ["bad"],
        )
        self.assertEqual(
            [row["entry_id"] for row in self.store.history(now=NOW, harness="claude")],
            ["good"],
        )
        self.assertEqual(self.store.harnesses(now=NOW), ["claude", "codex"])


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


class CatalogTests(StoreCase):
    """Catalog storage is separate from run history, and pages honestly."""

    def test_a_catalog_page_is_stored_apart_from_run_history(self) -> None:
        result = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        self.assertEqual(result.stored, 2)
        self.assertEqual(self.store.runs(now=NOW), [])
        plans = self.plans_by_key()
        self.assertEqual(set(plans), {"plan-a", "plan-b"})
        self.assertEqual(plans["plan-a"]["projects"], ["pi-monitoring"])
        self.assertEqual(plans["plan-a"]["counts"], {"sessions": 3})
        self.assertIs(plans["plan-a"]["exists"], True)
        self.assertIs(plans["plan-a"]["multi_repository"], False)
        self.assertEqual(plans["plan-a"]["observation_status"], "current")
        self.assertEqual(plans["plan-a"]["generation"], "gen-1")

    def test_a_complete_final_generation_retires_what_it_omits(self) -> None:
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        result = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
        )
        self.assertEqual(result.absent, 1)
        self.assertEqual(set(self.plans_by_key()), {"plan-a"})
        absent = self.plans_by_key(include_absent=True)["plan-b"]
        self.assertEqual(absent["observation_status"], "absent")

    def test_a_partial_generation_never_retires_unseen_work(self) -> None:
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        result = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
            complete=False,
        )
        self.assertEqual(result.absent, 0)
        plans = self.plans_by_key()
        self.assertEqual(set(plans), {"plan-a", "plan-b"})
        self.assertEqual(plans["plan-b"]["observation_status"], "unconfirmed")
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_PARTIAL)

    def test_a_complete_generation_still_arriving_retires_nothing_yet(self) -> None:
        """Retiring at page one would empty a catalog too large for one page."""
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        first = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
            final=False,
            total=2,
        )
        self.assertEqual(first.absent, 0)
        self.assertEqual(set(self.plans_by_key()), {"plan-a", "plan-b"})
        # Complete but unfinished is not complete yet, and must not be
        # reported as though absence already meant something.
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_UNKNOWN)
        second = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-c")],
            offset=1,
            total=2,
        )
        self.assertEqual(second.absent, 1)
        self.assertEqual(set(self.plans_by_key()), {"plan-a", "plan-c"})
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_COMPLETE)

    def test_a_replayed_page_of_the_same_generation_is_a_duplicate(self) -> None:
        entries = [catalog_entry("plan-a")]
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=entries,
            final=False,
            total=2,
        )
        replay = self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=entries,
            final=False,
            total=2,
        )
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.stored, 0)
        self.assertEqual(len(self.store.catalog(now=NOW)), 1)

    def test_an_older_catalog_generation_is_refused(self) -> None:
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
        )
        with self.assertRaises(store.IngestRefused) as refusal:
            self.send_catalog(
                observed_at=NOW - 20,
                generation="gen-1",
                entries=[catalog_entry("plan-b")],
            )
        self.assertEqual(refusal.exception.code, store.REFUSED_STALE_OBSERVATION)
        self.assertEqual(set(self.plans_by_key()), {"plan-a"})

    def test_a_page_that_repeats_a_plan_is_refused_without_partial_writes(
        self,
    ) -> None:
        self.send_catalog(
            observed_at=NOW - 20,
            generation="gen-1",
            entries=[catalog_entry("plan-a")],
        )
        with self.assertRaises(store.IngestInvalid):
            self.send_catalog(
                observed_at=NOW - 10,
                generation="gen-2",
                entries=[catalog_entry("plan-b"), catalog_entry("plan-b")],
            )
        self.assertEqual(set(self.plans_by_key()), {"plan-a"})

    def test_the_catalog_pages_filters_and_counts(self) -> None:
        entries = [
            catalog_entry("plan-a", urgency_rank=1, projects=["scripts"]),
            catalog_entry("plan-b", urgency_rank=2),
            catalog_entry("plan-c", urgency_rank=3),
        ]
        self.send_catalog(observed_at=NOW - 10, generation="gen-1", entries=entries)
        page = self.store.catalog(now=NOW, limit=2)
        self.assertEqual([row["plan_key"] for row in page], ["plan-a", "plan-b"])
        rest = self.store.catalog(now=NOW, limit=2, offset=2)
        self.assertEqual([row["plan_key"] for row in rest], ["plan-c"])
        self.assertEqual(
            [row["plan_key"] for row in self.store.catalog(now=NOW, project="scripts")],
            ["plan-a"],
        )
        self.assertEqual(
            [
                row["plan_key"]
                for row in self.store.catalog(now=NOW, search="title for plan-b")
            ],
            ["plan-b"],
        )
        counts = self.store.counts(now=NOW)
        self.assertEqual(counts.plans, 3)
        self.assertEqual(counts.absent_plans, 0)

    def test_catalog_labels_rather_than_run_rows_name_a_plan(self) -> None:
        """The catalog knows what a plan is called; a run summary only guessed."""
        self.send_snapshot(
            observed_at=NOW - 20,
            runs=[
                run_row(
                    "alpha",
                    plan_key="plan-a",
                    project="plan-1a2b3c4d5e6f-0011223344ff",
                )
            ],
        )
        self.send_catalog(
            observed_at=NOW - 10,
            generation="gen-1",
            entries=[
                catalog_entry(
                    "plan-a",
                    display_label="Alpha plan",
                    title="Ship the alpha",
                    projects=["scripts", "pi-monitoring"],
                )
            ],
        )
        row = self.store.runs(now=NOW)[0]
        self.assertEqual(row["project"], "scripts + pi-monitoring")
        self.assertEqual(row["plan_label"], "Alpha plan")
        self.assertEqual(row["plan_title"], "Ship the alpha")
        self.assertEqual(
            [r["run_key"] for r in self.store.runs(now=NOW, project="scripts")],
            ["alpha"],
        )
        self.assertEqual(
            [r["run_key"] for r in self.store.runs(now=NOW, search="ship the")],
            ["alpha"],
        )
        self.assertEqual(self.store.runs(now=NOW, project="nobody"), [])

    def test_an_unlabelled_plan_falls_back_without_leaking_a_directory_name(
        self,
    ) -> None:
        self.send_snapshot(
            observed_at=NOW - 10,
            runs=[run_row("alpha", project="plan-1a2b3c4d5e6f-0011223344ff")],
        )
        self.assertEqual(self.store.runs(now=NOW)[0]["project"], "Unknown project")


# --------------------------------------------------------------------------
# Operation journal
# --------------------------------------------------------------------------


def operation_record(operation_id: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "operation_id": operation_id,
        "command_id": f"cmd-{operation_id}",
        "method": "question.reply",
        "status": "pending",
        "plan_key": "plan-alpha",
        "run_key": "alpha",
        "run_id": "20260619T140000Z-alpha",
        "question_id": "q-1",
        "requested_at": NOW - 60,
        "deliver_by": NOW + 60,
    }
    record.update(overrides)
    return record


class OperationJournalTests(StoreCase):
    """One operation id is one mutation, whatever the browser does."""

    def test_a_repeated_operation_id_is_the_same_operation(self) -> None:
        first = self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        second = self.store.record_operation(
            operation_record("op-1", command_id="cmd-other"),
            connector_id=CONNECTOR_ID,
            received_at=NOW + 1,
        )
        self.assertEqual(first, second)
        self.assertEqual(second["command_id"], "cmd-op-1")
        self.assertEqual(len(self.store.operations_in_status("pending")), 1)

    def test_a_forbidden_transition_is_refused_and_a_repeat_is_not(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        self.store.advance_operation(
            "op-1", "dispatched", now=NOW + 1, dispatched_at=NOW + 1
        )
        repeated = self.store.advance_operation("op-1", "dispatched", now=NOW + 2)
        self.assertEqual(repeated["dispatched_at"], NOW + 1)
        with self.assertRaises(OperationError) as refusal:
            self.store.advance_operation("op-1", "pending", now=NOW + 3)
        self.assertEqual(refusal.exception.reason, "invalid-operation-transition")

    def test_an_unknown_operation_cannot_be_advanced(self) -> None:
        with self.assertRaises(OperationError) as refusal:
            self.store.advance_operation("nope", "dispatched", now=NOW)
        self.assertEqual(refusal.exception.reason, "unknown-operation")

    def test_one_outstanding_mutation_per_plan_is_visible_to_the_caller(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        self.assertEqual(
            self.store.outstanding_operation("plan-alpha")["operation_id"], "op-1"
        )
        self.store.advance_operation(
            "op-1", "dispatched", now=NOW + 1, dispatched_at=NOW + 1
        )
        self.store.advance_operation("op-1", "accepted", now=NOW + 2)
        # Accepted is still outstanding: ASL has the reply and has not yet
        # said what it did with it.
        self.assertIsNotNone(self.store.outstanding_operation("plan-alpha"))
        self.store.advance_operation(
            "op-1", "consumed", now=NOW + 3, settled_at=NOW + 3
        )
        self.assertIsNone(self.store.outstanding_operation("plan-alpha"))

    def test_the_journal_never_stores_the_reply_it_carried(self) -> None:
        with self.assertRaises(OperationError) as refusal:
            self.store.record_operation(
                operation_record("op-1", detail_code={"reply_text": "hello"}),
                connector_id=CONNECTOR_ID,
                received_at=NOW,
            )
        self.assertEqual(refusal.exception.reason, "operation-body-leak")
        self.assertIsNone(self.store.operation("op-1"))

    def test_an_exported_operation_is_narrower_than_the_stored_one(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR_ID, received_at=NOW
        )
        exported = self.store.exported_operations(now=NOW)
        self.assertEqual(len(exported), 1)
        self.assertNotIn("plan_key", exported[0])
        self.assertEqual(exported[0]["operation_id"], "op-1")

    def test_a_command_target_resolves_the_plan_the_client_never_chose(self) -> None:
        self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        target = self.store.command_target("alpha")
        self.assertEqual(target["plan_key"], "plan-alpha")
        self.assertEqual(target["connector_id"], CONNECTOR_ID)
        self.assertIsNone(self.store.command_target("nobody"))


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


class RetentionTests(StoreCase):
    """One cutoff for the time series, and never an open uncertainty."""

    def test_runs_and_history_share_one_cutoff(self) -> None:
        self.send_snapshot(observed_at=NOW - RETENTION - 10, runs=[run_row("old")])
        self.send_history(
            generated_at=NOW - RETENTION - 10,
            entries=[history_entry("old", recorded_at=NOW - RETENTION - 10)],
        )
        self.assertEqual(self.store.prune(now=NOW), 2)
        self.assertEqual(self.store.runs(now=NOW, include_absent=True), [])
        self.assertEqual(self.store.history(now=NOW), [])

    def test_only_settled_operations_age_out(self) -> None:
        """An open row is the record of a mutation whose outcome is unknown.

        Deleting it would erase that admission and release the hold that keeps
        a second attempt from running against an unfinished first one.
        """
        self.store.record_operation(
            operation_record("open", requested_at=NOW - RETENTION - 10),
            connector_id=CONNECTOR_ID,
            received_at=NOW - RETENTION - 10,
        )
        self.store.record_operation(
            operation_record(
                "settled", requested_at=NOW - RETENTION - 10, plan_key="plan-beta"
            ),
            connector_id=CONNECTOR_ID,
            received_at=NOW - RETENTION - 10,
        )
        self.store.advance_operation("settled", "cancelled", now=NOW - RETENTION - 5)
        self.store.prune(now=NOW)
        self.assertIsNotNone(self.store.operation("open"))
        self.assertIsNone(self.store.operation("settled"))
        self.assertEqual(
            self.store.outstanding_operation("plan-alpha")["operation_id"], "open"
        )

    def test_a_present_plan_never_expires_and_a_retired_one_does(self) -> None:
        """The catalog is not a time series: an old untouched plan is still a plan."""
        self.send_catalog(
            observed_at=NOW - RETENTION - 100,
            generation="gen-1",
            entries=[catalog_entry("plan-a"), catalog_entry("plan-b")],
        )
        self.send_catalog(
            observed_at=NOW - RETENTION - 50,
            generation="gen-2",
            entries=[catalog_entry("plan-a")],
            received_at=NOW - RETENTION - 50,
        )
        self.store.prune(now=NOW)
        self.assertEqual(set(self.plans_by_key(include_absent=True)), {"plan-a"})

    def test_retention_runs_at_most_once_an_interval_on_the_upload_path(self) -> None:
        self.send_snapshot(observed_at=NOW - RETENTION - 10, runs=[run_row("old")])
        first = self.send_snapshot(observed_at=NOW, runs=[run_row("fresh")])
        self.assertGreaterEqual(first.pruned, 1)
        self.send_heartbeat(observed_at=NOW + 1)
        self.assertEqual(self.store.meta(store.META_LAST_PRUNED_AT), NOW)

    def test_an_incident_still_publishing_is_never_deleted(self) -> None:
        with self.store.transaction() as connection:
            incidents.record(
                connection,
                kind=incidents.INCIDENT_TERMINAL_FAILURE,
                connector_id=CONNECTOR_ID,
                run_key="alpha",
                plan_key="plan-alpha",
                project="pi-monitoring",
                now=NOW - RETENTION - 100,
            )
            connection.execute(
                "UPDATE incident SET delivery_state = ?, resolved_at = ?",
                (incidents.DELIVERY_PUBLISHING, NOW - RETENTION - 50),
            )
        self.store.prune(now=NOW)
        with self.store.reading() as connection:
            remaining = connection.execute("SELECT COUNT(*) AS n FROM incident")
            self.assertEqual(remaining.fetchone()["n"], 1)


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------


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


class IncidentIdentityTests(IncidentCase):
    """A repeat is the same incident, and a different problem is a new one."""

    def test_identity_is_deterministic_and_connector_scoped(self) -> None:
        first = incidents.identity("unanswered-question", "mac-a", "run-1", "q-1")
        self.assertEqual(
            first, incidents.identity("unanswered-question", "mac-a", "run-1", "q-1")
        )
        self.assertNotEqual(
            first, incidents.identity("unanswered-question", "mac-b", "run-1", "q-1")
        )
        self.assertNotEqual(
            first, incidents.identity("unanswered-question", "mac-a", "run-1", "q-2")
        )

    def test_a_repeated_observation_produces_one_incident(self) -> None:
        self.observe()
        self.at(121)
        self.observe()
        for _ in range(4):
            self.engine.tick()
        self.assertEqual(len(self.diagnostics()), 1)
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.publisher.calls[0]["question_id"], "q-1")

    def test_a_different_question_is_a_different_incident(self) -> None:
        self.observe()
        self.at(121)
        self.observe()
        self.engine.tick()
        self.at(122)
        self.observe([self.question_row("q-2")])
        self.at(243)
        self.observe([self.question_row("q-2")])
        self.engine.tick()
        rows = self.diagnostics()
        self.assertEqual(len({row["incident_id"] for row in rows}), 2)
        self.assertEqual(sum(row["resolved_at"] is not None for row in rows), 1)

    def test_the_timer_measures_the_wait_and_not_each_snapshot(self) -> None:
        """Restarting the clock per snapshot means a question is never late."""
        self.observe()
        self.engine.tick()
        self.assertEqual(self.diagnostics(), [])
        self.at(120)
        self.observe()
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])
        self.at(120.001)
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_PUBLISHED
        )

    def test_an_unknown_incident_kind_is_refused(self) -> None:
        with self.store.transaction() as connection, self.assertRaises(ValueError):
            incidents.record(
                connection,
                kind="made-up",
                connector_id=CONNECTOR_ID,
                run_key="run-1",
                plan_key="plan-run-1",
                project="pi-monitoring",
                now=NOW,
            )


class IncidentResolutionTests(IncidentCase):
    """Fresh evidence resolves; a partial view does not."""

    def test_partial_omission_does_not_resolve_and_complete_absence_does(self) -> None:
        self.observe()
        self.at(121)
        self.observe()
        self.publisher.results = [incidents.PublishResult(incidents.DELIVERY_RETRY)]
        self.engine.tick()
        self.at(122)
        self.observe([run_row("other")], truncated=True)
        self.assertIsNone(self.diagnostics()[0]["resolved_at"])
        self.at(123)
        self.observe([])
        self.at(200)
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_CANCELLED
        )
        self.assertEqual(len(self.publisher.calls), 1)

    def test_a_reconnect_with_an_answer_discards_the_delayed_notification(
        self,
    ) -> None:
        self.observe()
        self.at(300)
        self.engine.tick()
        self.observe([run_row("run-1")])
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_CANCELLED
        )
        self.assertEqual(self.publisher.calls, [])

    def test_a_superseded_run_loses_its_watch_and_keeps_its_history(self) -> None:
        old = self.question_row(run_started_at=NOW - 1000)
        self.observe([old])
        self.at(301)
        self.observe([old])
        self.engine.publisher = None
        self.engine.tick()
        self.engine.publisher = self.publisher
        self.assertTrue(
            any(
                row["delivery_state"] == incidents.DELIVERY_PENDING
                for row in self.diagnostics()
            )
        )
        self.at(302)
        self.observe(
            [run_row("replacement", plan_key="plan-run-1", run_started_at=NOW + 300)],
            truncated=True,
        )
        rows = self.diagnostics()
        # Retired, not erased: what the store keeps about a superseded run is
        # the record that it was superseded.
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(row["resolved_at"] is not None for row in rows))
        self.assertTrue(
            all(row["delivery_state"] == incidents.DELIVERY_CANCELLED for row in rows)
        )
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])

    def test_a_terminal_failure_alerts_and_a_completion_does_not(self) -> None:
        self.at(10)
        self.observe([run_row("run-1")])
        self.at(20)
        self.send_history(
            generated_at=self.wall,
            entries=[
                history_entry(
                    "entry-1",
                    recorded_at=self.wall,
                    run_finished_at=self.wall,
                    run_key="run-1",
                    plan_key="plan-run-1",
                    outcome="failed",
                )
            ],
            received_at=self.wall,
        )
        self.engine.tick()
        rows = self.diagnostics()
        self.assertEqual(
            [row["kind"] for row in rows], [incidents.INCIDENT_TERMINAL_FAILURE]
        )
        self.assertEqual(len(self.publisher.calls), 1)

    def test_an_ending_older_than_the_monitoring_window_never_alerts(self) -> None:
        """A newly discovered plan's first import is a week of old endings."""
        self.at(10)
        self.send_history(
            generated_at=self.wall,
            entries=[
                history_entry(
                    "entry-old",
                    recorded_at=NOW - 10000,
                    run_finished_at=NOW - 10000,
                    outcome="failed",
                )
            ],
            received_at=self.wall,
        )
        self.engine.tick()
        self.assertEqual(self.diagnostics(), [])

    def test_losing_sight_of_active_work_alerts_and_recovery_resolves_it(
        self,
    ) -> None:
        self.observe([run_row("run-1")])
        self.at(121)
        self.engine.tick()
        rows = self.diagnostics()
        self.assertEqual(
            [row["kind"] for row in rows], [incidents.INCIDENT_LOST_OBSERVATION]
        )
        self.at(122)
        self.observe([run_row("run-1")])
        self.assertIsNotNone(self.diagnostics()[0]["resolved_at"])

    def test_a_paused_run_is_not_lost_active_work(self) -> None:
        self.observe([run_row("run-1", primary_state="paused-for-reply")])
        self.at(300)
        self.engine.tick()
        self.assertEqual(
            [row["kind"] for row in self.diagnostics()],
            [],
        )


class IncidentDeliveryTests(IncidentCase):
    """Persisted before it is attempted, and uncertainty recorded as uncertainty."""

    def ready(self) -> None:
        self.observe()
        self.at(121)
        self.observe()

    def test_a_refusal_to_send_retries_after_a_backoff(self) -> None:
        self.publisher.results = [incidents.PublishResult(incidents.DELIVERY_RETRY)]
        self.ready()
        self.engine.tick()
        row = self.diagnostics()[0]
        self.assertEqual(row["delivery_state"], incidents.DELIVERY_RETRY)
        self.assertEqual(row["detail_code"], incidents.DETAIL_NOT_SENT)
        self.assertEqual(row["attempts"], 1)
        # Still inside the backoff: the second tick must not send.
        self.engine.tick()
        self.assertEqual(len(self.publisher.calls), 1)
        self.at(121 + incidents.backoff_seconds(1) + 1)
        self.observe()
        self.engine.tick()
        self.assertEqual(len(self.publisher.calls), 2)
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_PUBLISHED
        )

    def test_backoff_doubles_and_then_stops_at_the_ceiling(self) -> None:
        self.assertEqual(incidents.backoff_seconds(0), 5.0)
        self.assertEqual(incidents.backoff_seconds(1), 10.0)
        self.assertEqual(incidents.backoff_seconds(5), 160.0)
        # A transport that has been down for an hour is not helped by being
        # asked every five seconds, so the interval stops growing rather than
        # running away.
        self.assertEqual(incidents.backoff_seconds(6), 300.0)
        self.assertEqual(incidents.backoff_seconds(50), 300.0)

    def test_an_ambiguous_delivery_is_recorded_and_never_replayed(self) -> None:
        self.publisher.results = [
            incidents.PublishResult(incidents.DELIVERY_UNCONFIRMED)
        ]
        self.ready()
        self.engine.tick()
        row = self.diagnostics()[0]
        self.assertEqual(row["delivery_state"], incidents.DELIVERY_UNCONFIRMED)
        self.assertEqual(row["detail_code"], incidents.DETAIL_DELIVERY_UNKNOWN)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(len(self.publisher.calls), 1)

    def test_a_crash_during_publishing_is_unconfirmed_on_restart(self) -> None:
        """A row left mid-send is the one thing a restart may not guess about.

        The process died between claiming the incident and learning what the
        transport did with it. Treating that as pending would double-send and
        treating it as published would hide a notification that never arrived,
        so it is recorded as the unknown it is and never attempted again.
        """
        self.ready()
        self.engine.tick()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE incident SET delivery_state = ?, detail_code = NULL, "
                "receipt_id = NULL, published_at = NULL",
                (incidents.DELIVERY_PUBLISHING,),
            )
        sent_before_the_crash = len(self.publisher.calls)
        self.restart()
        row = self.diagnostics()[0]
        self.assertEqual(row["delivery_state"], incidents.DELIVERY_UNCONFIRMED)
        self.assertEqual(row["detail_code"], incidents.DETAIL_RESTART_DURING_PUBLISH)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(len(self.publisher.calls), sent_before_the_crash)

    def test_a_publisher_that_raises_establishes_nothing(self) -> None:
        class Exploding:
            def publish(self, **_: Any):
                raise RuntimeError("remote detail nobody should see")

        self.engine.publisher = Exploding()
        self.ready()
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_UNCONFIRMED
        )

    def test_resolution_before_the_send_stops_it_without_any_bytes(self) -> None:
        """The gate is consulted at the transport, not only when claiming.

        An answer that lands while the publisher is connecting should stop the
        notification, because the moment that matters is the one just before
        the bytes leave.
        """
        self.ready()

        def answer_arrives() -> None:
            self.at(122)
            self.observe([run_row("run-1")])

        self.publisher.before_gate = answer_arrives
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_CANCELLED
        )

    def test_a_deployment_that_does_not_publish_never_sends(self) -> None:
        quiet = observation.ObservationPolicy(
            observation_stale_seconds=60.0,
            lost_observation_alert_seconds=120.0,
            unanswered_question_alert_seconds=120.0,
            publish_incidents=False,
        )
        self.store.close()
        self.policy = quiet
        self.store = self.open_store(created_at=NOW)
        self.engine = incidents.IncidentEngine(
            store=self.store, clock=lambda: self.wall, publisher=self.publisher
        )
        self.ready()
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_PENDING
        )

    def test_many_stale_questions_cannot_starve_a_terminal_failure(self) -> None:
        """A candidate nobody may send steps aside rather than blocking the queue."""
        stale = [
            self.question_row(f"q-{index}", run_key=f"stale-{index}")
            for index in range(3)
        ]
        self.observe(stale)
        self.at(121)
        self.observe(stale)
        # The projection goes quiet, so every question becomes ineligible.
        self.at(400)
        self.send_history(
            generated_at=self.wall,
            entries=[
                history_entry(
                    "entry-failed",
                    recorded_at=self.wall,
                    run_finished_at=self.wall,
                    run_key="stale-2",
                    plan_key="plan-stale-2",
                    outcome="failed",
                )
            ],
            received_at=self.wall,
        )
        self.engine.tick()
        self.assertEqual(
            [call["kind"] for call in self.publisher.calls],
            [incidents.INCIDENT_TERMINAL_FAILURE],
        )

    def test_diagnostics_expose_state_and_never_prose(self) -> None:
        self.ready()
        self.engine.tick()
        row = self.diagnostics()[0]
        self.assertEqual(set(row), set(incidents.DIAGNOSTIC_FIELDS))
        self.assertEqual(set(row) & FORBIDDEN_PERSISTED_KEYS, set())
        self.assertEqual(self.engine.diagnostics(limit=0), self.diagnostics()[:1])

    def test_a_refused_snapshot_cannot_change_incident_state(self) -> None:
        self.ready()
        self.engine.tick()
        published = self.diagnostics()[0]
        with self.assertRaises(store.IngestRefused):
            self.send_snapshot(
                observed_at=NOW - 100, runs=[run_row("run-1")], received_at=self.wall
            )
        self.assertEqual(self.diagnostics()[0], published)


# --------------------------------------------------------------------------
# Summary-only answers
# --------------------------------------------------------------------------


class SummaryTests(StoreCase):
    """What a summary credential is entitled to: numbers, and no identity."""

    def test_counts_carry_no_identifier(self) -> None:
        self.send_heartbeat(observed_at=NOW - 10)
        self.send_snapshot(
            observed_at=NOW - 10,
            runs=[
                run_row("alpha"),
                run_row(
                    "beta",
                    urgency_group="needs-action",
                    primary_state="paused-for-reply",
                    question_id="q-1",
                    unanswered_count=1,
                ),
            ],
        )
        self.send_history(
            generated_at=NOW - 20, entries=[history_entry("e1", recorded_at=NOW - 20)]
        )
        self.send_catalog(
            observed_at=NOW - 10, generation="gen-1", entries=[catalog_entry("plan-a")]
        )
        document = self.store.counts(now=NOW).as_document()
        self.assertEqual(
            document,
            {
                "runs": 2,
                "needs_attention": 1,
                "running": 1,
                "absent_runs": 0,
                "unconfirmed_runs": 0,
                "history_entries": 1,
                "connectors": 1,
                "plans": 1,
                "absent_plans": 0,
                "unconfirmed_plans": 0,
            },
        )
        for value in document.values():
            self.assertIsInstance(value, int)

    def test_a_status_document_names_its_source_and_nothing_inside_it(self) -> None:
        self.send_heartbeat(observed_at=NOW - 5)
        self.send_snapshot(observed_at=NOW - 5, runs=[run_row("alpha")])
        document = self.store.connector_statuses(now=NOW)[0].as_document()
        self.assertEqual(document["connector_id"], CONNECTOR_ID)
        self.assertEqual(document["freshness"], freshness.FRESHNESS_LIVE)
        self.assertTrue(document["absence_proves_removal"])
        self.assertEqual(set(document) & FORBIDDEN_PERSISTED_KEYS, set())
        self.assertNotIn("run_key", document)

    def test_a_deployment_with_no_source_knows_that_it_knows_nothing(self) -> None:
        self.assertEqual(self.store.connector_statuses(now=NOW), [])
        self.assertEqual(freshness.aggregate_freshness([]), freshness.FRESHNESS_LOST)
        self.assertEqual(self.store.counts(now=NOW).connectors, 0)

    def test_an_unsupported_contract_is_reported_rather_than_ingested_blindly(
        self,
    ) -> None:
        self.send_heartbeat(observed_at=NOW - 5, contract_set_id="asl-amx-99")
        self.send_snapshot(
            observed_at=NOW - 5,
            runs=[run_row("alpha")],
            contract_set_id="asl-amx-99",
        )
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertFalse(status.compatible)
        self.assertEqual(status.freshness.label, freshness.FRESHNESS_INCOMPATIBLE)
        self.assertFalse(status.freshness.replies_enabled)
        # Stored, and never used as incident evidence.
        self.assertEqual(len(self.store.runs(now=NOW)), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
