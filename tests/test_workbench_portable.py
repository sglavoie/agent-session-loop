"""The production artifact, and the packaged application actually running.

This module is the one place in the suite that is not allowed to know
``agent_session_loop`` exists. It imports the standard library and
``asl_workbench``, and it runs under the Pi's interpreter:

    python3.13 -m unittest -q tests.test_workbench_portable

Everything else in ``tests/`` may import the Mac worker runtime, and nearly
everything does. That is why the portable half needs a lane of its own -- a
module that imports ASL somewhere in its graph still passes on a laptop when
the packaged artifact it is testing would not load on the Pi at all.

Two questions are asked, in this order:

* **Is the artifact right?** Not the working tree -- the bytes. Packaged
  content is checked against the portable boundary, against the member
  allowlist, and against the ways a zip can carry something other than what its
  names suggest.
* **Does the artifact run?** The release is unpacked and started as a
  subprocess from the release directory, with the forbidden roots refused at
  ``sys.meta_path``, and it is asked to serve an HTTP request before it is
  asked to stop. An import check alone would pass for a package that imports
  cleanly and cannot bind a socket.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest import mock

from asl_workbench import lifecycle, notifications, runtime
from asl_workbench.compatibility import (
    FORBIDDEN_IMPORT_ROOTS,
    MINIMUM_PYTHON_VERSION,
    blocking_meta_path_source,
)
from asl_workbench.installation import lock
from asl_workbench.integrations import PI_SITE, provision

#: A fixed timestamp. Packing is deterministic, so the tests can compare
#: digests; using the wall clock would make two packs of one tree differ for a
#: reason that has nothing to do with their content.
STAMP = 1_700_000_000

#: How long the packaged runtime is given to bind and publish readiness, and
#: how long it is then given to stop. Generous: the point is to fail on a
#: runtime that never starts, not on a loaded machine.
READY_TIMEOUT = 30.0
STOP_TIMEOUT = 30.0

PACKAGE_ROOT = Path(lifecycle.__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent

#: What the connector half is provisioned as. Any value the provisioner
#: accepts; the pairing is not exercised here.
OWNER = "owner@example.com"
CONNECTOR_ID = "workbench-portable"

#: The program the packaged release is started with. It is ``runtime.run``
#: with one substitution -- an ephemeral port, because a test may not take the
#: deployed service's fixed one -- and the site is the Pi's otherwise, so the
#: configuration the installed prefix carries is the configuration that is
#: checked.
_SERVE = (
    "import sys\n"
    "from dataclasses import replace\n"
    "from pathlib import Path\n"
    "from asl_workbench import runtime\n"
    "from asl_workbench.integrations import PI_SITE\n"
    "runtime.run(Path(sys.argv[1]), site=replace(PI_SITE, bind_port=0))\n"
)


def working_tree_members() -> dict[str, bytes]:
    """The artifact this checkout would produce, without asking git for it.

    ``lifecycle.pack`` builds from a commit, which is right for a release and
    wrong for a test: the module under test is usually uncommitted while it is
    being written, and packing the last commit would check the previous version
    of exactly the code that changed.
    """
    members: dict[str, bytes] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*")):
        name = path.relative_to(REPOSITORY_ROOT).as_posix()
        if path.is_file() and lifecycle.packable(name):
            members[name] = path.read_bytes()
    return members


def artifact(extra: dict[str, bytes] | None = None) -> bytes:
    """A production artifact, optionally with something added to it."""
    return lifecycle.normalize(
        {**working_tree_members(), **(extra or {})}, timestamp=STAMP
    )


def zip_with(entries: list[zipfile.ZipInfo], payload: bytes = b"") -> bytes:
    """A zip built entry by entry, for the shapes ``normalize`` cannot make."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for entry in entries:
            # Set on the entry rather than the archive: `writestr` takes the
            # compression from a ZipInfo it is handed, so an artifact built
            # here would otherwise be stored whole and fail its size bound for
            # the wrong reason.
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, payload)
    return buffer.getvalue()


class ArtifactBoundaryTest(unittest.TestCase):
    """What may be in a production artifact, decided over the bytes."""

    def test_this_interpreter_meets_the_portable_floor(self) -> None:
        # The lane's premise. Running it on 3.14 still checks the artifact, but
        # the contract runs it on the Pi's series, and a silent drop back to
        # the developer's interpreter would make that contract decorative.
        self.assertGreaterEqual(sys.version_info[:2], MINIMUM_PYTHON_VERSION)

    def test_the_test_module_itself_never_reaches_the_worker_runtime(self) -> None:
        for root in FORBIDDEN_IMPORT_ROOTS:
            self.assertNotIn(root, sys.modules)

    def test_the_artifact_carries_the_runtime_entry_points(self) -> None:
        members = lifecycle.check_artifact(artifact())
        self.assertLessEqual(lifecycle.REQUIRED_MEMBERS, set(members))

    def test_the_artifact_carries_the_browser_assets(self) -> None:
        members = lifecycle.check_artifact(artifact())
        prefix = f"{lifecycle.STATIC_DIRECTORY}/"
        static = [name for name in members if name.startswith(prefix)]
        self.assertIn(f"{lifecycle.STATIC_DIRECTORY}/index.html", static)
        self.assertTrue(any(name.endswith(".js") for name in static))
        self.assertTrue(any(name.endswith(".css") for name in static))

    def test_the_artifact_carries_nothing_but_code_and_assets(self) -> None:
        members = lifecycle.check_artifact(artifact())
        for name in members:
            self.assertTrue(
                name.endswith(lifecycle.SOURCE_SUFFIX)
                or name.endswith(lifecycle.STATIC_SUFFIXES),
                name,
            )
            self.assertTrue(name.startswith(f"{lifecycle.PACKAGE_DIRECTORY}/"), name)

    def test_the_package_documentation_stays_out_of_the_artifact(self) -> None:
        # A file that exists, beside the modules, and is still not production
        # content. Asserting on something absent from the tree would pass for
        # the wrong reason.
        self.assertTrue((PACKAGE_ROOT / "README.md").is_file())
        self.assertNotIn(
            f"{lifecycle.PACKAGE_DIRECTORY}/README.md",
            lifecycle.check_artifact(artifact()),
        )

    def test_the_repository_around_the_package_stays_out(self) -> None:
        members = lifecycle.check_artifact(artifact())
        for name in ("tests", "agent_session_loop", "bin", "pyproject.toml"):
            # Each of these exists beside the package, so a member naming one
            # would be a real leak rather than a check of something absent.
            self.assertTrue((REPOSITORY_ROOT / name).exists(), name)
            self.assertFalse(any(member.startswith(name) for member in members), name)

    def test_a_noncode_member_is_refused(self) -> None:
        raw = artifact({f"{lifecycle.PACKAGE_DIRECTORY}/secrets.json": b"{}"})
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.check_artifact(raw)
        self.assertIn("non-code", str(caught.exception))

    def test_a_member_that_imports_the_worker_runtime_is_refused(self) -> None:
        raw = artifact(
            {f"{lifecycle.PACKAGE_DIRECTORY}/rogue.py": b"import agent_session_loop\n"}
        )
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.check_artifact(raw)
        self.assertIn("boundary", str(caught.exception))

    def test_a_member_that_needs_a_third_party_wheel_is_refused(self) -> None:
        rogue = f"{lifecycle.PACKAGE_DIRECTORY}/rogue.py"
        raw = artifact({rogue: b"import requests\n"})
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.check_artifact(raw)
        self.assertIn("boundary", str(caught.exception))

    def test_a_member_the_pi_cannot_parse_is_refused(self) -> None:
        # PEP 758's unparenthesized except group: valid on the Mac's 3.14,
        # a SyntaxError on the deployed host. Exactly the failure that is
        # invisible until the service manager restarts a release forever.
        raw = artifact(
            {
                f"{lifecycle.PACKAGE_DIRECTORY}/rogue.py": (
                    b"try:\n    pass\nexcept OSError, ValueError:\n    pass\n"
                )
            }
        )
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.check_artifact(raw)
        self.assertIn("boundary", str(caught.exception))

    def test_a_traversing_member_is_refused(self) -> None:
        outside = f"{lifecycle.PACKAGE_DIRECTORY}/../x.py"
        for name in ("../escape.py", "/etc/passwd", outside):
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.artifact_members(zip_with([zipfile.ZipInfo(name)]))

    def test_a_member_outside_the_package_is_refused(self) -> None:
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.artifact_members(zip_with([zipfile.ZipInfo("elsewhere/mod.py")]))

    def test_a_duplicated_member_is_refused(self) -> None:
        name = f"{lifecycle.PACKAGE_DIRECTORY}/__init__.py"
        with warnings.catch_warnings():
            # `zipfile` warns while *writing* the duplicate; the refusal under
            # test belongs to whoever later reads it.
            warnings.simplefilter("ignore", UserWarning)
            raw = zip_with([zipfile.ZipInfo(name), zipfile.ZipInfo(name)])
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.artifact_members(raw)

    def test_a_symlink_member_is_refused(self) -> None:
        entry = zipfile.ZipInfo(f"{lifecycle.PACKAGE_DIRECTORY}/link.py")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.artifact_members(zip_with([entry], b"/etc/passwd"))
        self.assertIn("nonregular", str(caught.exception))

    def test_an_artifact_that_expands_beyond_the_bound_is_refused(self) -> None:
        entry = zipfile.ZipInfo(f"{lifecycle.PACKAGE_DIRECTORY}/huge.py")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFREG | 0o644) << 16
        raw = zip_with([entry], b"# " + b"x" * (lifecycle.MAX_ARTIFACT + 1))
        self.assertLess(len(raw), lifecycle.MAX_ARTIFACT)
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.artifact_members(raw)
        self.assertIn("bound", str(caught.exception))

    def test_an_artifact_missing_an_entry_point_is_refused(self) -> None:
        members = working_tree_members()
        del members[f"{lifecycle.PACKAGE_DIRECTORY}/runtime.py"]
        with self.assertRaises(lifecycle.LifecycleError) as caught:
            lifecycle.check_artifact(lifecycle.normalize(members, timestamp=STAMP))
        self.assertIn("entry points", str(caught.exception))

    def test_packing_the_same_content_twice_gives_the_same_bytes(self) -> None:
        # What makes a digest a pin. If the bytes moved with the packer, an
        # operator could not verify that the artifact on the Pi is the one the
        # release notes name.
        self.assertEqual(artifact(), artifact())


class PackagedRuntimeTest(unittest.TestCase):
    """The unpacked release, started, serving, and stopped."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-portable-")).resolve()
        self.addCleanup(self._clean)
        self.prefix = self.root / "opt" / "install"
        self.release = self.root / "release"
        provision(self.prefix, OWNER, CONNECTOR_ID)
        lifecycle.unpack(artifact(), self.release)

    def _clean(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.root.rmdir()

    def _run(self, program: str, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        """Run one program inside the release, with ASL refused outright."""
        return subprocess.run(
            [sys.executable, "-E", "-s", "-B", "-c", program, *arguments],
            cwd=self.release,
            capture_output=True,
            timeout=STOP_TIMEOUT,
            check=False,
        )

    def test_the_release_is_the_package_and_imports_without_asl(self) -> None:
        completed = self._run(
            blocking_meta_path_source()
            + "import asl_workbench\nprint(asl_workbench.__file__)\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            Path(completed.stdout.decode().strip()).resolve(),
            self.release / lifecycle.PACKAGE_DIRECTORY / "__init__.py",
        )

    def test_the_forbidden_root_is_genuinely_unavailable(self) -> None:
        # Proves the check above proves something: the same interpreter, the
        # same cwd, asked for the module the boundary forbids.
        completed = self._run(
            blocking_meta_path_source() + "import agent_session_loop\n"
        )
        self.assertNotEqual(completed.returncode, 0)

    @contextlib.contextmanager
    def _serving(self) -> Iterator[tuple[subprocess.Popen[bytes], dict[str, object]]]:
        """The packaged release, started and listening, then cleaned up."""
        started = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
            [
                sys.executable,
                "-E",
                "-s",
                "-B",
                "-c",
                blocking_meta_path_source() + _SERVE,
                str(self.prefix),
            ],
            cwd=self.release,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            yield started, self._await_ready(started)
        finally:
            if started.poll() is None:
                started.kill()
            started.wait(timeout=STOP_TIMEOUT)
            for stream in (started.stdout, started.stderr):
                if stream is not None:
                    stream.close()

    def _request(
        self,
        ready: dict[str, object],
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, object]]:
        """One request against the running release, answered as JSON."""
        connection = http.client.HTTPConnection(
            str(ready["host"]), int(ready["port"]), timeout=READY_TIMEOUT
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
        finally:
            connection.close()
        return response.status, json.loads(payload) if payload else {}

    def test_the_packaged_runtime_serves_and_stops(self) -> None:
        with self._serving() as (started, ready):
            self.assertEqual(ready["pid"], started.pid)
            self.assertEqual(ready["artifact"], self.release.name)
            self.assertEqual(ready["host"], PI_SITE.bind_host)
            self.assertGreater(ready["port"], 0)

            status, body = self._request(ready, "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "ok")

            started.terminate()
            self.assertEqual(started.wait(timeout=STOP_TIMEOUT), 0)
        # Readiness is removed on the way out, so an installer polling it never
        # mistakes a stopped release for a running one.
        self.assertFalse((self.prefix / "state" / "ready.json").exists())

    def test_the_runtime_ticks_the_production_incident_publisher(self) -> None:
        credentials, _ = runtime.service_secrets(self.prefix / runtime.SECRETS_PATH)
        publisher = mock.Mock()
        engine = mock.Mock()
        server = mock.Mock(bound_host="127.0.0.1", bound_port=8766)
        ticked = threading.Event()
        engine.tick.side_effect = ticked.set

        def serve_until_tick(_server: object) -> None:
            self.assertTrue(ticked.wait(5.0))

        with (
            mock.patch.object(
                runtime,
                "service_secrets",
                return_value=(credentials, "tk_runtimefixture123"),
            ),
            mock.patch.object(
                notifications, "select_publisher", return_value=publisher
            ) as select,
            mock.patch.object(
                runtime.incidents, "IncidentEngine", return_value=engine
            ) as construct,
            mock.patch.object(runtime, "ApplicationServer", return_value=server),
            mock.patch.object(
                runtime, "install_shutdown_handlers", return_value=mock.Mock()
            ),
        ):
            runtime.run(
                self.prefix,
                site=replace(PI_SITE, bind_port=0),
                serve=serve_until_tick,
            )

        select.assert_called_once_with(
            mode=runtime.MODE_REMOTE,
            deployment=notifications.DEPLOYMENT_PRODUCTION,
            token="tk_runtimefixture123",
        )
        self.assertIs(construct.call_args.kwargs["publisher"], publisher)
        engine.tick.assert_called()

    def test_the_published_origin_is_the_one_a_mutation_is_accepted_from(
        self,
    ) -> None:
        """A control from the shell's own page is not a cross-site request.

        TLS is terminated in front of this socket, so the Origin a browser
        sends is the site's published ``https`` one -- never the ``http``
        loopback form the policy derives from the Host. A release that knew
        only that form refused every owner control from the page it had just
        served, and refused it as "cross-site request refused", which names a
        cause that is not the cause. The two requests differ in one header, so
        what is being checked is the origin and nothing else.
        """
        control = json.dumps(
            {
                "operation_id": "portable-origin-probe",
                "plan_key": "0" * 16,
                "run_key": "0" * 16,
                "bridge_token": "0" * 32,
            }
        ).encode()
        headers = {
            "Tailscale-User-Login": OWNER,
            "X-Workbench-Request": "1",
            "Content-Type": "application/json",
        }
        with self._serving() as (_started, ready):
            published, _ = self._request(
                ready,
                "POST",
                "/api/control/stop-after-current",
                headers={**headers, "Origin": PI_SITE.public_origin},
                body=control,
            )
            elsewhere, refusal = self._request(
                ready,
                "POST",
                "/api/control/stop-after-current",
                headers={**headers, "Origin": "https://example.invalid"},
                body=control,
            )
        self.assertNotEqual(published, 403)
        self.assertEqual(elsewhere, 403)
        self.assertEqual(refusal["error"], "cross-site request refused")

    def test_a_second_runtime_refuses_the_same_state(self) -> None:
        # One process owns the state. Two would put two writers on one SQLite
        # file and publish two readiness notes for one prefix. The lock is held
        # here rather than by a second child so the refusal is what is being
        # timed, not a race between two starts.
        with lock(self.prefix / "state" / "runtime.lock"):
            completed = self._run(
                blocking_meta_path_source() + _SERVE, str(self.prefix)
            )
        self.assertNotEqual(completed.returncode, 0)

    def _await_ready(self, started: subprocess.Popen[bytes]) -> dict[str, object]:
        path = self.prefix / "state" / "ready.json"
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if started.poll() is not None:
                self.fail(f"runtime exited early: {started.stderr.read()!r}")
            try:
                return json.loads(path.read_bytes())
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("runtime never published readiness")


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
