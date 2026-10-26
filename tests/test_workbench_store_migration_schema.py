"""Migration of an existing companion database, schema boundaries and portability."""

from __future__ import annotations

import ast
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from asl_workbench import (
    incidents,
    observation,
    store_columns,
    store_migrations,
    store_types,
    store_workbench,
)
from asl_workbench.operations import (
    FORBIDDEN_PERSISTED_KEYS,
    OPERATION_PERSIST_FIELDS,
    OperationError,
)
from tests.support_workbench_store import (
    NOW,
    PACKAGE,
    POLICY,
    RETENTION,
    StoreCase,
    catalog_entry,
    catalog_payload,
    companion_database,
    envelope,
    history_entry,
    run_row,
)

ASL_TEST_PORTABLE = True


class _FailingStatement:
    """A connection proxy whose first statement matching ``fail_on`` fails.

    A proxy because :class:`sqlite3.Connection` is a C type whose ``execute``
    cannot be patched on an instance.
    """

    def __init__(self, connection: sqlite3.Connection, fail_on: str) -> None:
        self._connection = connection
        self._fail_on = fail_on
        self.failed = False

    def execute(self, sql: str, *parameters: object) -> sqlite3.Cursor:
        if not self.failed and sql.strip() == self._fail_on:
            self.failed = True
            raise sqlite3.OperationalError("database is locked")
        return self._connection.execute(sql, *parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class TransactionTests(StoreCase):
    """A unit of work that fails at COMMIT leaves the connection reusable."""

    def test_a_failed_commit_rolls_back_and_frees_the_connection(self) -> None:
        real = self.store._connection
        self.store._connection = _FailingStatement(real, "COMMIT")  # type: ignore[assignment]
        self.addCleanup(setattr, self.store, "_connection", real)
        with (
            self.assertRaises(sqlite3.OperationalError),
            self.store.transaction() as connection,
        ):
            connection.execute(
                "INSERT INTO service_meta (key, value) VALUES ('probe', '1')"
            )
        self.assertFalse(real.in_transaction)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO service_meta (key, value) VALUES ('probe', '2')"
            )
        self.assertEqual(
            real.execute(
                "SELECT value FROM service_meta WHERE key = 'probe'"
            ).fetchone()[0],
            "2",
        )

    def test_a_rollback_that_cannot_run_does_not_mask_the_failure(self) -> None:
        real = self.store._connection
        self.store._connection = _FailingStatement(real, "ROLLBACK")  # type: ignore[assignment]
        self.addCleanup(setattr, self.store, "_connection", real)
        with self.assertRaises(KeyError), self.store.transaction():
            raise KeyError("the original failure")
        real.execute("ROLLBACK")


class MigrationTests(unittest.TestCase):
    """The ledger is one integer, and every upgrade keeps what came before."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workbench.sqlite3"

    def test_a_fresh_database_arrives_at_the_declared_version(self) -> None:
        with store_workbench.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ) as opened:
            self.assertEqual(opened.schema_version, store_types.SCHEMA_VERSION)
            self.assertEqual(opened.created_at, NOW)
            self.assertTrue(opened.healthy())

    def test_reopening_migrates_nothing_and_keeps_the_first_creation_time(
        self,
    ) -> None:
        """Restarting is safe because a current database is a no-op."""
        with store_workbench.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ):
            pass
        with store_workbench.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW + 5000
        ) as second:
            self.assertEqual(second.schema_version, store_types.SCHEMA_VERSION)
            self.assertEqual(second.created_at, NOW)

    def test_a_newer_database_is_refused_rather_than_written_through(self) -> None:
        with store_workbench.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ):
            pass
        connection = store_migrations.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute(f"PRAGMA user_version={store_types.SCHEMA_VERSION + 1}")
        with self.assertRaises(store_types.StoreError):
            store_migrations.migrate(connection)

    def test_a_failed_migration_leaves_the_previous_version_intact(self) -> None:
        """Each step commits with its own version bump, so a failure rolls back."""
        connection = store_migrations.connect(self.path)
        self.addCleanup(connection.close)
        broken = (
            *store_migrations.MIGRATIONS[:1],
            (2, ("CREATE TABLE service_meta (x)",)),
        )
        with (
            mock.patch.object(store_migrations, "MIGRATIONS", broken),
            self.assertRaises(sqlite3.OperationalError),
        ):
            store_migrations.migrate(connection, created_at=NOW)
        self.assertEqual(store_migrations.schema_version(connection), 1)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM connector").fetchone()[0], 0
        )

    def test_a_failed_migration_commit_rolls_back_and_can_be_retried(self) -> None:
        connection = store_migrations.connect(self.path)
        self.addCleanup(connection.close)
        flaky = _FailingStatement(connection, "COMMIT")
        with self.assertRaises(sqlite3.OperationalError):
            store_migrations.migrate(flaky, created_at=NOW)  # type: ignore[arg-type]
        self.assertFalse(connection.in_transaction)
        self.assertEqual(store_migrations.schema_version(connection), 0)
        self.assertEqual(
            store_migrations.migrate(connection, created_at=NOW),
            store_types.SCHEMA_VERSION,
        )

    def test_the_inherited_migrations_are_unchanged(self) -> None:
        """Migrations 1 to 3 are the Companion's, so an existing file skips them."""
        versions = [version for version, _ in store_migrations.MIGRATIONS]
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertEqual(store_migrations.MIGRATIONS[2][1], incidents.MIGRATION)
        self.assertEqual(store_types.SCHEMA_VERSION, versions[-1])
        self.assertEqual(store_types.COMPANION_SCHEMA_VERSION, 3)

    def test_the_per_upload_lookups_are_answered_from_an_index(self) -> None:
        """Incident observation and run resolution do not scan their tables."""
        connection = store_migrations.connect(self.path)
        self.addCleanup(connection.close)
        store_migrations.migrate(connection, created_at=NOW)
        lookups = {
            "history_entry_run": (
                "SELECT MAX(recorded_at) FROM history_entry "
                "WHERE connector_id = ? AND run_key = ?",
                ("c", "r"),
            ),
            "history_entry_plan": (
                "SELECT 1 FROM history_entry WHERE connector_id = ? AND plan_key = ? "
                "AND outcome = 'completed' AND recorded_at > ?",
                ("c", "p", 0.0),
            ),
            "run_summary_run_key": (
                "SELECT 1 FROM run_summary WHERE run_key = ? AND present = 1",
                ("r",),
            ),
        }
        for index, (sql, parameters) in lookups.items():
            with self.subTest(index=index):
                plan = " ".join(
                    str(row["detail"])
                    for row in connection.execute(
                        f"EXPLAIN QUERY PLAN {sql}", parameters
                    )
                )
                self.assertIn(index, plan)

    def test_the_index_migration_reapplies_over_an_index_added_by_hand(self) -> None:
        connection = store_migrations.connect(self.path)
        self.addCleanup(connection.close)
        upto_four = store_migrations.MIGRATIONS[:4]
        with mock.patch.object(store_migrations, "MIGRATIONS", upto_four):
            store_migrations.migrate(connection, created_at=NOW)
        connection.execute("CREATE INDEX run_summary_run_key ON run_summary (run_key)")
        self.assertEqual(
            store_migrations.migrate(connection, created_at=NOW),
            store_types.SCHEMA_VERSION,
        )

    def test_an_installed_companion_database_keeps_every_row_and_identifier(
        self,
    ) -> None:
        companion_database(self.path, created_at=NOW - RETENTION)
        with store_workbench.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ) as opened:
            self.assertEqual(opened.schema_version, store_types.SCHEMA_VERSION)
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
        with store_workbench.WorkbenchStore.open(
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

    def test_a_malformed_project_list_carries_no_projects_and_does_not_block(
        self,
    ) -> None:
        companion_database(self.path, created_at=NOW - RETENTION)
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (
                store_types.META_PROJECT_CATALOG_PREFIX + "other-mac",
                '{"number": {"projects": 5}, "word": {"projects": "scripts"}, '
                '"mixed": {"projects": ["scripts", {"a": 1}]}}',
            ),
        )
        connection.commit()
        connection.close()
        with store_workbench.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        ) as opened:
            self.assertEqual(opened.schema_version, store_types.SCHEMA_VERSION)
            with opened.reading() as reading:
                rows = reading.execute(
                    "SELECT plan_key, projects FROM catalog_entry "
                    "WHERE connector_id = 'other-mac' ORDER BY plan_key"
                ).fetchall()
        self.assertEqual(
            [(row["plan_key"], row["projects"]) for row in rows],
            [("mixed", '["scripts"]'), ("number", "[]"), ("word", "[]")],
        )

    def test_a_carried_label_is_retired_by_the_first_complete_generation(
        self,
    ) -> None:
        """A plan the Mac no longer enumerates is genuinely gone."""
        companion_database(self.path, created_at=NOW - RETENTION)
        opened = store_workbench.WorkbenchStore.open(
            self.path, policy=POLICY, created_at=NOW
        )
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
                bookkeeping = store_columns.SCHEMA_BOOKKEEPING[table]
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
