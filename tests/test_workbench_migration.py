"""Moving an installed Companion onto this package, and rolling one back.

Every other module in this suite starts from a prefix this package provisioned.
This one starts from the prefix somebody else's code left on a host months ago
-- the Companion's layout, its database at the schema it stopped at, its issued
token in the file it put it in -- because that is the only input the migration
will ever actually be given, and a fixture built by the code under test would
prove the migration can read what it writes rather than what it will find.

So the legacy databases here are built by replaying this package's own
migrations up to :data:`~asl_workbench.store.COMPANION_SCHEMA_VERSION` and
inserting rows with SQL. That is the Companion's schema, not an imitation of
it: migration 3 is the shared one, and the rows go in under the column names
the Pi wrote them under. What the tests then assert is mostly a negative -- the
run keys, entry ids and incident ids that come out are the ones that went in --
because the whole risk of this change is a migration that quietly rebuilds an
identity somebody has already been shown.

The rollback half is an integration test for the ordinary reason: it installs
two real releases, starts them, writes observations between the two installs,
and rolls back. A fake switch would not prove that the state a release is
handed is the state it ran on, which is the entire claim.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from asl_workbench import history, lifecycle, migration, store
from asl_workbench.credentials import (
    ROLE_CONNECTOR,
    Credential,
    CredentialStore,
    issue,
)
from asl_workbench.installation import (
    BACKUPS_DIRECTORY,
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_FILE,
    DATABASE_PATH,
    PRIVATE_FILE_MODE,
    RECEIPTS_DIRECTORY,
    SECRETS_PATH,
    STATE_DIRECTORY,
    SUMMARY_TOKEN_PATH,
    InstallationError,
    document,
    write_json,
)
from asl_workbench.integrations import PI_SITE, provision, service_endpoint
from asl_workbench.operations import (
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_DEADLINE_PASSED,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_UNCONFIRMED,
)
from tests.test_workbench_lifecycle import (
    ASL_EXECUTABLE,
    CONNECTOR_ID,
    OWNER,
    TARGET_ID,
    InstallationTestCase,
)

#: The connector whose observations the legacy database holds. Not the same
#: string as the connector this prefix would provision: the rows were uploaded
#: by a Mac that has been running for months, and an identity test that used
#: one name for both would pass for the wrong reason.
UPLOADER = "mac-companion"

PLAN_KEY = "plan-3f2a1c"
RUN_KEY = "run-9b8c7d"
ENTRY_ID = "entry-441"
INCIDENT_ID = "incident-88ec"
OPERATION_ID = "operation-0f21"
PROJECT = "Raspberry Pi"

#: What an operator saw the plan called, in the blob the Companion kept it in.
#: It is carried into ``catalog_entry`` by the schema migration, and it is here
#: because "the labels survived" is the difference between a migrated history
#: and a page of runs headed "Unknown project".
PLAN_LABEL = "pi-monitoring"
PLAN_TITLE = "Pi monitoring rollout"


def companion_database(path: Path) -> None:
    """One database at the schema the Companion stopped at, with rows in it.

    Built by replaying this package's migrations and stopping where the
    Companion's ledger stops, so the fixture is the legacy schema rather than a
    hand-copied description of it that could drift from the Pi's.
    """
    with closing(store.connect(path)) as connection:
        for version, statements in store.MIGRATIONS:
            if version > store.COMPANION_SCHEMA_VERSION:
                break
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={int(version)}")
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (store.META_CREATED_AT, json.dumps(1000.0)),
        )
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (
                store.META_PROJECT_CATALOG_PREFIX + UPLOADER,
                json.dumps(
                    {
                        PLAN_KEY: {
                            "plan": PLAN_LABEL,
                            "title": PLAN_TITLE,
                            "projects": [PROJECT],
                        }
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO connector (connector_id, first_seen_at) VALUES (?, ?)",
            (UPLOADER, 1010.0),
        )
        connection.execute(
            "INSERT INTO run_summary (connector_id, run_key, project, plan_key, "
            "run_id, primary_state, observed_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (UPLOADER, RUN_KEY, PROJECT, PLAN_KEY, "run-1", "paused", 1100.0, 1101.0),
        )
        connection.execute(
            "INSERT INTO history_entry (connector_id, entry_id, source_key, "
            "plan_key, run_key, project, outcome, recorded_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                UPLOADER,
                ENTRY_ID,
                "source-a",
                PLAN_KEY,
                RUN_KEY,
                PROJECT,
                "complete",
                1200.0,
                1201.0,
            ),
        )
        connection.execute(
            "INSERT INTO incident (incident_id, connector_id, run_key, plan_key, "
            "project, kind, detected_at, evidence_at, next_attempt_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                INCIDENT_ID,
                UPLOADER,
                RUN_KEY,
                PLAN_KEY,
                PROJECT,
                "unanswered-question",
                1300.0,
                1290.0,
                1400.0,
            ),
        )
        connection.execute(
            "INSERT INTO operation (operation_id, connector_id, command_id, "
            "method, status, plan_key, run_key, requested_at, deliver_by, "
            "received_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                OPERATION_ID,
                UPLOADER,
                "command-7",
                "asl.reply",
                OPERATION_CONSUMED,
                PLAN_KEY,
                RUN_KEY,
                1250.0,
                1260.0,
                1251.0,
                1261.0,
            ),
        )
    # The Companion's own database is private, and a prefix carrying a
    # world-readable file is refused before any migration step runs.
    path.chmod(PRIVATE_FILE_MODE)


def companion_service_configuration() -> dict[str, Any]:
    """A service ``runtime.json`` as the Companion wrote it.

    ``observation_only`` is absent on purpose: the Companion's default was to
    permit mutations, so its file never had to say so. That absence is the one
    field whose meaning inverts across the upgrade.
    """
    return {
        "role": "service",
        "settings": {
            "owner_identity": OWNER,
            "history_window_days": 7,
            "fixture_mode": False,
        },
    }


def companion_connector_configuration(root: Path) -> dict[str, Any]:
    """A connector ``runtime.json`` as the Companion wrote it."""
    state = root / "asl-state"
    plans = root / "plans"
    for path in (state, plans):
        path.mkdir(mode=0o700, exist_ok=True)
    return {
        "role": "connector",
        "settings": {"public_origin": PI_SITE.public_origin},
        "connector_id": CONNECTOR_ID,
        "asl": ASL_EXECUTABLE,
        "harnesses": ["claude"],
        "asl_state": str(state),
        "plan_roots": [str(plans)],
    }


def companion_journal() -> dict[str, Any]:
    """A dispatch journal with one of every state a migration has to answer for.

    The two open records carry fields the journal has no column for. They are
    not there to be migrated; they are there so that "the reply text did not
    survive" is asserted against a record that had one.
    """
    return {
        "version": migration.JOURNAL_SCHEMA_VERSION,
        "operations": [
            {
                "operation_id": "op-pending",
                "command_id": "command-1",
                "method": "asl.reply",
                "status": OPERATION_PENDING,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 100.0,
                "deliver_by": 400.0,
                "reply": "yes, proceed with the migration",
                "action_token": "token-abc",
            },
            {
                "operation_id": "op-dispatched",
                "command_id": "command-2",
                "method": "asl.reply",
                "status": OPERATION_DISPATCHED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 110.0,
                "deliver_by": 410.0,
                "dispatched_at": 115.0,
                "body": "the answer nobody may send twice",
            },
            {
                "operation_id": "op-accepted",
                "command_id": "command-3",
                "method": "asl.launch",
                "status": OPERATION_ACCEPTED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 120.0,
                "deliver_by": 420.0,
            },
            {
                "operation_id": "op-settled",
                "command_id": "command-4",
                "method": "asl.reply",
                "status": OPERATION_EXPIRED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 130.0,
                "deliver_by": 430.0,
                "settled_at": 431.0,
                "detail_code": DETAIL_DEADLINE_PASSED,
            },
            {
                "operation_id": "op-unconfirmed",
                "command_id": "command-5",
                "method": "asl.reply",
                "status": OPERATION_UNCONFIRMED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 140.0,
                "deliver_by": 440.0,
                "detail_code": DETAIL_CONNECTOR_RESTARTED,
            },
        ],
    }


def companion_checkpoints() -> dict[str, Any]:
    """A history checkpoint as the Companion wrote it: this one, minus a field.

    Derived from this package's own payload rather than typed out, so the
    fixture stays a *subset* of the current shape even if the current shape
    grows -- which is the property the carry depends on.
    """
    payload = history.SourceCheckpoint(
        source_key="plan-a",
        device=64,
        inode=4242,
        offset=8192,
        head_digest="c0ffee",
        head_length=4096,
        records=17,
        plan_dir="/home/pi/plans/plan-a",
        delivered=("run-0",),
    ).to_payload()
    del payload["open_questions"]
    return {"version": history.CHECKPOINT_SCHEMA_VERSION, "sources": [payload]}


def receipt(kind: str) -> dict[str, Any]:
    """One integration receipt, in the shape a receipt store reads."""
    return {"kind": kind, "owner": OWNER, "resource": f"/srv/{kind}", "claimed_at": 1.0}


def stored_credentials(path: Path) -> list[Credential]:
    """Every credential a service secrets file holds, disabled ones included."""
    store = CredentialStore.from_document(document(path)["credentials"])
    return list(store.credentials())


def write_credentials(path: Path, credentials: list[Credential]) -> None:
    """Put a credential list back into a service secrets file, in place."""
    store = CredentialStore(credentials)
    write_json(path, {**document(path), "credentials": store.as_document()})


def issue_extra(path: Path, *, subject: str = "second-mac") -> str:
    """Issue one more connector credential, and return its id.

    A second one, because the two a prefix is provisioned with cannot be
    disabled without making the installation unusable: withdrawing a pairing is
    something that happens to the third Mac somebody added, not to the service
    itself.
    """
    credential, _ = issue(ROLE_CONNECTOR, created_at=1.0, subject=subject)
    write_credentials(path, [*stored_credentials(path), credential])
    return credential.credential_id


def revoke(path: Path, credential_id: str) -> None:
    """Disable one credential, the way withdrawing a pairing would."""
    write_credentials(
        path,
        [
            replace(item, disabled=True)
            if item.credential_id == credential_id
            else item
            for item in stored_credentials(path)
        ],
    )


def revoke_everything(path: Path) -> None:
    """Disable every credential a file holds."""
    write_credentials(
        path, [replace(item, disabled=True) for item in stored_credentials(path)]
    )


def keys_in(database: Path) -> set[str]:
    """Every run key one database holds, read without migrating it."""
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0]) for row in connection.execute("SELECT run_key FROM run_summary")
        }


class LegacyPrefixTestCase(unittest.TestCase):
    """A prefix in the state the Companion left it in."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-migrate-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.prefix = self.root / "opt" / "companion"

    def service_prefix(self) -> Path:
        """A provisioned prefix, put back into the Companion's layout.

        Provisioned first because the credentials are the one thing a test
        cannot fabricate: the connector token exists only as a digest, and the
        point of several of these tests is that it survives untouched.
        """
        provision(self.prefix, OWNER, CONNECTOR_ID)
        write_json(self.prefix / CONFIG_PATH, companion_service_configuration())
        companion_database(self.prefix / migration.LEGACY_DATABASE_PATH)
        for name in ("route", "ntfy"):
            write_json(self.prefix / RECEIPTS_DIRECTORY / f"{name}.json", receipt(name))
        return self.prefix

    def connector_prefix(self) -> Path:
        """The Mac's half: a configuration, an issued token, and two files."""
        self.prefix.mkdir(mode=0o700, parents=True)
        for name in ("config", "credentials", STATE_DIRECTORY):
            (self.prefix / name).mkdir(mode=0o700)
        write_json(
            self.prefix / CONFIG_PATH, companion_connector_configuration(self.root)
        )
        write_json(self.prefix / SECRETS_PATH, {"connector_token": "a" * 43})
        write_json(self.prefix / migration.JOURNAL_PATH, companion_journal())
        write_json(self.prefix / migration.CHECKPOINT_PATH, companion_checkpoints())
        return self.prefix

    def migration(self, **options: Any) -> migration.Migration:
        return migration.Migration(self.prefix, **options)


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
        with closing(store.connect(prefix / DATABASE_PATH)) as connection:
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
        with closing(store.connect(prefix / DATABASE_PATH)) as connection:
            self.assertEqual(store.schema_version(connection), store.SCHEMA_VERSION)

    def test_the_plan_labels_an_operator_saw_survive_the_move(self) -> None:
        # Carried by the schema migration rather than by this module, which is
        # the point: the rows go through the store's own upgrade path, so
        # months of history keep the names they were shown under.
        prefix = self.service_prefix()
        self.migration().apply()
        with closing(store.connect(prefix / DATABASE_PATH)) as connection:
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


# ---------------------------------------------------------------------------
# Rolling back


class RollbackPairingTest(InstallationTestCase):
    """A release and the data it ran on, put back together."""

    def observe(self, run_key: str) -> None:
        """Record one run summary, the way an upload would leave one."""
        with closing(store.connect(self.prefix / DATABASE_PATH)) as connection:
            connection.execute(
                "INSERT INTO run_summary (connector_id, run_key, project, "
                "plan_key, primary_state, observed_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (UPLOADER, run_key, PROJECT, PLAN_KEY, "paused", 1.0, 2.0),
            )

    def observed(self) -> set[str]:
        return keys_in(self.prefix / DATABASE_PATH)

    def backups(self) -> list[str]:
        return sorted(path.name for path in (self.prefix / BACKUPS_DIRECTORY).iterdir())

    def test_a_rollback_restores_the_state_the_older_release_ran_on(self) -> None:
        # The pairing claim. Putting the application back on its own would hand
        # an older release a database a newer one migrated, which is the case
        # the store is written to refuse.
        self.install(self.first, activate=True)
        self.observe("run-before")
        self.install(self.second, upgrade=True)
        self.observe("run-after")
        status = self.installation().rollback()
        self.assertEqual(status["artifact"], self.first[1])
        self.assertEqual(self.observed(), {"run-before"})

    def test_the_data_written_since_the_upgrade_is_kept_not_discarded(self) -> None:
        # A rollback is often the first half of diagnosing why the upgrade went
        # wrong, and the evidence is in the rows the new release wrote.
        self.install(self.first, activate=True)
        self.observe("run-before")
        self.install(self.second, upgrade=True)
        self.observe("run-after")
        status = self.installation().rollback()
        retained = self.prefix / BACKUPS_DIRECTORY / str(status["retained"])
        self.assertIn(migration.RETAINED_MARKER, retained.name)
        self.assertEqual(keys_in(retained / DATABASE_FILE), {"run-before", "run-after"})

    def test_the_restored_snapshot_is_the_one_taken_before_the_upgrade(self) -> None:
        self.install(self.first, activate=True)
        self.install(self.second, upgrade=True)
        expected = migration.paired_backup(self.prefix, self.second[1])
        status = self.installation().rollback()
        self.assertEqual(status["restored"], expected.name if expected else None)

    def test_retained_data_is_never_offered_as_a_pair_for_a_release(self) -> None:
        # The naming rule, asserted rather than assumed: post-upgrade data
        # offered later as "the state this release ran on" would pair a release
        # with rows its successor wrote.
        self.install(self.first, activate=True)
        self.install(self.second, upgrade=True)
        self.installation().rollback()
        paired = migration.paired_backup(self.prefix, self.second[1])
        self.assertIsNotNone(paired)
        self.assertNotIn(migration.RETAINED_MARKER, str(paired))
        self.assertTrue(
            any(migration.RETAINED_MARKER in name for name in self.backups())
        )

    def test_an_application_only_rollback_keeps_the_newer_observations(self) -> None:
        # The escape hatch, and the reason it is spelled out rather than
        # inferred: keeping the newer rows under an older release is a trade an
        # operator may want and nobody else may make for them.
        self.install(self.first, activate=True)
        self.observe("run-before")
        self.install(self.second, upgrade=True)
        self.observe("run-after")
        status = self.installation().rollback(data=False)
        self.assertIsNone(status["restored"])
        self.assertNotIn("retained", status)
        self.assertEqual(self.observed(), {"run-before", "run-after"})

    def test_a_rollback_does_not_revive_a_revoked_credential(self) -> None:
        # Revocation is a decision about who may connect now. An application
        # going back a version is not a reason to reinstate anybody, so the
        # credentials stay out of the pair that moves -- and the credential
        # here was enabled at the moment the restored snapshot was taken,
        # which is the only version of this that could go wrong.
        self.install(self.first, activate=True)
        extra = issue_extra(self.prefix / SECRETS_PATH)
        self.install(self.second, upgrade=True)
        revoke(self.prefix / SECRETS_PATH, extra)
        self.installation().rollback()
        disabled = {
            item.credential_id: item.disabled
            for item in stored_credentials(self.prefix / SECRETS_PATH)
        }
        self.assertIs(disabled[extra], True)
        self.assertEqual(sum(disabled.values()), 1)

    def test_a_rollback_with_no_snapshot_to_pair_with_still_rolls_back(self) -> None:
        # An installation that predates this is an installation nobody
        # snapshotted. The application still goes back; what it cannot do is
        # claim to have restored anything.
        self.install(self.first, activate=True)
        self.install(self.second, upgrade=True)
        for path in (self.prefix / BACKUPS_DIRECTORY).iterdir():
            shutil.rmtree(path)
        status = self.installation().rollback()
        self.assertEqual(status["artifact"], self.first[1])
        self.assertIsNone(status["restored"])

    def test_the_connector_has_no_paired_state_to_restore(self) -> None:
        installation = lifecycle.Installation(self.prefix)
        with self.assertRaises(lifecycle.LifecycleError):
            installation.install(
                python=sys.executable, restore=self.prefix / BACKUPS_DIRECTORY
            )


if __name__ == "__main__":  # pragma: no cover - operator entry point
    unittest.main()
