"""Translate legacy configuration, journal records, and checkpoints."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from asl_workbench import migration
from asl_workbench.integrations import PI_SITE
from asl_workbench.operations import (
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_DEADLINE_PASSED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_UNCONFIRMED,
)
from tests.workbench_lifecycle_support import OWNER, TARGET_ID

from .support_workbench_migration import (
    companion_checkpoints,
    companion_connector_configuration,
    companion_journal,
    companion_service_configuration,
)

# Half of it is pure translation, but the rollback half installs two real
# releases and starts them, because "the data a release is handed is the
# data it ran on" cannot be proven against a switch that was faked.
ASL_TEST_LANE = "integration"

# ---------------------------------------------------------------------------
# The translations, with no prefix in sight


class JournalReconciliationTest(unittest.TestCase):
    """What a migrated connector says about operations it cannot account for."""

    def reconciled(self) -> dict[str, dict[str, Any]]:
        records = migration.reconcile_journal(
            companion_journal()["operations"], now=900.0
        )
        return {str(record["operation_id"]): record for record in records}

    def test_an_admitted_operation_that_never_ran_expires(self) -> None:
        # `pending` is the one state where the absence is provable: admission
        # and attempt are separate steps, so nothing was sent.
        record = self.reconciled()["op-pending"]
        self.assertEqual(record["status"], OPERATION_EXPIRED)
        self.assertEqual(record["detail_code"], DETAIL_CONNECTOR_RESTARTED)
        self.assertEqual(record["settled_at"], 900.0)

    def test_an_attempted_operation_becomes_unconfirmed_rather_than_settled(
        self,
    ) -> None:
        # The honest answer, and the one that costs something to give: nobody
        # here can establish that the dispatch did not land, so it is not
        # settled, and `settled_at` stays empty to say so.
        for name in ("op-dispatched", "op-accepted"):
            with self.subTest(operation=name):
                record = self.reconciled()[name]
                self.assertEqual(record["status"], OPERATION_UNCONFIRMED)
                self.assertEqual(record["detail_code"], DETAIL_CONNECTOR_RESTARTED)
                self.assertNotIn("settled_at", record)

    def test_a_settled_operation_keeps_the_outcome_it_reached(self) -> None:
        record = self.reconciled()["op-settled"]
        self.assertEqual(record["status"], OPERATION_EXPIRED)
        self.assertEqual(record["detail_code"], DETAIL_DEADLINE_PASSED)
        self.assertEqual(record["settled_at"], 431.0)

    def test_an_already_unconfirmed_operation_is_not_restamped(self) -> None:
        # It already says "nobody can account for this". Rewriting it would
        # replace whatever explained it with this migration's own reason.
        record = self.reconciled()["op-unconfirmed"]
        self.assertEqual(record["status"], OPERATION_UNCONFIRMED)
        self.assertEqual(record["requested_at"], 140.0)

    def test_no_reconciled_record_carries_anything_that_could_be_resent(
        self,
    ) -> None:
        # The claim the whole reconciliation rests on. Both open records went
        # in carrying a reply, and what comes out has no field to put one in --
        # which is why "the migration cannot repeat an action" is a property of
        # the record rather than a promise about the code that writes it.
        for record in self.reconciled().values():
            self.assertNotIn("reply", record)
            self.assertNotIn("body", record)
            self.assertNotIn("action_token", record)

    def test_a_record_this_package_cannot_read_is_dropped(self) -> None:
        # A journal is a file on a host, so the entry that is not a record at
        # all is part of the input: annotated `Any` because that is what this
        # asserts the migration survives.
        unreadable: list[Any] = [
            {"status": OPERATION_PENDING},
            {"operation_id": "op-x", "status": "invented"},
            {"operation_id": "", "status": OPERATION_PENDING},
            "not a record",
        ]
        self.assertEqual(migration.reconcile_journal(unreadable, now=900.0), [])

    def test_a_journal_from_another_schema_migrates_to_an_empty_one(self) -> None:
        # The Companion's own posture. The operations such a file describes are
        # certainly over by now, and refusing to migrate would strand an
        # installation on a file nobody can read.
        migrated = migration.migrate_journal({"version": 99, "operations": []}, now=1.0)
        self.assertEqual(migrated["operations"], [])
        self.assertEqual(migrated["version"], migration.JOURNAL_SCHEMA_VERSION)


class ConfigurationTranslationTest(unittest.TestCase):
    """The two configurations, and what the translation is allowed to change."""

    def test_the_connector_trades_its_harness_list_for_a_target(self) -> None:
        legacy = companion_connector_configuration(
            Path(tempfile.mkdtemp(prefix="workbench-config-"))
        )
        migrated = migration.migrate_connector_configuration(
            legacy, target_id=TARGET_ID
        )
        self.assertEqual(set(migrated), set(migration.CONNECTOR_KEYS))
        self.assertEqual(migrated["target_id"], TARGET_ID)
        self.assertNotIn("harnesses", migrated)
        self.assertEqual(migrated["connector_id"], legacy["connector_id"])

    def test_a_connector_migration_refuses_to_invent_a_target(self) -> None:
        # Nothing on disk names one, and the transport hello is judged against
        # it: a guess here would attach this Mac to a service nobody named.
        legacy = companion_connector_configuration(
            Path(tempfile.mkdtemp(prefix="workbench-config-"))
        )
        with self.assertRaises(migration.MigrationError):
            migration.migrate_connector_configuration(legacy, target_id="")

    def test_a_connector_configuration_with_an_unknown_field_is_refused(self) -> None:
        legacy = {
            **companion_connector_configuration(
                Path(tempfile.mkdtemp(prefix="workbench-config-"))
            ),
            "surprise": True,
        }
        with self.assertRaises(migration.MigrationError):
            migration.migrate_connector_configuration(legacy, target_id=TARGET_ID)

    def test_the_service_is_made_to_say_what_it_used_to_leave_unsaid(self) -> None:
        # The Companion defaulted to permitting mutations and this package
        # defaults to refusing them, so an omitted field would change meaning
        # in the direction nobody would notice until a reply was refused.
        migrated = migration.migrate_service_configuration(
            companion_service_configuration(), site=PI_SITE
        )
        self.assertIs(migrated["settings"]["observation_only"], True)
        self.assertEqual(migrated["settings"]["owner_identity"], OWNER)
        self.assertNotIn("fixture_mode", migrated["settings"])

    def test_a_service_that_permitted_mutations_keeps_permitting_them(self) -> None:
        legacy = companion_service_configuration()
        legacy["settings"]["observation_only"] = False
        migrated = migration.migrate_service_configuration(legacy, site=PI_SITE)
        self.assertIs(migrated["settings"]["observation_only"], False)

    def test_a_configuration_naming_another_endpoint_is_refused(self) -> None:
        # Refused rather than rewritten: a prefix configured for a different
        # origin is a deployment this migration was not shown, and pointing it
        # at this site would move a running service.
        legacy = companion_service_configuration()
        legacy["settings"]["public_origin"] = "https://elsewhere.example:9443"
        with self.assertRaises(migration.MigrationError):
            migration.migrate_service_configuration(legacy, site=PI_SITE)

    def test_a_service_without_an_owner_is_refused(self) -> None:
        legacy = companion_service_configuration()
        del legacy["settings"]["owner_identity"]
        with self.assertRaises(migration.MigrationError):
            migration.migrate_service_configuration(legacy, site=PI_SITE)


class CheckpointCarryTest(unittest.TestCase):
    """The history window, which is worth a week of re-reading if it is lost."""

    def test_the_legacy_document_carries_every_field_it_had(self) -> None:
        migrated = migration.migrate_checkpoints(companion_checkpoints())
        source = migrated["sources"][0]
        self.assertEqual(source["source_key"], "plan-a")
        self.assertEqual(source["offset"], 8192)
        self.assertEqual(source["head_digest"], "c0ffee")
        self.assertEqual(source["delivered"], ["run-0"])
        self.assertEqual(source["open_questions"], [])

    def test_a_document_from_another_schema_starts_the_window_again(self) -> None:
        migrated = migration.migrate_checkpoints({"version": 99, "sources": []})
        self.assertEqual(migrated["sources"], [])


if __name__ == "__main__":
    unittest.main()
