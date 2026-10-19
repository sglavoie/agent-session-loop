"""Preserve service and connector identities when migrating installed prefixes."""

from __future__ import annotations

import shutil
import unittest
from contextlib import closing

from asl_workbench import history, lifecycle, migration, store_migrations, store_types
from asl_workbench.installation import (
    BACKUPS_DIRECTORY,
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_PATH,
    RECEIPTS_DIRECTORY,
    SECRETS_PATH,
    InstallationError,
    document,
)
from asl_workbench.integrations import PI_SITE, service_endpoint
from asl_workbench.operations import (
    OPERATION_CONSUMED,
    OPERATION_EXPIRED,
    OPERATION_UNCONFIRMED,
)
from tests.workbench_lifecycle_support import CONNECTOR_ID, OWNER, TARGET_ID

from .support_workbench_migration import (
    ENTRY_ID,
    INCIDENT_ID,
    OPERATION_ID,
    PLAN_KEY,
    PLAN_LABEL,
    PLAN_TITLE,
    PROJECT,
    RUN_KEY,
    UPLOADER,
    LegacyPrefixTestCase,
    keys_in,
    receipt,
)

# ---------------------------------------------------------------------------
# The migration itself


class ServiceMigrationTest(LegacyPrefixTestCase):
    """The Pi's prefix, moved onto this package."""

    def test_a_plan_names_every_step_and_takes_none_of_them(self) -> None:
        prefix = self.service_prefix()
        before = (prefix / CONFIG_PATH).read_bytes()
        plan = self.migration().plan()
        self.assertEqual(
            plan["steps"],
            [
                migration.STEP_DATABASE,
                migration.STEP_CONFIGURATION,
                migration.STEP_RECEIPTS,
            ],
        )
        self.assertEqual(plan["role"], "service")
        self.assertTrue((prefix / migration.LEGACY_DATABASE_PATH).is_file())
        self.assertEqual((prefix / CONFIG_PATH).read_bytes(), before)
        self.assertFalse((prefix / DATABASE_PATH).exists())

    def test_the_observations_move_to_the_name_every_release_opens(self) -> None:
        prefix = self.service_prefix()
        self.migration().apply()
        self.assertTrue((prefix / DATABASE_PATH).is_file())
        self.assertFalse((prefix / migration.LEGACY_DATABASE_PATH).exists())

    def test_every_identity_that_was_there_is_the_one_that_comes_out(self) -> None:
        # The whole risk of this change in one test. Run keys are published in
        # notification links, incident ids are what a resolved incident is
        # matched against, and an upgrade that recomputed either would break
        # every link the Pi has sent and split every open incident in two.
        prefix = self.service_prefix()
        self.migration().apply()
        with closing(store_migrations.connect(prefix / DATABASE_PATH)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT run_key, plan_key, connector_id FROM run_summary"
                ).fetchall()[0][:],
                (RUN_KEY, PLAN_KEY, UPLOADER),
            )
            self.assertEqual(
                connection.execute("SELECT entry_id FROM history_entry").fetchone()[0],
                ENTRY_ID,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT incident_id, run_key FROM incident"
                ).fetchone()[:],
                (INCIDENT_ID, RUN_KEY),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT operation_id, status FROM operation"
                ).fetchone()[:],
                (OPERATION_ID, OPERATION_CONSUMED),
            )

    def test_the_database_arrives_at_this_releases_schema(self) -> None:
        prefix = self.service_prefix()
        self.migration().apply()
        with closing(store_migrations.connect(prefix / DATABASE_PATH)) as connection:
            self.assertEqual(
                store_migrations.schema_version(connection), store_types.SCHEMA_VERSION
            )

    def test_the_plan_labels_an_operator_saw_survive_the_move(self) -> None:
        # Carried by the schema migration rather than by this module, which is
        # the point: the rows go through the store's own upgrade path, so
        # months of history keep the names they were shown under.
        prefix = self.service_prefix()
        self.migration().apply()
        with closing(store_migrations.connect(prefix / DATABASE_PATH)) as connection:
            row = connection.execute(
                "SELECT display_label, title, project_group FROM catalog_entry "
                "WHERE plan_key = ?",
                (PLAN_KEY,),
            ).fetchone()
        self.assertEqual(row["display_label"], PLAN_LABEL)
        self.assertEqual(row["title"], PLAN_TITLE)
        self.assertEqual(row["project_group"], PROJECT)

    def test_the_migrated_configuration_is_one_the_service_can_read(self) -> None:
        prefix = self.service_prefix()
        self.migration().apply()
        endpoint = service_endpoint(prefix / CONFIG_PATH, PI_SITE)
        self.assertEqual(endpoint.owner_identity, OWNER)
        self.assertIs(endpoint.observation_only, True)

    def test_the_credentials_are_not_touched(self) -> None:
        # They cannot be re-provisioned: the connector token exists on this
        # host only as a digest. A migration that rewrote this file would be a
        # migration that silently re-paired the Mac.
        prefix = self.service_prefix()
        before = (prefix / SECRETS_PATH).read_bytes()
        token = (prefix / CONNECTOR_TOKEN_PATH).read_bytes()
        self.migration().apply()
        self.assertEqual((prefix / SECRETS_PATH).read_bytes(), before)
        self.assertEqual((prefix / CONNECTOR_TOKEN_PATH).read_bytes(), token)

    def test_the_receipts_come_through_unchanged(self) -> None:
        prefix = self.service_prefix()
        self.migration().apply()
        self.assertEqual(
            document(prefix / RECEIPTS_DIRECTORY / "route.json"), receipt("route")
        )

    def test_a_receipt_this_package_cannot_read_stops_the_migration(self) -> None:
        # Found now, while the backup is one step away, rather than at the
        # first integration run against a host the receipt describes.
        prefix = self.service_prefix()
        (prefix / RECEIPTS_DIRECTORY / "route.json").write_bytes(b"[]")
        with self.assertRaises(InstallationError):
            self.migration().apply()
        self.assertTrue((prefix / migration.LEGACY_DATABASE_PATH).is_file())

    def test_the_migration_backs_the_whole_prefix_up_first(self) -> None:
        prefix = self.service_prefix()
        result = self.migration().apply()
        source = prefix / BACKUPS_DIRECTORY / str(result["backup"])
        self.assertIn(migration.LEGACY_DATABASE_PATH, migration.verify(source))
        self.assertEqual(keys_in(source / migration.LEGACY_DATABASE_PATH), {RUN_KEY})

    def test_migrating_an_already_migrated_prefix_changes_nothing(self) -> None:
        # What makes running the command twice safe, and what makes "did this
        # host get migrated?" answerable without reading the backup directory.
        prefix = self.service_prefix()
        self.migration().apply()
        digest = (prefix / DATABASE_PATH).read_bytes()
        result = self.migration().apply()
        self.assertFalse(result["migrated"])
        self.assertEqual(result["steps"], [])
        self.assertEqual((prefix / DATABASE_PATH).read_bytes(), digest)

    def test_a_prefix_carrying_both_databases_is_refused(self) -> None:
        # Two databases and no way to tell which one an operator meant. The
        # recovery path exists precisely so nobody has to guess here.
        prefix = self.service_prefix()
        shutil.copy(prefix / migration.LEGACY_DATABASE_PATH, prefix / DATABASE_PATH)
        with self.assertRaises(migration.MigrationError):
            self.migration().apply()


class ConnectorMigrationTest(LegacyPrefixTestCase):
    """The Mac's prefix, which keeps different things in different files."""

    def test_the_issued_token_moves_to_the_file_this_package_reads(self) -> None:
        # One name for two documents is how a connector prefix ends up being
        # read as a half-provisioned service.
        prefix = self.connector_prefix()
        self.migration(target_id=TARGET_ID).apply()
        self.assertEqual(
            document(prefix / CONNECTOR_TOKEN_PATH), {"connector_token": "a" * 43}
        )
        self.assertFalse((prefix / SECRETS_PATH).exists())

    def test_the_migrated_connector_configuration_is_one_the_attachment_reads(
        self,
    ) -> None:
        prefix = self.connector_prefix()
        self.migration(target_id=TARGET_ID).apply()
        settings = lifecycle.connector_settings(prefix)
        self.assertEqual(settings["target_id"], TARGET_ID)
        self.assertEqual(settings["connector_id"], CONNECTOR_ID)

    def test_the_journal_comes_out_reconciled(self) -> None:
        prefix = self.connector_prefix()
        self.migration(target_id=TARGET_ID).apply()
        journal = document(prefix / migration.JOURNAL_PATH)
        statuses = {
            str(record["operation_id"]): record["status"]
            for record in journal["operations"]
        }
        self.assertEqual(statuses["op-pending"], OPERATION_EXPIRED)
        self.assertEqual(statuses["op-dispatched"], OPERATION_UNCONFIRMED)
        self.assertEqual(statuses["op-accepted"], OPERATION_UNCONFIRMED)
        self.assertEqual(journal["version"], migration.JOURNAL_SCHEMA_VERSION)

    def test_the_migrated_journal_carries_nothing_that_could_be_resent(self) -> None:
        prefix = self.connector_prefix()
        self.migration(target_id=TARGET_ID).apply()
        raw = (prefix / migration.JOURNAL_PATH).read_bytes()
        self.assertNotIn(b"proceed with the migration", raw)
        self.assertNotIn(b"token-abc", raw)

    def test_the_history_window_survives(self) -> None:
        prefix = self.connector_prefix()
        self.migration(target_id=TARGET_ID).apply()
        checkpoints = history.checkpoints_from_document(
            document(prefix / migration.CHECKPOINT_PATH)
        )
        self.assertEqual(checkpoints["plan-a"].offset, 8192)
        self.assertEqual(checkpoints["plan-a"].delivered, ("run-0",))

    def test_reconciling_a_reconciled_journal_settles(self) -> None:
        # The connector's half of "running this twice is safe". Both documents
        # keep their names across the migration, so the second run can only
        # tell there is nothing to do by migrating them and finding the same
        # thing back -- and an unconfirmed record that got re-stamped every
        # time would make that never settle.
        prefix = self.connector_prefix()
        self.migration(target_id=TARGET_ID).apply()
        journal = (prefix / migration.JOURNAL_PATH).read_bytes()
        result = self.migration(target_id=TARGET_ID).apply()
        self.assertFalse(result["migrated"])
        self.assertEqual(result["steps"], [])
        self.assertEqual((prefix / migration.JOURNAL_PATH).read_bytes(), journal)

    def test_a_connector_migration_without_a_target_refuses(self) -> None:
        self.connector_prefix()
        with self.assertRaises(migration.MigrationError):
            self.migration().apply()


if __name__ == "__main__":
    unittest.main()
