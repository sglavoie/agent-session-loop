"""Test workbench lifecycle connector."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from asl_workbench import lifecycle
from asl_workbench.installation import (
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_FILE,
    READY_FILE,
    RUNTIME_LOCK_FILE,
    STATE_DIRECTORY,
    write_json,
)

from .workbench_lifecycle_support import (
    ASL_EXECUTABLE,
    CONNECTOR_ID,
    FakeManager,
    accept,
    connector_configuration,
    connector_prefix,
)

ASL_TEST_LANE = "integration"


class ConnectorLifecycleTest(unittest.TestCase):
    """The Mac half: attached to the ASL that is already installed."""

    def setUp(self) -> None:
        FakeManager.reset()
        self.addCleanup(FakeManager.reset)
        self.root = Path(tempfile.mkdtemp(prefix="workbench-connector-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.prefix = connector_prefix(self.root / "opt" / "connector")
        write_json(self.prefix / CONFIG_PATH, connector_configuration(self.prefix))

    def installation(self) -> lifecycle.Installation:
        return lifecycle.Installation(
            self.prefix, manager_factory=FakeManager, validator=accept
        )

    def test_installing_records_the_attachment_rather_than_a_package(self) -> None:
        status = self.installation().install(python=sys.executable, activate=True)
        self.assertTrue(status["installed"])
        self.assertIsNone(status["artifact"])
        attachment = status["attachment"]
        self.assertEqual(attachment["executable"], ASL_EXECUTABLE)
        self.assertEqual(attachment["python"], sys.executable)
        self.assertRegex(attachment["revision"], r"^[0-9a-f]{40}$")
        self.assertIn("dirty", attachment)

    def test_the_recorded_transport_is_the_one_the_service_would_judge(self) -> None:
        # Recorded by the same function the service applies to a live pairing.
        # A second implementation of that judgement would record nothing.
        status = self.installation().install(python=sys.executable)
        transport = status["attachment"]["transport"]
        self.assertEqual(transport["protocol"], 1)
        self.assertEqual(transport["connector_id"], CONNECTOR_ID)
        self.assertTrue(transport["reads_allowed"])
        self.assertFalse(transport["mutations_allowed"])
        self.assertIn("observe", transport["capabilities"])

    def test_an_artifact_is_refused_for_the_connector(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            self.installation().install(
                python=sys.executable, artifact=self.root / "x.zip", digest="a" * 64
            )
        self.assertIn("attaches", str(caught.exception))

    def test_reattaching_records_the_revision_but_offers_no_rollback(self) -> None:
        first = self.installation().install(python=sys.executable)
        second = self.installation().install(python=sys.executable, upgrade=True)
        self.assertEqual(
            first["attachment"]["revision"], second["attachment"]["revision"]
        )
        # A connector has no previous release to go back to: the checkout the
        # attachment names has already moved on.
        self.assertIsNone(second["previous"])
        with self.assertRaises(lifecycle.LifecycleError):
            self.installation().rollback()

    def test_an_executable_that_is_not_an_installation_is_refused(self) -> None:
        impostor = self.root / "impostor"
        impostor.write_text("#!/bin/sh\nexit 0\n")
        impostor.chmod(0o755)
        write_json(
            self.prefix / CONFIG_PATH,
            {**connector_configuration(self.prefix), "asl": str(impostor)},
        )
        with self.assertRaises(lifecycle.LifecycleError):
            self.installation().install(python=sys.executable)

    def test_a_configuration_with_unexpected_fields_is_refused(self) -> None:
        write_json(
            self.prefix / CONFIG_PATH,
            {**connector_configuration(self.prefix), "extra": 1},
        )
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.connector_settings(self.prefix)

    def test_acting_remotely_is_off_until_the_prefix_says_otherwise(self) -> None:
        """The launch agent carries the permission the prefix was given.

        Without this, a connector installed natively can observe and answer
        reads and can never carry out a command, because the flag that builds
        its performer only ever existed on the command line. The default stays
        off: an installation that never mentioned it is not asking to be acted
        through.
        """
        base = connector_configuration(self.prefix)
        self.assertNotIn(
            "--allow-remote-actions",
            lifecycle.connector_arguments(self.prefix, base, sys.executable),
        )
        allowed = {
            **base,
            "settings": {**base["settings"], "remote_actions": True},
        }
        self.assertEqual(
            lifecycle.connector_arguments(self.prefix, allowed, sys.executable)[-1],
            "--allow-remote-actions",
        )

    def test_a_permission_spelled_as_anything_but_a_boolean_is_refused(self) -> None:
        base = connector_configuration(self.prefix)
        write_json(
            self.prefix / CONFIG_PATH,
            {**base, "settings": {**base["settings"], "remote_actions": "false"}},
        )
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.connector_settings(self.prefix)

    def test_removing_the_connector_unloads_it_and_keeps_the_token(self) -> None:
        self.installation().install(python=sys.executable, activate=True)
        status = self.installation().remove()
        self.assertFalse(status["installed"])
        self.assertIn("remove", FakeManager.calls)
        self.assertTrue((self.prefix / CONNECTOR_TOKEN_PATH).is_file())


class HostCommandTest(unittest.TestCase):
    """The narrow things every other operation is built out of."""

    def test_a_failing_command_never_quotes_its_child(self) -> None:
        # A child's stderr can carry a token it was handed, an origin, or a
        # path nobody asked to publish.
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.command(["/bin/sh", "-c", "echo secret-token >&2; exit 1"])
        self.assertNotIn("secret-token", str(caught.exception))

    def test_a_relative_executable_is_refused(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.executable("python3")

    def test_a_world_writable_executable_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tool"
            path.write_text("#!/bin/sh\n")
            path.chmod(0o777)
            with self.assertRaises(lifecycle.LifecycleError) as caught:
                lifecycle.executable(str(path))
            self.assertIn("writable", str(caught.exception))

    def test_this_interpreter_is_inside_the_supported_band(self) -> None:
        self.assertGreaterEqual(lifecycle.python_check(sys.executable)[:2], (3, 13))

    def test_an_interpreter_that_is_not_one_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "python"
            path.write_text("#!/bin/sh\necho not-a-version\n")
            path.chmod(0o755)
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.python_check(str(path))


class SnapshotTest(unittest.TestCase):
    """Copying a state directory the way a live database has to be copied."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-snapshot-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = self.root / STATE_DIRECTORY
        self.state.mkdir(mode=0o700)
        with closing(sqlite3.connect(self.state / DATABASE_FILE)) as connection:
            connection.execute("CREATE TABLE note (id INTEGER PRIMARY KEY, body TEXT)")
            connection.execute("INSERT INTO note (body) VALUES ('kept')")
            connection.commit()

    def test_the_database_arrives_intact(self) -> None:
        target = self.root / "backup"
        lifecycle.snapshot(self.state, target)
        with closing(sqlite3.connect(target / DATABASE_FILE)) as connection:
            self.assertEqual(
                connection.execute("SELECT body FROM note").fetchone()[0], "kept"
            )

    def test_files_that_describe_the_process_are_left_behind(self) -> None:
        for name in (RUNTIME_LOCK_FILE, READY_FILE, DATABASE_FILE + "-wal"):
            (self.state / name).write_bytes(b"")
        target = self.root / "backup"
        lifecycle.snapshot(self.state, target)
        self.assertEqual(
            sorted(path.name for path in target.iterdir()), [DATABASE_FILE]
        )

    def test_a_snapshot_is_private(self) -> None:
        target = self.root / "backup"
        lifecycle.snapshot(self.state, target)
        self.assertFalse(target.stat().st_mode & 0o077)
        self.assertFalse((target / DATABASE_FILE).stat().st_mode & 0o077)

    def test_an_unexpected_state_entry_is_refused(self) -> None:
        (self.state / "link").symlink_to("/etc/passwd")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.snapshot(self.state, self.root / "backup")


if __name__ == "__main__":
    unittest.main()
