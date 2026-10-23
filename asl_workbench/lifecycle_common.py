"""Lifecycle common."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .compatibility import PACKAGE_NAME
from .installation import (
    InstallationError,
)

#: The native identifiers. Owned names, not descriptions: they are how an
#: already-installed launch agent and unit are recognised.
LABEL = "com.sglavoie.asl-companion"


UNIT = "asl-companion.service"


#: The two deployment roles. Named here rather than reusing the credential
#: roles of :mod:`asl_workbench.credentials`: a credential role says what a
#: caller may ask for, and these say which half of the deployment a prefix is.
ROLE_CONNECTOR = "connector"


ROLE_SERVICE = "service"


DEPLOYMENT_ROLES: tuple[str, ...] = (ROLE_CONNECTOR, ROLE_SERVICE)


#: Artifact bounds. Both are checked before and after expansion, because a zip
#: that is small on disk is not necessarily small once unpacked.
MAX_ARTIFACT = 32 * 1024 * 1024


MAX_MEMBERS = 256


#: How long any one host command may take.
COMMAND_TIMEOUT_SECONDS = 45


#: The launch agent's ``ExitTimeOut``: how long launchd waits for the connector
#: to exit before it kills it.
CONNECTOR_EXIT_TIMEOUT_SECONDS = 30

#: How long a stop waits for launchd to let go of the job, past that timeout.
UNLOAD_WAIT_SECONDS = CONNECTOR_EXIT_TIMEOUT_SECONDS + 10


#: What may be in an artifact. The package's own Python, and the browser assets
#: under its ``static`` tree. Nothing else -- not the package README, not a
#: fixture, not a credential, not a Node manifest -- because "only portable
#: application code and static assets" is a property of the file rather than of
#: the process that built it.
PACKAGE_DIRECTORY = PACKAGE_NAME


STATIC_DIRECTORY = f"{PACKAGE_DIRECTORY}/static"


SOURCE_SUFFIX = ".py"


STATIC_SUFFIXES: tuple[str, ...] = (".js", ".css", ".html")


#: What an artifact must carry to be a runnable application: the package, the
#: entry point a service manager starts, the transport it starts, and the
#: lifecycle that will later upgrade it in place.
REQUIRED_MEMBERS: frozenset[str] = frozenset(
    {
        f"{PACKAGE_DIRECTORY}/__init__.py",
        f"{PACKAGE_DIRECTORY}/lifecycle.py",
        f"{PACKAGE_DIRECTORY}/runtime.py",
        f"{PACKAGE_DIRECTORY}/server.py",
        f"{PACKAGE_DIRECTORY}/http.py",
        f"{PACKAGE_DIRECTORY}/store_workbench.py",
        f"{STATIC_DIRECTORY}/index.html",
    }
)


#: Zip entries can carry any of these as the member's file type. A directory
#: entry is fine; anything that is not a regular file or a directory -- a
#: symlink above all -- is refused rather than unpacked.
_ALLOWED_MEMBER_TYPES = (0, stat.S_IFREG, stat.S_IFDIR)


_DIGEST = re.compile(r"[0-9a-f]{64}")


_REVISION = re.compile(r"[0-9a-f]{40}")


#: How the attached ASL installation is asked what it would announce. Run in
#: the attachment's own checkout, so what is recorded is that installation's
#: pins rather than the ones this process was imported with.
_HELLO_PROBE = (
    "import json, sys\n"
    "from asl_workbench import CONNECTOR_CAPABILITIES, connector_hello\n"
    "print(json.dumps(connector_hello(\n"
    "    connector_id=sys.argv[1],\n"
    "    target_id=sys.argv[2],\n"
    "    capabilities=CONNECTOR_CAPABILITIES,\n"
    ")))\n"
)


#: The flags the connector subcommand must offer before a launch agent is
#: written for it. Availability only: this does not open a pairing, and the
#: runtime hello is what actually settles the contract.
_CONNECT_FLAGS: tuple[str, ...] = ("--target-id", "--origin", "--token-file")


class LifecycleError(InstallationError):
    """An installation operation refused rather than acted."""


# ---------------------------------------------------------------------------
# Host commands


def command(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run one bounded host command, keeping its stderr out of the message.

    A child's stderr can quote configuration, a private origin, or a token it
    was handed, so a failure names the command and nothing else.
    """
    result = subprocess.run(
        [str(part) for part in argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if check and result.returncode:
        raise LifecycleError(
            "prerequisite or service command failed: " + Path(argv[0]).name
        )
    return result


def executable(value: str) -> None:
    """Refuse anything but an absolute executable only its owner may rewrite."""
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise LifecycleError("an absolute executable path is required")
    if path.stat().st_mode & 0o022:
        raise LifecycleError("executable is writable by another user")


def python_check(python: str) -> tuple[int, ...]:
    """Prove an interpreter exists, is owned, and is inside the supported band.

    The floor is the portable package's own
    (:data:`asl_workbench.compatibility.MINIMUM_PYTHON_VERSION`), which is the
    floor the Companion installer already enforces on the deployed host.
    ``sqlite3`` is imported by the probe because an interpreter without it
    would fail at the first observation rather than here.
    """
    from .compatibility import (  # noqa: PLC0415 - kept beside the check it feeds
        MAXIMUM_PYTHON_SERIES,
        MINIMUM_PYTHON_VERSION,
    )

    executable(python)
    probe = "import sys, sqlite3; print('%d.%d' % sys.version_info[:2])"
    result = command([python, "-I", "-c", probe])
    try:
        version = tuple(int(part) for part in result.stdout.decode().strip().split("."))
    except ValueError as error:
        raise LifecycleError("cannot identify Python") from error
    if not MINIMUM_PYTHON_VERSION <= version[:2] < MAXIMUM_PYTHON_SERIES:
        floor = ".".join(str(part) for part in MINIMUM_PYTHON_VERSION)
        raise LifecycleError(f"Python {floor} or later is required")
    return version


def service_environment(role: str, *, asl: str | None = None) -> dict[str, str]:
    """The environment a supervised process is given, and no more of one.

    No inherited environment: a service manager starts these from a session
    nobody chose, and a ``PATH`` or a ``PYTHON*`` variable picked up from it is
    how a release runs against an interpreter it was never checked with.
    """
    paths = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if role == ROLE_CONNECTOR and asl:
        paths = [str(Path(asl).parent), *paths]
    return {
        "HOME": str(Path.home()),
        "PATH": ":".join(paths),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }


def await_unloaded(
    loaded: Callable[[], bool],
    *,
    timeout: float = UNLOAD_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Return once ``loaded()`` is false, or refuse once ``timeout`` passes.

    ``launchctl bootout`` returns while launchd is still waiting for the job to
    exit, and a ``bootstrap`` of the same label in that window fails with an
    I/O error. A stop that does not wait therefore turns every upgrade into an
    outage the transaction then has to undo.
    """
    deadline = monotonic() + timeout
    while loaded():
        if monotonic() >= deadline:
            raise LifecycleError(
                f"native service was still loaded {timeout:.0f}s after it was stopped"
            )
        sleep(0.25)
