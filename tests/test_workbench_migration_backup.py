"""Backup integrity, interrupted migration recovery, and migration commands."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from asl_workbench import lifecycle, migration
from asl_workbench.installation import (
    BACKUPS_DIRECTORY,
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_PATH,
    RECEIPTS_DIRECTORY,
    SECRETS_PATH,
    SUMMARY_TOKEN_PATH,
    document,
    write_json,
)
from tests.workbench_lifecycle_support import TARGET_ID

from .support_workbench_migration import (
    RUN_KEY,
    LegacyPrefixTestCase,
    companion_service_configuration,
    keys_in,
    receipt,
    revoke_everything,
    stored_credentials,
)

ASL_TEST_LANE = "integration"

# ---------------------------------------------------------------------------
# Backups


class BackupTest(LegacyPrefixTestCase):
    """What one backup covers, and what a restore refuses to put back."""

    def test_a_backup_covers_the_receipts_as_well_as_the_state(self) -> None:
        # The expansion this module exists to make. The Companion's snapshot
        # covered three files, and a restore that rebuilt a database beside
        # receipts describing another era's host resources would be a restore
        # of half a system.
        prefix = self.service_prefix()
        manifest = migration.backup(prefix, prefix / BACKUPS_DIRECTORY / "one")
        self.assertEqual(
            set(manifest["files"]),
            {
                CONFIG_PATH,
                SECRETS_PATH,
                CONNECTOR_TOKEN_PATH,
                SUMMARY_TOKEN_PATH,
                migration.LEGACY_DATABASE_PATH,
                f"{RECEIPTS_DIRECTORY}/ntfy.json",
                f"{RECEIPTS_DIRECTORY}/route.json",
            },
        )
        self.assertEqual(
            migration.verify(prefix / BACKUPS_DIRECTORY / "one"), manifest["files"]
        )

    def test_the_backed_up_database_is_a_database(self) -> None:
        # Copied through SQLite rather than as bytes, so the copy is a moment
        # that existed. A file copy of an open database is not.
        prefix = self.service_prefix()
        migration.backup(prefix, prefix / BACKUPS_DIRECTORY / "one")
        copied = prefix / BACKUPS_DIRECTORY / "one" / migration.LEGACY_DATABASE_PATH
        self.assertEqual(keys_in(copied), {RUN_KEY})

    def test_a_member_that_changed_since_the_manifest_is_refused(self) -> None:
        prefix = self.service_prefix()
        source = prefix / BACKUPS_DIRECTORY / "one"
        migration.backup(prefix, source)
        write_json(source / CONFIG_PATH, {"role": "service", "settings": {}})
        with self.assertRaises(migration.MigrationError):
            migration.verify(source)

    def test_a_file_the_manifest_does_not_name_is_refused(self) -> None:
        # Both directions, because a directory somebody has added to is not a
        # backup anything vouched for.
        prefix = self.service_prefix()
        source = prefix / BACKUPS_DIRECTORY / "one"
        migration.backup(prefix, source)
        write_json(source / "extra.json", {"added": True})
        with self.assertRaises(migration.MigrationError):
            migration.verify(source)

    def test_a_backup_will_not_be_written_over_an_existing_one(self) -> None:
        prefix = self.service_prefix()
        migration.backup(prefix, prefix / BACKUPS_DIRECTORY / "one")
        with self.assertRaises(migration.MigrationError):
            migration.backup(prefix, prefix / BACKUPS_DIRECTORY / "one")

    def test_restoring_puts_back_a_receipt_that_was_discarded(self) -> None:
        prefix = self.service_prefix()
        source = prefix / BACKUPS_DIRECTORY / "one"
        migration.backup(prefix, source)
        (prefix / RECEIPTS_DIRECTORY / "route.json").unlink()
        report = migration.restore(source, prefix)
        self.assertIn(f"{RECEIPTS_DIRECTORY}/route.json", report["restored"])
        self.assertEqual(
            document(prefix / RECEIPTS_DIRECTORY / "route.json"), receipt("route")
        )

    def test_restoring_never_writes_a_credential_store_back(self) -> None:
        # The one member a backup does not put back. Revocation is a decision
        # taken after the backup was made, and a restore is not a reason to
        # undo it -- so the live document stands and the member is reported as
        # preserved rather than quietly skipped.
        prefix = self.service_prefix()
        source = prefix / BACKUPS_DIRECTORY / "one"
        migration.backup(prefix, source)
        revoke_everything(prefix / SECRETS_PATH)
        report = migration.restore(source, prefix, credentials=True)
        self.assertIn(SECRETS_PATH, report["preserved"])
        self.assertNotIn(SECRETS_PATH, report["restored"])
        self.assertTrue(
            all(item.disabled for item in stored_credentials(prefix / SECRETS_PATH))
        )


class InterruptedMigrationTest(LegacyPrefixTestCase):
    """The half-finished cases, which are the reason any of this is staged."""

    def interrupt(self, steps: tuple[str, ...]) -> Path:
        """Leave the prefix exactly where a killed process would leave it.

        The prologue of :meth:`~asl_workbench.migration.Migration.apply` minus
        its unwinding, built from that method's own pieces so the reconstructed
        crash cannot drift from the real one.
        """
        instance = self.migration(target_id=TARGET_ID)
        plan = instance.plan()
        destination = self.prefix / BACKUPS_DIRECTORY / "interrupted"
        write_json(
            self.prefix / migration.MIGRATION_PATH,
            {**plan, "backup": destination.name, "started_at": 0.0},
        )
        migration.backup(self.prefix, destination)
        for step in steps:
            getattr(instance, f"_{step}")()
        return destination

    def test_a_step_that_fails_puts_the_whole_prefix_back(self) -> None:
        prefix = self.service_prefix()

        class Failing(migration.Migration):
            def _receipts(self) -> None:
                raise RuntimeError("interrupted")

        with self.assertRaises(RuntimeError):
            Failing(prefix).apply()
        self.assertTrue((prefix / migration.LEGACY_DATABASE_PATH).is_file())
        self.assertFalse((prefix / DATABASE_PATH).exists())
        self.assertEqual(
            document(prefix / CONFIG_PATH), companion_service_configuration()
        )
        self.assertFalse((prefix / migration.MIGRATION_PATH).exists())

    def test_a_prefix_left_mid_migration_recovers_to_the_legacy_layout(self) -> None:
        # The killed-process case: the database moved and nothing undid it. The
        # recovery has to remove what the migration created as well as put back
        # what it replaced, or neither application will start.
        prefix = self.service_prefix()
        self.interrupt((migration.STEP_DATABASE,))
        self.assertTrue((prefix / DATABASE_PATH).is_file())
        result = self.migration().recover()
        self.assertTrue(result["recovered"])
        self.assertIn(DATABASE_PATH, result["removed"])
        self.assertFalse((prefix / DATABASE_PATH).exists())
        self.assertEqual(keys_in(prefix / migration.LEGACY_DATABASE_PATH), {RUN_KEY})
        self.assertFalse((prefix / migration.MIGRATION_PATH).exists())

    def test_recovering_twice_is_not_an_error(self) -> None:
        self.service_prefix()
        self.interrupt((migration.STEP_DATABASE,))
        self.migration().recover()
        self.assertFalse(self.migration().recover()["recovered"])

    def test_a_migration_refuses_while_an_interrupted_one_stands(self) -> None:
        # Two migrations over one prefix, the second of which would back up the
        # first one's half-finished work as though it were the original.
        self.service_prefix()
        self.interrupt((migration.STEP_DATABASE,))
        with self.assertRaises(migration.MigrationError):
            self.migration().apply()

    def test_a_recovered_prefix_migrates_cleanly_on_the_next_attempt(self) -> None:
        prefix = self.service_prefix()
        self.interrupt((migration.STEP_DATABASE,))
        self.migration().recover()
        self.assertTrue(self.migration().apply()["migrated"])
        self.assertEqual(keys_in(prefix / DATABASE_PATH), {RUN_KEY})

    def test_the_installations_recovery_finishes_a_migration_too(self) -> None:
        # An operator who has been told "run recover" should not have to know
        # which of the two things was interrupted.
        prefix = self.service_prefix()
        self.interrupt((migration.STEP_DATABASE,))
        self.assertTrue(lifecycle.Installation(prefix).status()["migration_pending"])
        lifecycle.Installation(prefix).recover()
        self.assertFalse((prefix / DATABASE_PATH).exists())
        self.assertFalse(lifecycle.Installation(prefix).status()["migration_pending"])

    def test_the_interrupted_connector_keeps_its_issued_token(self) -> None:
        # The credential rule from the other direction: the token moved, the
        # move was undone, and what came back is the token -- not a re-issued
        # one, which could not exist, and not nothing.
        prefix = self.connector_prefix()
        self.interrupt((migration.STEP_CREDENTIALS,))
        self.migration().recover()
        self.assertEqual(document(prefix / SECRETS_PATH), {"connector_token": "a" * 43})
        self.assertFalse((prefix / CONNECTOR_TOKEN_PATH).exists())


class MigrationCommandTest(LegacyPrefixTestCase):
    """The operator-facing half: one action, and a dry run that is the plan."""

    def command(self, *arguments: str) -> dict[str, Any]:
        return lifecycle.run(lifecycle.build_parser().parse_args(list(arguments)))

    def test_a_dry_run_prints_the_plan_and_changes_nothing(self) -> None:
        prefix = self.service_prefix()
        plan = self.command("migrate", "--prefix", str(prefix), "--dry-run")
        self.assertIn(migration.STEP_DATABASE, plan["steps"])
        self.assertTrue((prefix / migration.LEGACY_DATABASE_PATH).is_file())

    def test_the_action_migrates(self) -> None:
        prefix = self.service_prefix()
        result = self.command("migrate", "--prefix", str(prefix))
        self.assertTrue(result["migrated"])
        self.assertTrue((prefix / DATABASE_PATH).is_file())


if __name__ == "__main__":
    unittest.main()
