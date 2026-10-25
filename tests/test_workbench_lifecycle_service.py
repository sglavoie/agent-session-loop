"""Test workbench lifecycle service."""

from __future__ import annotations

import time
import unittest

from asl_workbench import lifecycle
from asl_workbench.installation import (
    DATABASE_FILE,
    DEPLOYMENT_PATH,
    READY_FILE,
    SECRETS_PATH,
    STATE_DIRECTORY,
    TRANSACTION_PATH,
    InstallationError,
    write_json,
)
from tests.test_workbench_portable import artifact

from .workbench_lifecycle_support import (
    PYTHON,
    TIMEOUT,
    FakeManager,
    InstallationTestCase,
    accept,
    older_store,
)

ASL_TEST_LANE = "integration"


class ServiceLifecycleTest(InstallationTestCase):
    """Install, upgrade, roll back, recover, remove -- with a live release."""

    def test_installing_without_activating_starts_nothing(self) -> None:
        status = self.install(self.first)
        self.assertTrue(status["installed"])
        self.assertFalse(status["active"])
        self.assertEqual(status["artifact"], self.first[1])
        self.assertNotIn("start", FakeManager.calls)

    def test_an_activated_release_serves_and_reports_ready(self) -> None:
        status = self.install(self.first, activate=True)
        self.assertTrue(status["active"])
        self.assertEqual(self.ready()["artifact"], self.first[1])
        self.assertGreater(self.ready()["port"], 0)

    def test_the_release_directory_is_the_digest(self) -> None:
        self.install(self.first)
        releases = self.prefix / lifecycle.RELEASES_DIRECTORY
        self.assertTrue(
            (releases / self.first[1] / "asl_workbench/runtime.py").is_file()
        )
        self.assertTrue((releases / (self.first[1] + ".zip")).is_file())

    def test_reinstalling_verifies_the_unpacked_bytes(self) -> None:
        self.install(self.first)
        self.install(self.first)
        self.assertEqual(self.record()["artifact"], self.first[1])

    def test_a_modified_release_is_refused_rather_than_trusted(self) -> None:
        # A content address is a promise about bytes. Trusting the directory
        # name would make an edited release indistinguishable from the artifact
        # it was unpacked from.
        self.install(self.first)
        edited = (
            self.prefix
            / lifecycle.RELEASES_DIRECTORY
            / self.first[1]
            / "asl_workbench/runtime.py"
        )
        edited.write_bytes(edited.read_bytes() + b"\n# edited\n")
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.install(self.first)
        self.assertIn("modified", str(caught.exception))

    def test_a_different_artifact_needs_upgrade(self) -> None:
        self.install(self.first)
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.install(self.second)
        self.assertIn("upgrade", str(caught.exception))

    def test_upgrade_requires_something_installed(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError):
            self.install(self.first, upgrade=True)

    def test_upgrading_an_active_release_restarts_it(self) -> None:
        self.install(self.first, activate=True)
        status = self.install(self.second, upgrade=True)
        self.assertEqual(status["artifact"], self.second[1])
        self.assertEqual(status["previous"], self.first[1])
        self.assertTrue(status["active"])
        self.assertEqual(self.ready()["artifact"], self.second[1])

    def test_every_switch_backs_the_state_up_first(self) -> None:
        self.install(self.first, activate=True)
        self.install(self.second, upgrade=True)
        backups = sorted((self.prefix / lifecycle.BACKUPS_DIRECTORY).iterdir())
        self.assertEqual(len(backups), 2)
        self.assertIn(self.second[1][:12], backups[-1].name)
        self.assertTrue((backups[-1] / DATABASE_FILE).is_file())

    def test_rollback_reinstalls_the_previous_release(self) -> None:
        self.install(self.first, activate=True)
        self.install(self.second, upgrade=True)
        status = self.installation().rollback()
        self.assertEqual(status["artifact"], self.first[1])
        self.assertTrue(status["active"])
        self.assertEqual(self.ready()["artifact"], self.first[1])

    def test_rollback_refuses_when_there_is_nothing_to_roll_back_to(self) -> None:
        self.install(self.first)
        with self.assertRaises(lifecycle.LifecycleError):
            self.installation().rollback()

    def test_a_release_that_cannot_read_the_state_never_takes_over(self) -> None:
        # The downgrade case: the incoming release declares an older schema
        # than the database already carries. It has to be refused before the
        # switch, while the outgoing release can still be put back.
        self.install(self.first, activate=True)
        older = self.write_artifact("older.zip", older_store())
        with self.assertRaises(lifecycle.LifecycleError):
            self.install(older, upgrade=True)
        self.assertEqual(self.record()["artifact"], self.first[1])
        self.assertFalse((self.prefix / TRANSACTION_PATH).exists())
        self.assertEqual(self.ready()["artifact"], self.first[1])

    def test_a_failed_activation_restores_the_previous_release(self) -> None:
        self.install(self.first, activate=True)
        FakeManager.refuse_starts = 1
        with self.assertRaises(lifecycle.LifecycleError):
            self.install(self.second, upgrade=True)
        self.assertEqual(self.record()["artifact"], self.first[1])
        self.assertFalse((self.prefix / TRANSACTION_PATH).exists())
        self.assertEqual(self.ready()["artifact"], self.first[1])

    def test_a_missing_host_prerequisite_stops_before_any_write(self) -> None:
        FakeManager.missing_prerequisite = True
        with self.assertRaises(lifecycle.LifecycleError):
            self.install(self.first, activate=True)
        self.assertFalse((self.prefix / DEPLOYMENT_PATH).exists())
        self.assertFalse((self.prefix / TRANSACTION_PATH).exists())

    def test_a_definition_owned_by_another_installation_is_refused(self) -> None:
        FakeManager.foreign_definition = True
        with self.assertRaises(lifecycle.LifecycleError):
            self.install(self.first, activate=True)
        self.assertFalse((self.prefix / DEPLOYMENT_PATH).exists())

    def interrupt(self) -> None:
        """The record an install leaves if it dies between its two writes."""
        old = self.record()
        write_json(
            self.prefix / TRANSACTION_PATH,
            {"old": old, "candidate": {**old, "artifact": self.second[1]}},
        )

    def test_an_interrupted_transaction_blocks_everything_until_recovered(self) -> None:
        self.install(self.first)
        self.interrupt()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.install(self.second, upgrade=True)
        self.assertIn("recover", str(caught.exception))
        with self.assertRaises(lifecycle.LifecycleError):
            self.installation().remove()
        self.assertTrue(self.installation().status()["transaction_pending"])

    def test_recover_undoes_an_interrupted_transaction(self) -> None:
        self.install(self.first)
        self.interrupt()
        status = self.installation().recover()
        self.assertFalse(status["transaction_pending"])
        self.assertEqual(status["artifact"], self.first[1])

    def test_recover_is_safe_to_repeat(self) -> None:
        self.install(self.first)
        self.interrupt()
        self.installation().recover()
        self.assertEqual(self.installation().recover()["artifact"], self.first[1])

    def test_removal_stops_the_service_and_keeps_what_was_provisioned(self) -> None:
        self.install(self.first, activate=True)
        status = self.installation().remove()
        self.assertFalse(status["installed"])
        self.assertFalse(status["active"])
        self.assertIn("remove", FakeManager.calls)
        self.assertTrue((self.prefix / SECRETS_PATH).is_file())
        self.assertTrue((self.prefix / STATE_DIRECTORY / DATABASE_FILE).is_file())
        self.assertEqual(
            list((self.prefix / lifecycle.RELEASES_DIRECTORY).iterdir()), []
        )

    def test_reset_requires_removal_and_the_prefix_spelled_out(self) -> None:
        self.install(self.first)
        with self.assertRaises(lifecycle.LifecycleError):
            self.installation().remove(reset=True, confirm=str(self.prefix))
        self.installation().remove()
        with self.assertRaises(lifecycle.LifecycleError):
            self.installation().remove(reset=True, confirm="/somewhere/else")
        self.installation().remove(reset=True, confirm=str(self.prefix))
        self.assertFalse((self.prefix / STATE_DIRECTORY / DATABASE_FILE).exists())
        self.assertTrue((self.prefix / SECRETS_PATH).is_file())

    def test_a_dry_run_changes_nothing_and_starts_nothing(self) -> None:
        result = self.install(self.first, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.prefix / DEPLOYMENT_PATH).exists())
        self.assertFalse((self.prefix / lifecycle.RELEASES_DIRECTORY).exists())
        self.assertNotIn("start", FakeManager.calls)

    def test_a_dry_run_reports_an_artifact_that_could_not_install(self) -> None:
        rogue = self.write_artifact(
            "rogue.zip",
            artifact(
                {
                    f"{lifecycle.PACKAGE_DIRECTORY}/rogue.py": (
                        b"import agent_session_loop\n"
                    )
                }
            ),
        )
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.install(rogue, dry_run=True)
        self.assertIn("boundary", str(caught.exception))

    def test_an_unprovisioned_prefix_is_not_installed_into(self) -> None:
        # Credentials are the provisioner's to mint. An installer that could
        # create one could create a second for a pairing that already exists.
        bare = self.root / "opt" / "bare"
        bare.mkdir(mode=0o700)
        with self.assertRaises(InstallationError):
            lifecycle.Installation(
                bare, manager_factory=FakeManager, validator=accept
            ).install(artifact=self.first[0], digest=self.first[1], python=PYTHON)


class ReadinessTest(InstallationTestCase):
    """Readiness is a fact about a running process, not a delay."""

    def test_a_note_left_by_a_dead_process_is_not_readiness(self) -> None:
        self.install(self.first, activate=True)
        FakeManager(lifecycle.ROLE_SERVICE).stop()
        # The runtime removes its note on the way out. Put a stale one back:
        # that is what a release killed outright would have left.
        write_json(
            self.prefix / STATE_DIRECTORY / READY_FILE,
            {"pid": 2**31 - 1, "artifact": self.first[1], "host": "127.0.0.1"},
        )
        started = time.monotonic()
        with self.assertRaises(lifecycle.LifecycleError):
            self.installation().wait_ready(self.record(), timeout=1.0)
        self.assertLess(time.monotonic() - started, TIMEOUT)

    def test_a_connector_has_no_readiness_to_wait_for(self) -> None:
        self.installation().wait_ready(
            {"role": lifecycle.ROLE_CONNECTOR, "artifact": None}, timeout=0.0
        )


if __name__ == "__main__":
    unittest.main()
