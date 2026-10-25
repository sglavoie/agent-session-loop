"""Workbench lifecycle support."""

from __future__ import annotations

import atexit
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Any

from asl_workbench import lifecycle
from asl_workbench.installation import (
    CONNECTOR_TOKEN_PATH,
    DEPLOYMENT_PATH,
    READY_FILE,
    STATE_DIRECTORY,
    write_json,
)
from asl_workbench.integrations import PI_SITE, provision
from asl_workbench.store_types import SCHEMA_VERSION
from tests.test_workbench_portable import PACKAGE_ROOT, REPOSITORY_ROOT, artifact

OWNER = "owner@example.com"


CONNECTOR_ID = "workbench-lifecycle"


TARGET_ID = "pi-lifecycle"


ORIGIN = PI_SITE.public_origin


#: How long a started release is given to publish readiness or to stop.
TIMEOUT = 30.0


def _owned_python() -> str:
    """The interpreter running the suite, as one only its owner may rewrite.

    The lifecycle refuses an interpreter another user can write to, as it
    should. Whether ``sys.executable`` passes is up to whoever installed it:
    Homebrew's is ``0755``, the python.org framework build CI installs is
    group-writable. So where it would be refused, the tests get a ``0755``
    wrapper that execs it, and the refusal itself stays covered by the tests
    that ask for it.
    """
    if not os.stat(sys.executable).st_mode & 0o022:
        return sys.executable
    directory = tempfile.mkdtemp(prefix="asl-lifecycle-python-")
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    wrapper = Path(directory) / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


#: The interpreter every lifecycle test installs with; see :func:`_owned_python`.
PYTHON = _owned_python()


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
    source = (PACKAGE_ROOT / "store_types.py").read_bytes()
    return artifact(
        {
            f"{lifecycle.PACKAGE_DIRECTORY}/store_types.py": source.replace(
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
            artifact=path, digest=digest, python=PYTHON, **options
        )

    def record(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.prefix / DEPLOYMENT_PATH).read_bytes())
        return data

    def ready(self) -> dict[str, Any]:
        path = self.prefix / STATE_DIRECTORY / READY_FILE
        data: dict[str, Any] = json.loads(path.read_bytes())
        return data
