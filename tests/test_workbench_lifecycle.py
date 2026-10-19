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

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests
from agent_session_loop import cli_trace
from asl_workbench import lifecycle, runtime
from asl_workbench.installation import CONFIG_PATH, CONNECTOR_TOKEN_PATH, write_json
from asl_workbench.integrations import provision

from .workbench_lifecycle_support import (
    ASL_EXECUTABLE,
    CONNECTOR_ID,
    OWNER,
    TARGET_ID,
    connector_configuration,
    connector_prefix,
    stub_package,
)


class RuntimePolicyTests(unittest.TestCase):
    def test_installed_runtime_uses_the_documented_two_minute_phone_alert(self) -> None:
        for observation_only in (False, True):
            policy = runtime.policy_for(observation_only)
            self.assertEqual(120.0, policy.unanswered_question_alert_seconds)
            self.assertEqual(observation_only, policy.observation_only)
            self.assertEqual(not observation_only, policy.mutations_permitted)


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


if __name__ == "__main__":
    unittest.main()
