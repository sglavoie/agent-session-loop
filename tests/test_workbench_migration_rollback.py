"""Rollback pairs releases with their state without reviving old credentials."""

from __future__ import annotations

import shutil
import sys
import unittest
from contextlib import closing

from asl_workbench import lifecycle, migration, store_migrations
from asl_workbench.installation import (
    BACKUPS_DIRECTORY,
    DATABASE_FILE,
    DATABASE_PATH,
    SECRETS_PATH,
)
from tests.workbench_lifecycle_support import InstallationTestCase

from .support_workbench_migration import (
    PLAN_KEY,
    PROJECT,
    UPLOADER,
    issue_extra,
    keys_in,
    revoke,
    stored_credentials,
)

# ---------------------------------------------------------------------------
# Rolling back


class RollbackPairingTest(InstallationTestCase):
    """A release and the data it ran on, put back together."""

    def observe(self, run_key: str) -> None:
        """Record one run summary, the way an upload would leave one."""
        with closing(
            store_migrations.connect(self.prefix / DATABASE_PATH)
        ) as connection:
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


if __name__ == "__main__":
    unittest.main()
