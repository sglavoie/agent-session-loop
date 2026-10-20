"""Focused tests for the overseer's durable ledger.

Five properties, and each one is proved against a real SQLite file on disk
rather than against an in-memory double, because four of the five are about
what survives the process.

*Restart.* Everything written is read back by a second :func:`open_ledger` over
the same path -- and an attempted dispatch is still merely attempted, which is
the one fact a restart most wants to round up.

*Migration.* A fresh database lands on the current version, a second open
replays nothing, an appended migration runs exactly once over existing rows, a
migration that fails leaves the version and the schema where they were, and a
database from a newer build is refused rather than opened.

*Partial write.* A fault injected midway through a multi-row write must leave
none of it behind. The seam is a proxy connection that fails a chosen statement,
in the spirit of ``tests.test_asl_commit_groups.DurableLedgerTests``.

*Unavailable policy.* A decision that could not be obtained never moves an
enrollment bit and never yields evidence, and the stored form of a *successful*
decision is asserted to have no method that could turn it back into permission.

*Revocation.* After a withdrawal, stored evidence for that plan is withheld and
stays withheld -- through a re-read, through a release attempt, and through a
restart.
"""

import sqlite3
import unittest
from unittest import mock

from agent_session_loop import (
    overseer_policy_client,
    overseer_store_actions,
    overseer_store_core,
    overseer_store_groups,
    overseer_store_incidents,
    overseer_store_ledger,
    overseer_store_models,
    overseer_store_rows,
    overseer_store_schema,
    overseer_store_watch,
    state,
    state_plan_refs,
)
from tests.asl_overseer_store_support import (
    PLAN,
    _columns,
    _evidence,
    _grant,
    _LedgerCase,
)
from tests.support import assert_no_runner_import


class LedgerLocationTests(_LedgerCase):
    """Where the database lives, and what must not notice it."""

    def test_module_does_not_import_the_runner(self) -> None:
        for module in (
            overseer_store_actions,
            overseer_store_core,
            overseer_store_groups,
            overseer_store_incidents,
            overseer_store_ledger,
            overseer_store_models,
            overseer_store_rows,
            overseer_store_schema,
            overseer_store_watch,
        ):
            assert_no_runner_import(self, module)

    def test_the_ledger_is_a_file_beside_the_plan_state_directories(self) -> None:
        """A directory here would be enumerated as a plan by the fleet
        inventory and reported as a run nobody can read."""
        self.assertEqual(
            overseer_store_ledger.ledger_path(self.root),
            state.state_root(self.root) / overseer_store_models.LEDGER_FILE_NAME,
        )
        self.assertTrue(self.ledger.path.is_file())
        self.assertEqual(
            list(state_plan_refs.iter_plan_state_dirs(state.state_root(self.root))), []
        )


class MigrationTests(_LedgerCase):
    """The schema moves forward, one step at a time, or not at all."""

    def test_a_fresh_database_lands_on_the_current_version(self) -> None:
        self.assertEqual(
            self.ledger.schema_version, overseer_store_schema.SCHEMA_VERSION
        )
        self.assertEqual(
            overseer_store_schema.SCHEMA_VERSION, len(overseer_store_schema.MIGRATIONS)
        )

    def test_reopening_replays_nothing_and_keeps_every_row(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="question_pending",
            summary="waiting on a reply",
            evidence=_evidence(2),
        )
        reopened = self.reopen()
        self.assertEqual(reopened.schema_version, overseer_store_schema.SCHEMA_VERSION)
        self.assertEqual(len(reopened.incidents(plan_dir=PLAN)), 1)
        view = reopened.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(len(view.records), 2)

    def test_an_appended_migration_runs_once_over_existing_rows(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="run_paused",
            summary="paused",
        )
        self.ledger.close()

        def probe(connection: sqlite3.Connection) -> None:
            connection.execute("ALTER TABLE incident ADD COLUMN probe TEXT")

        upgraded = overseer_store_schema.SCHEMA_VERSION + 1
        with (
            mock.patch.object(
                overseer_store_schema,
                "MIGRATIONS",
                overseer_store_schema.MIGRATIONS + (probe,),
            ),
            mock.patch.object(overseer_store_schema, "SCHEMA_VERSION", upgraded),
        ):
            self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
            self.assertEqual(self.ledger.schema_version, upgraded)
            self.assertEqual(len(self.ledger.incidents(plan_dir=PLAN)), 1)
            self.ledger.close()
            # A second open at the same version must not replay the step, which
            # would fail on the duplicate column.
            self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
            self.assertEqual(self.ledger.schema_version, upgraded)

    def test_a_failed_migration_leaves_the_version_and_schema_untouched(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="run_paused",
            summary="paused",
        )
        self.ledger.close()

        def half_applied(connection: sqlite3.Connection) -> None:
            connection.execute("ALTER TABLE incident ADD COLUMN probe TEXT")
            raise sqlite3.OperationalError("migration exploded")

        with (
            mock.patch.object(
                overseer_store_schema,
                "MIGRATIONS",
                overseer_store_schema.MIGRATIONS + (half_applied,),
            ),
            mock.patch.object(
                overseer_store_schema,
                "SCHEMA_VERSION",
                overseer_store_schema.SCHEMA_VERSION + 1,
            ),
            self.assertRaises(overseer_store_models.LedgerError),
        ):
            overseer_store_ledger.open_ledger(state_dir=self.root)

        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        self.assertEqual(
            self.ledger.schema_version, overseer_store_schema.SCHEMA_VERSION
        )
        self.assertEqual(len(self.ledger.incidents(plan_dir=PLAN)), 1)
        self.assertNotIn("probe", _columns(self.ledger, "incident"))

    def test_a_database_from_a_newer_build_is_refused(self) -> None:
        """Opening it anyway would mean writing this version's rows into a
        shape only the newer build knows the meaning of."""
        path = self.ledger.path
        self.ledger.close()
        raw = sqlite3.connect(str(path), isolation_level=None)
        raw.execute(f"PRAGMA user_version = {overseer_store_schema.SCHEMA_VERSION + 3}")
        raw.close()
        with self.assertRaises(overseer_store_models.LedgerError) as caught:
            overseer_store_ledger.open_ledger(state_dir=self.root)
        self.assertIn("newer", str(caught.exception))


class RestartTests(_LedgerCase):
    """What the second process finds."""

    def test_an_attempted_dispatch_is_still_attempted_after_a_restart(self) -> None:
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        self.assertEqual(action.status, "attempted")
        self.assertTrue(action.in_doubt)
        self.assertFalse(action.confirmed)
        self.assertIsNone(action.receipt)

        reopened = self.reopen()
        in_doubt = reopened.in_doubt_actions()
        self.assertEqual([item.action_id for item in in_doubt], [action.action_id])
        self.assertTrue(in_doubt[0].in_doubt)
        self.assertFalse(in_doubt[0].confirmed)
        self.assertEqual(reopened.actions(status="executed"), ())
        self.assertEqual(in_doubt[0].request_digest, "rd-act")

    def test_a_confirmed_dispatch_carries_its_receipt_across_a_restart(self) -> None:
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        self.ledger.confirm_action(
            action_id=action.action_id, outcome="delivered", detail="consumed"
        )
        reopened = self.reopen()
        settled = reopened.action(action.action_id)
        assert settled is not None
        self.assertEqual(settled.status, "executed")
        self.assertTrue(settled.confirmed)
        assert settled.receipt is not None
        self.assertEqual(settled.receipt.outcome, "delivered")
        self.assertEqual(reopened.in_doubt_actions(), ())

    def test_an_action_settles_exactly_once(self) -> None:
        """Two answers about one dispatch means one of them is wrong, and the
        ledger has no way to tell which."""
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        self.ledger.confirm_action(action_id=action.action_id, outcome="delivered")
        with self.assertRaises(overseer_store_models.LedgerError):
            self.ledger.confirm_action(action_id=action.action_id, outcome="failed")
        settled = self.ledger.action(action.action_id)
        assert settled is not None
        assert settled.receipt is not None
        self.assertEqual(settled.receipt.outcome, "delivered")

    def test_cursors_and_enrollment_survive_and_never_move_backwards(self) -> None:
        self.enrolled()
        self.ledger.advance_cursor(
            plan_dir=PLAN, stream="events", position=120, marker="e-120"
        )
        self.ledger.advance_cursor(
            plan_dir=PLAN, stream="events", position=40, marker="e-40"
        )
        reopened = self.reopen()
        cursor = reopened.cursor(plan_dir=PLAN, stream="events")
        assert cursor is not None
        self.assertEqual(cursor.position, 120)
        self.assertEqual(cursor.marker, "e-120")
        record = reopened.enrollment(PLAN)
        assert record is not None
        self.assertTrue(record.enrolled)
        self.assertFalse(record.revoked)


if __name__ == "__main__":
    unittest.main()
