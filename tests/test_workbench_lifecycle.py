"""Packaging, native definitions, and both roles through their lifecycles.

The half of packaging that needs the Mac: git, ``plutil``, the real ``asl``
executable, and a service that is genuinely started and genuinely stopped.
``tests/test_workbench_portable.py`` is the other half and runs under the Pi's
interpreter; what is here would fail there for reasons that have nothing to do
with the artifact.

The service is started for real. A fake service manager that only recorded
"started" would let every readiness, locking and state check in
:mod:`asl_workbench.runtime` pass by not running, and those checks are most of
what an upgrade depends on. What is faked is only the part that cannot be
exercised here: ``launchctl bootstrap`` would take over the operator's own
launch agent, and ``systemctl --user`` does not exist on this host at all. So
:class:`FakeManager` stands in for the manager and starts the release the way
the definition says to start it, and the definitions themselves are checked as
text -- plus ``plutil -lint`` for the plist, which is the real validator.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any

import tests
from agent_session_loop import cli_trace
from asl_workbench import lifecycle
from asl_workbench.installation import (
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_FILE,
    DEPLOYMENT_PATH,
    READY_FILE,
    RUNTIME_LOCK_FILE,
    SECRETS_PATH,
    STATE_DIRECTORY,
    TRANSACTION_PATH,
    InstallationError,
    write_json,
)
from asl_workbench.integrations import PI_SITE, provision
from asl_workbench.store import SCHEMA_VERSION
from tests.test_workbench_portable import PACKAGE_ROOT, REPOSITORY_ROOT, artifact

OWNER = "owner@example.com"
CONNECTOR_ID = "workbench-lifecycle"
TARGET_ID = "pi-lifecycle"
ORIGIN = PI_SITE.public_origin

#: How long a started release is given to publish readiness or to stop.
TIMEOUT = 30.0

#: The ASL this checkout installs, named the way an operator names it. Not
#: resolved: the executable is a symlink, and what an attachment records is the
#: path the configuration gave it.
ASL_EXECUTABLE = str(REPOSITORY_ROOT / "bin/.local/bin/asl")

#: The program a started service release runs. ``runtime.run`` with an
#: ephemeral port, because the deployed service's fixed port belongs to the
#: deployed service and a test may not take it.
_SERVE = (
    "import sys\n"
    "from dataclasses import replace\n"
    "from pathlib import Path\n"
    "from asl_workbench import runtime\n"
    "from asl_workbench.integrations import PI_SITE\n"
    "runtime.run(Path(sys.argv[1]), site=replace(PI_SITE, bind_port=0))\n"
)


def stub_package() -> dict[str, bytes]:
    """The smallest tree ``pack`` will accept, for the git-level tests."""
    members: dict[str, bytes] = {name: b"" for name in lifecycle.REQUIRED_MEMBERS}
    members[f"{lifecycle.PACKAGE_DIRECTORY}/README.md"] = b"# not production\n"
    return members


def older_store() -> bytes:
    """This artifact, with a store that predates the installed schema.

    The only honest way to reach the downgrade refusal: a release that declares
    an older schema than the database it is handed. Editing the database
    instead would break the *outgoing* release too, and the case worth testing
    is precisely the one where the outgoing release is still fine.
    """
    source = (PACKAGE_ROOT / "store.py").read_bytes()
    return artifact(
        {
            f"{lifecycle.PACKAGE_DIRECTORY}/store.py": source.replace(
                f"\nSCHEMA_VERSION = {SCHEMA_VERSION}\n".encode(),
                f"\nSCHEMA_VERSION = {SCHEMA_VERSION - 1}\n".encode(),
            )
        }
    )


class FakeManager:
    """The service manager, minus the part that would touch the real host.

    One instance per operation, like :class:`asl_workbench.lifecycle.
    NativeManager`, so the started process lives in a class-level registry: the
    installation builds a new manager for every call, and a per-instance handle
    would make ``status()`` answer about a manager that started nothing.
    """

    started: dict[str, subprocess.Popen[bytes]] = {}
    definitions: dict[str, Path] = {}
    calls: list[str] = []
    refuse_starts = 0
    missing_prerequisite = False
    foreign_definition = False

    @classmethod
    def reset(cls) -> None:
        for process in list(cls.started.values()):
            process.kill()
            process.wait(timeout=TIMEOUT)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        cls.started = {}
        cls.definitions = {}
        cls.calls = []
        cls.refuse_starts = 0
        cls.missing_prerequisite = False
        cls.foreign_definition = False

    def __init__(self, role: str) -> None:
        self.role = role

    def check_owner(self, prefix: Path) -> None:
        del prefix
        type(self).calls.append("check_owner")
        if self.foreign_definition:
            raise lifecycle.LifecycleError("native definition belongs to another")

    def check_prerequisites(self) -> None:
        type(self).calls.append("check_prerequisites")
        if self.missing_prerequisite:
            raise lifecycle.LifecycleError("linger is required")

    def status(self) -> bool:
        process = self.started.get(self.role)
        return process is not None and process.poll() is None

    def start(self, definition_path: Path) -> None:
        type(self).calls.append("start")
        if self.refuse_starts:
            type(self).refuse_starts -= 1
            raise lifecycle.LifecycleError("native service did not become active")
        type(self).definitions[self.role] = definition_path
        if self.role == lifecycle.ROLE_CONNECTOR:
            # Nothing is started: the connector's launch agent runs the
            # operator's live ASL, and a test that started it would attach this
            # machine to whatever the configuration names.
            return
        prefix = definition_path.parent.parent
        record = json.loads((prefix / DEPLOYMENT_PATH).read_bytes())
        release = prefix / lifecycle.RELEASES_DIRECTORY / str(record["artifact"])
        type(self).started[self.role] = subprocess.Popen(
            [sys.executable, "-E", "-s", "-B", "-c", _SERVE, str(prefix)],
            cwd=release,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def stop(self) -> None:
        type(self).calls.append("stop")
        process = type(self).started.pop(self.role, None)
        if process is None:
            return
        process.terminate()
        process.wait(timeout=TIMEOUT)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def remove(self) -> None:
        type(self).calls.append("remove")
        self.stop()
        type(self).definitions.pop(self.role, None)


def accept(path: Path, role: str) -> None:
    """A validator that accepts. The real one is exercised separately."""
    del path, role


def connector_prefix(prefix: Path) -> Path:
    """A connector prefix: the layout, without the service's credentials."""
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("config", "credentials"):
        (prefix / name).mkdir(mode=0o700, exist_ok=True)
    token = prefix / CONNECTOR_TOKEN_PATH
    if not token.exists():
        write_json(token, {"connector_token": "a" * 43})
    return prefix


def connector_configuration(prefix: Path) -> dict[str, Any]:
    """The seven fields ``connector_settings`` accepts, and no others."""
    state = prefix / "asl-state"
    plans = prefix / "plans"
    for path in (state, plans):
        path.mkdir(mode=0o700, exist_ok=True)
    return {
        "role": lifecycle.ROLE_CONNECTOR,
        "settings": {"public_origin": ORIGIN},
        "connector_id": CONNECTOR_ID,
        "target_id": TARGET_ID,
        "asl": ASL_EXECUTABLE,
        "asl_state": str(state),
        "plan_roots": [str(plans)],
    }


class TraceVariableTest(unittest.TestCase):
    """The one duplicated constant in ``tests/__init__.py``, held to one value."""

    def test_the_suite_disables_tracing_under_the_real_variable(self) -> None:
        # `tests/__init__.py` spells the name out because the portable lane
        # loads it under an interpreter that cannot parse `cli_trace` at all.
        # This is the check that keeps the two from drifting apart.
        self.assertEqual(tests.TRACE_ENV_VAR, cli_trace.TRACE_ENV_VAR)
        self.assertEqual(os.environ[cli_trace.TRACE_ENV_VAR], "0")


class PackTest(unittest.TestCase):
    """Building an artifact from a commit, in a repository made for it."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-pack-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self._git("init", "--initial-branch=main")
        for name, payload in stub_package().items():
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
            },
        )
        return completed.stdout.decode().strip()

    def _commit(self) -> str:
        self._git("add", "-A")
        self._git("commit", "-m", "packaged")
        return self._git("rev-parse", "HEAD")

    def test_packing_a_commit_twice_gives_one_artifact(self) -> None:
        # A digest that moved with the machine would make "pinned" mean
        # nothing: the Pi could not tell the release it was promised from
        # another build of the same commit.
        revision = self._commit()
        first = lifecycle.pack(
            self.root / "a.zip", revision, repository=self.repository
        )
        second = lifecycle.pack(
            self.root / "b.zip", revision, repository=self.repository
        )
        self.assertEqual(first, second)
        self.assertEqual(
            (self.root / "a.zip").read_bytes(), (self.root / "b.zip").read_bytes()
        )

    def test_a_packed_artifact_passes_the_installers_own_gate(self) -> None:
        revision = self._commit()
        path = self.root / "a.zip"
        digest = lifecycle.pack(path, revision, repository=self.repository)
        members = lifecycle.artifact_members(lifecycle.artifact_bytes(path, digest))
        self.assertLessEqual(lifecycle.REQUIRED_MEMBERS, set(members))

    def test_documentation_beside_the_modules_is_not_packed(self) -> None:
        revision = self._commit()
        path = self.root / "a.zip"
        digest = lifecycle.pack(path, revision, repository=self.repository)
        members = lifecycle.artifact_members(lifecycle.artifact_bytes(path, digest))
        self.assertNotIn(f"{lifecycle.PACKAGE_DIRECTORY}/README.md", members)

    def test_uncommitted_work_is_not_packed(self) -> None:
        # A release built from the working tree could not be rebuilt, so the
        # digest it is pinned under would name bytes nobody can produce again.
        revision = self._commit()
        (self.repository / lifecycle.PACKAGE_DIRECTORY / "later.py").write_bytes(
            b"LATER = 1\n"
        )
        path = self.root / "a.zip"
        digest = lifecycle.pack(path, revision, repository=self.repository)
        members = lifecycle.artifact_members(lifecycle.artifact_bytes(path, digest))
        self.assertNotIn(f"{lifecycle.PACKAGE_DIRECTORY}/later.py", members)

    def test_a_partial_revision_is_refused(self) -> None:
        revision = self._commit()
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.pack(
                self.root / "a.zip", revision[:12], repository=self.repository
            )

    def test_a_symlink_in_the_package_tree_is_refused(self) -> None:
        (self.repository / lifecycle.PACKAGE_DIRECTORY / "link.py").symlink_to(
            "/etc/passwd"
        )
        revision = self._commit()
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.pack(self.root / "a.zip", revision, repository=self.repository)
        self.assertIn("symlink", str(caught.exception))

    def test_a_tampered_artifact_is_refused(self) -> None:
        revision = self._commit()
        path = self.root / "a.zip"
        digest = lifecycle.pack(path, revision, repository=self.repository)
        path.write_bytes(path.read_bytes() + b"\x00")
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.artifact_bytes(path, digest)
        self.assertIn("digest", str(caught.exception))


class DefinitionTest(unittest.TestCase):
    """What the native managers are handed, read as text."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-definition-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.prefix = self.root / "opt" / "install"
        provision(self.prefix, OWNER, CONNECTOR_ID)
        self.release = self.prefix / lifecycle.RELEASES_DIRECTORY / ("a" * 64)

    def unit(self) -> str:
        name, content = lifecycle.definition(
            self.prefix, self.release, sys.executable, lifecycle.ROLE_SERVICE
        )
        self.assertEqual(name, lifecycle.UNIT)
        return content.decode("utf-8")

    def plist(self) -> bytes:
        connector = connector_prefix(self.root / "opt" / "connector")
        write_json(connector / CONFIG_PATH, connector_configuration(connector))
        name, content = lifecycle.definition(
            connector, connector, sys.executable, lifecycle.ROLE_CONNECTOR
        )
        self.assertEqual(name, lifecycle.LABEL + ".plist")
        return content

    def test_the_native_names_are_the_ones_a_deployed_host_already_owns(self) -> None:
        # Renaming either would orphan a launch agent and a unit that are
        # running right now, which is an outage dressed as a rename.
        self.assertEqual(lifecycle.UNIT, "asl-companion.service")
        self.assertEqual(lifecycle.LABEL, "com.sglavoie.asl-companion")

    def test_the_unit_stops_the_whole_control_group(self) -> None:
        self.assertIn("KillMode=control-group", self.unit())

    def test_the_unit_restarts_and_bounds_its_own_shutdown(self) -> None:
        text = self.unit()
        self.assertIn("Restart=always", text)
        self.assertIn("RestartSec=10", text)
        self.assertIn("TimeoutStopSec=30", text)
        self.assertIn("NoNewPrivileges=true", text)
        self.assertIn("WantedBy=default.target", text)

    def test_the_unit_runs_the_packaged_runtime_from_the_release(self) -> None:
        text = self.unit()
        self.assertIn(f"WorkingDirectory={self.release}", text)
        self.assertIn('"-m" "asl_workbench.runtime"', text)
        self.assertIn(f'"--prefix" "{self.prefix}"', text)

    def test_the_unit_inherits_no_environment(self) -> None:
        # A PATH or a PYTHON* variable picked up from whatever session the
        # manager starts in is how a release runs against an interpreter it was
        # never checked with.
        text = self.unit()
        self.assertIn('Environment="PATH=/usr/bin:/bin:/usr/sbin:/sbin"', text)
        self.assertIn('Environment="PYTHONDONTWRITEBYTECODE=1"', text)
        self.assertNotIn("PYTHONPATH", text)

    def test_unit_words_with_systemd_specifiers_are_escaped(self) -> None:
        self.assertEqual(lifecycle.quote_unit("100%"), '"100%%"')
        self.assertEqual(lifecycle.quote_unit("a$b"), '"a$$b"')
        self.assertEqual(lifecycle.quote_unit('a"b'), '"a\\"b"')
        self.assertEqual(lifecycle.quote_unit("a\\b"), '"a\\\\b"')

    def test_the_launch_agent_abandons_the_workers_process_group(self) -> None:
        # The whole point of the connector's definition: stopping it must not
        # kill the runs an operator launched through it.
        plist = plistlib.loads(self.plist())
        self.assertIs(plist["AbandonProcessGroup"], True)
        self.assertEqual(plist["Label"], lifecycle.LABEL)
        self.assertEqual(plist["Umask"], 0o077)
        self.assertIs(plist["KeepAlive"], True)
        self.assertEqual(plist["ThrottleInterval"], 10)

    def test_the_launch_agent_names_its_interpreter_and_its_token(self) -> None:
        connector = self.root / "opt" / "connector"
        arguments = plistlib.loads(self.plist())["ProgramArguments"]
        self.assertEqual(arguments[0], sys.executable)
        self.assertEqual(arguments[1], ASL_EXECUTABLE)
        self.assertEqual(arguments[2:4], ["workbench", "connect"])
        self.assertIn(str(connector / CONNECTOR_TOKEN_PATH), arguments)
        self.assertIn(TARGET_ID, arguments)

    def test_the_real_validator_accepts_the_launch_agent(self) -> None:
        path = self.root / (lifecycle.LABEL + ".plist")
        path.write_bytes(self.plist())
        lifecycle.validate_native(path, lifecycle.ROLE_CONNECTOR)

    def test_the_real_validator_refuses_a_damaged_launch_agent(self) -> None:
        path = self.root / (lifecycle.LABEL + ".plist")
        path.write_bytes(b"<plist><dict>")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.validate_native(path, lifecycle.ROLE_CONNECTOR)


class InstallationTestCase(unittest.TestCase):
    """A provisioned service prefix and two real artifacts to install."""

    def setUp(self) -> None:
        FakeManager.reset()
        self.addCleanup(FakeManager.reset)
        self.root = Path(tempfile.mkdtemp(prefix="workbench-install-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.prefix = self.root / "opt" / "install"
        provision(self.prefix, OWNER, CONNECTOR_ID)
        self.first = self.write_artifact("first.zip", artifact())
        self.second = self.write_artifact(
            "second.zip",
            artifact({f"{lifecycle.PACKAGE_DIRECTORY}/marker.py": b'MARKER = "2"\n'}),
        )

    def write_artifact(self, name: str, raw: bytes) -> tuple[Path, str]:
        path = self.root / name
        path.write_bytes(raw)
        return path, sha256(raw).hexdigest()

    def installation(self) -> lifecycle.Installation:
        return lifecycle.Installation(
            self.prefix, manager_factory=FakeManager, validator=accept
        )

    def install(self, which: tuple[Path, str], **options: Any) -> dict[str, Any]:
        path, digest = which
        return self.installation().install(
            artifact=path, digest=digest, python=sys.executable, **options
        )

    def record(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.prefix / DEPLOYMENT_PATH).read_bytes())
        return data

    def ready(self) -> dict[str, Any]:
        path = self.prefix / STATE_DIRECTORY / READY_FILE
        data: dict[str, Any] = json.loads(path.read_bytes())
        return data


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
            ).install(
                artifact=self.first[0], digest=self.first[1], python=sys.executable
            )


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


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
