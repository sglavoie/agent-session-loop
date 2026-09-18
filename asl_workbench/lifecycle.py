"""Content-addressed installation for both halves, on their own native managers.

Two roles are deployed, and they are deployed differently on purpose:

* **service** -- the Pi. A pinned zip of this package and nothing else, unpacked
  into ``releases/<sha256>/``, run by a systemd user unit as
  ``python3 -m asl_workbench.runtime --prefix PREFIX``. The artifact is built
  from a commit, verified against an explicit digest, and checked *as packaged
  content* against the portable boundary, so a module that imports the Mac's
  worker runtime or uses grammar the Pi's interpreter cannot parse is refused
  at pack time and again at install time rather than at start-up on the host.
* **connector** -- the Mac. Nothing is packaged. The connector is
  ``asl workbench connect`` from the ASL installation that is already there,
  started by a launch agent, and what the installation records is the
  attachment: which executable, which revision of the checkout behind it, and
  what the two ends may do with each other on the wire. Copying ASL into an
  artifact would create a second ASL, which is the duplication this program
  exists to close; recording the attachment instead makes "which ASL is the Pi
  actually paired with?" a fact in ``deployment.json``.

Everything else is one mechanism:

* **The prefix is provisioned before it is installed into.** Credentials are
  :func:`asl_workbench.integrations.provision`'s to mint, never packaging's.
  An installer that could create a credential could create a *second* one for a
  pairing that already exists.
* **A release is immutable and content-addressed.** Reinstalling the same
  digest verifies the unpacked bytes rather than trusting the directory name,
  and the original zip is retained so a rollback rebuilds a verified artifact
  from bytes rather than from files something may have edited.
* **An interrupted install is recoverable, not ambiguous.** ``transaction.json``
  is written before anything native is touched and removed after the last step;
  while it exists every other operation refuses and points at ``recover``.
* **No implicit schema downgrade.** Both the incoming and the outgoing release
  must read a copy of the state before the switch, so a rollback is known to be
  possible while it is still possible. See
  :func:`asl_workbench.runtime.check_state`.
* **The worker keeps its own process group.** ``AbandonProcessGroup`` on the
  launch agent and ``KillMode=control-group`` on the unit are preserved from
  the Companion's definitions: stopping the connector must not kill the runs an
  operator launched through it, and stopping the service must take its own
  threads with it.

The native identifiers -- ``com.sglavoie.asl-companion`` and
``asl-companion.service`` -- are deliberately unchanged. They name resources an
already-deployed host owns; renaming them would orphan a launch agent and a
unit file that are running right now.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from .compatibility import PACKAGE_NAME, check_sources
from .installation import (
    BACKUPS_DIRECTORY,
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_FILE,
    DEFINITIONS_DIRECTORY,
    DEPLOYMENT_PATH,
    INSTALLED_DIRECTORIES,
    LIFECYCLE_LOCK,
    LOGS_DIRECTORY,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    READY_FILE,
    RELEASES_DIRECTORY,
    RUNTIME_LOCK,
    RUNTIME_LOCK_FILE,
    SECRETS_PATH,
    SHARED_MODE_BITS,
    STATE_DIRECTORY,
    TRANSACTION_PATH,
    InstallationError,
    atomic,
    document,
    lock,
    private,
    safe_prefix,
    write_json,
)
from .integrations import PI_SITE, IntegrationSite, service_endpoint, service_secrets
from .migration import (
    MIGRATION_PATH,
    Migration,
    paired_backup,
    restore_state,
    retained_name,
)
from .observation import ObservationPolicy
from .transport import TransportCompatibility, evaluate_hello

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
        f"{PACKAGE_DIRECTORY}/store.py",
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
    "from asl_workbench.connector import CONNECTOR_CAPABILITIES, connector_hello\n"
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


# ---------------------------------------------------------------------------
# Artifacts


def packable(name: str) -> bool:
    """Whether one path belongs in a production artifact."""
    member = PurePosixPath(name)
    if not name.startswith(PACKAGE_DIRECTORY + "/"):
        return False
    if member.suffix == SOURCE_SUFFIX:
        return "__pycache__" not in member.parts
    return member.suffix in STATIC_SUFFIXES and name.startswith(STATIC_DIRECTORY + "/")


def artifact_bytes(artifact: Path, digest: str) -> bytes:
    """Read one artifact, or refuse it. The gate every install passes through.

    Four questions, in the order that makes a "no" cheap: is the digest one
    somebody wrote down, is the file within bounds, are the bytes the ones the
    digest names, and is what is inside the file only portable application code
    and static assets that hold the boundary.
    """
    if not _DIGEST.fullmatch(digest):
        raise LifecycleError("an explicit SHA-256 is required")
    if artifact.stat().st_size > MAX_ARTIFACT:
        raise LifecycleError("artifact exceeds size bound")
    raw = artifact.read_bytes()
    if sha256(raw).hexdigest() != digest:
        raise LifecycleError("artifact digest mismatch")
    check_artifact(raw)
    return raw


def artifact_members(raw: bytes) -> dict[str, bytes]:
    """Every member of an artifact, once the file itself has been vouched for.

    Traversal, duplication, symlinks, non-code members and both size bounds are
    decided here, so no caller unpacks anything this function has not read.
    """
    members: dict[str, bytes] = {}
    expanded = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if (
                entry.filename in members
                or name.is_absolute()
                or ".." in name.parts
                or not name.parts
                or name.parts[0] != PACKAGE_DIRECTORY
                or "\\" in entry.filename
            ):
                raise LifecycleError("unsafe artifact member")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in _ALLOWED_MEMBER_TYPES:
                raise LifecycleError("artifact contains nonregular member")
            if entry.is_dir():
                continue
            if not packable(entry.filename):
                raise LifecycleError("artifact contains non-code member")
            expanded += entry.file_size
            if expanded > MAX_ARTIFACT or len(members) >= MAX_MEMBERS:
                raise LifecycleError("expanded artifact exceeds bound")
            members[entry.filename] = archive.read(entry)
    if not set(members) >= REQUIRED_MEMBERS:
        raise LifecycleError("artifact lacks runtime entry points")
    return members


def check_artifact(raw: bytes) -> dict[str, bytes]:
    """Hold the portable boundary against the packaged bytes themselves.

    The working tree is checked by ``tests/test_workbench_foundations.py``;
    this checks the file that will actually be copied to the Pi. They are not
    the same question. An artifact built from a different revision, or from a
    tree with an extra module in it, is exactly the case where "the checkout
    was fine" says nothing useful.
    """
    members = artifact_members(raw)
    report = check_sources(
        {
            name[: -len(SOURCE_SUFFIX)].replace("/", "."): payload.decode("utf-8")
            for name, payload in members.items()
            if name.endswith(SOURCE_SUFFIX)
        },
        package=PACKAGE_DIRECTORY,
    )
    if (
        report.forbidden_imports
        or report.third_party_imports
        or report.unsupported_syntax
    ):
        raise LifecycleError(
            "artifact breaks the portable boundary: " + "; ".join(report.problems())
        )
    return members


def normalize(members: dict[str, bytes], *, timestamp: int) -> bytes:
    """Build one artifact whose bytes depend only on its content and a commit.

    ``git archive`` already produces a stable tree, but its zip carries the
    packer's choices; rebuilding here fixes the member order, the mode and the
    timestamp, so two packs of one revision on two machines produce one digest.
    A digest that moved with the machine would make "pinned" mean nothing.
    """
    stamp = time.gmtime(timestamp)[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            entry = zipfile.ZipInfo(name, date_time=stamp)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, members[name])
    return buffer.getvalue()


def pack(artifact: Path, revision: str, *, repository: Path | None = None) -> str:
    """Build a production artifact from a commit. Returns its SHA-256.

    From a commit, never from the working tree: an artifact packed from
    uncommitted files cannot be rebuilt, and the digest it is installed under
    would name bytes nobody can produce again.
    """
    if not _REVISION.fullmatch(revision):
        raise LifecycleError("pack requires a full commit revision")
    root = repository or Path(__file__).resolve().parents[1]
    committed_at = int(
        command(["git", "show", "-s", "--format=%ct", revision], cwd=root)
        .stdout.decode()
        .strip()
    )
    raw = command(
        ["git", "archive", "--format=zip", revision, PACKAGE_DIRECTORY], cwd=root
    ).stdout
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise LifecycleError("the package tree contains a symlink")
            if not entry.is_dir() and packable(entry.filename):
                members[entry.filename] = archive.read(entry)
    payload = normalize(members, timestamp=committed_at)
    digest = sha256(payload).hexdigest()
    atomic(artifact, payload)
    # Packed content is checked by the installer's own gate, here, rather than
    # by a weaker one: what ships is what an install would accept.
    artifact_bytes(artifact, digest)
    return digest


def unpack(raw: bytes, destination: Path) -> None:
    """Write a verified artifact into a fresh private directory."""
    destination.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    for name, payload in check_artifact(raw).items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        atomic(path, payload)


# ---------------------------------------------------------------------------
# Deployment records


def deployment_role(prefix: Path) -> str:
    """Which half of the deployment a provisioned prefix is.

    A directory with no configuration in it is the same answer as one holding a
    configuration for something else: not an installation. Both arrive here as
    a refusal rather than as a missing file, because the operator who typed the
    wrong ``--prefix`` is owed the same sentence either way.
    """
    try:
        role = document(prefix / CONFIG_PATH).get("role")
    except OSError as error:
        raise LifecycleError(
            "prefix is not a provisioned Workbench installation"
        ) from error
    if role not in DEPLOYMENT_ROLES:
        raise LifecycleError("prefix is not a provisioned Workbench installation")
    return str(role)


def connector_settings(prefix: Path) -> dict[str, Any]:
    """Read a connector prefix's configuration, refusing anything unusable.

    Closed, like every other document the installation reads: an unexpected
    field is a configuration written for something else, and acting on the part
    that parsed would attach the Mac to a target nobody named.
    """
    data = document(prefix / CONFIG_PATH)
    expected = {
        "role",
        "settings",
        "connector_id",
        "target_id",
        "asl",
        "asl_state",
        "plan_roots",
    }
    if set(data) != expected:
        raise LifecycleError("connector configuration fields differ")
    for name in ("connector_id", "target_id"):
        value = data[name]
        if not isinstance(value, str) or not value or len(value) > 128:
            raise LifecycleError(f"invalid {name}")
    if not isinstance(data["plan_roots"], list):
        raise LifecycleError("plan_roots must be an array")
    for entry in [data["asl_state"], *data["plan_roots"]]:
        root = Path(str(entry))
        if not root.is_absolute() or not root.is_dir():
            raise LifecycleError(
                "ASL state and plan roots must be absolute directories"
            )
    executable(str(data["asl"]))
    settings = data["settings"]
    if not isinstance(settings, dict) or not isinstance(
        settings.get("public_origin"), str
    ):
        raise LifecycleError("connector configuration needs a public origin")
    return data


def attached_root(asl: str) -> Path:
    """The checkout behind an ASL executable.

    Derived the way the executable derives it itself -- the wrapper inserts its
    fourth parent on ``sys.path`` -- and then verified by looking for the
    portable package there, so a path that merely has four parents is not
    mistaken for an installation.
    """
    root = Path(asl).resolve().parents[3]
    if not (root / PACKAGE_DIRECTORY / "__init__.py").is_file():
        raise LifecycleError("cannot locate the installation behind this executable")
    return root


def attachment(
    settings: dict[str, Any], python: str, *, policy: ObservationPolicy | None = None
) -> dict[str, Any]:
    """What the Mac connector is, recorded as three facts.

    The executable, the revision of the checkout behind it, and what the two
    ends may do with each other. The third is not asserted here: the attached
    installation is asked what it would announce, and
    :func:`asl_workbench.transport.evaluate_hello` -- the same function the
    service applies to a live pairing -- judges it. An attachment recorded as
    compatible by a second implementation of that judgement would be a record
    of nothing.
    """
    asl = str(settings["asl"])
    executable(asl)
    root = attached_root(asl)
    environment = service_environment(ROLE_CONNECTOR, asl=asl)
    revision = (
        command(["git", "rev-parse", "HEAD"], cwd=root, env=environment)
        .stdout.decode()
        .strip()
    )
    if not _REVISION.fullmatch(revision):
        raise LifecycleError("the attached installation is not at a known revision")
    dirty = bool(
        command(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            env=environment,
        ).stdout.strip()
    )
    # Availability, under the named interpreter. This is where "the Mac runs
    # 3.14" is actually enforced: the wrapper imports the worker runtime, whose
    # grammar an older Python cannot parse, so a mismatch fails here instead of
    # under a launch agent that then restarts forever.
    help_text = command(
        [python, asl, "workbench", "connect", "--help"], env=environment
    ).stdout
    if any(flag.encode() not in help_text for flag in _CONNECT_FLAGS):
        raise LifecycleError("executable does not provide the Workbench connector")
    hello = json.loads(
        command(
            [
                python,
                "-s",
                "-B",
                "-c",
                _HELLO_PROBE,
                str(settings["connector_id"]),
                str(settings["target_id"]),
            ],
            cwd=root,
            env=environment,
        ).stdout
    )
    compatibility = evaluate_hello(
        hello,
        policy=policy or ObservationPolicy(),
        target_id=str(settings["target_id"]),
    )
    if not compatibility.compatible:
        raise LifecycleError(
            "attached installation is not compatible: "
            + ", ".join(compatibility.reasons)
        )
    return {
        "executable": asl,
        "python": python,
        "revision": revision,
        "dirty": dirty,
        "transport": transport_record(compatibility),
    }


def transport_record(compatibility: TransportCompatibility) -> dict[str, Any]:
    """One verdict, as data a deployment record can carry."""
    return {
        "protocol": compatibility.protocol,
        "contract_set_id": compatibility.contract_set_id,
        "connector_id": compatibility.connector_id,
        "capabilities": sorted(compatibility.granted),
        "reads_allowed": compatibility.reads_allowed,
        "mutations_allowed": compatibility.mutations_allowed,
        "mutation_refusal": compatibility.mutation_refusal,
    }


def prerequisites(
    prefix: Path, python: str, *, site: IntegrationSite
) -> dict[str, Any]:
    """Everything that must be true of the host before anything is written.

    Returns the payload the deployment record will carry for this role: an
    attachment for the connector, and nothing for the service, whose payload is
    the artifact digest the caller already named.
    """
    python_check(python)
    role = deployment_role(prefix)
    if role == ROLE_SERVICE:
        service_endpoint(prefix / CONFIG_PATH, site)
        service_secrets(prefix / SECRETS_PATH)
        return {}
    settings = connector_settings(prefix)
    private(prefix / CONNECTOR_TOKEN_PATH)
    return {"attachment": attachment(settings, python)}


# ---------------------------------------------------------------------------
# Native service definitions


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


def quote_unit(value: Any) -> str:
    """Quote one systemd command word, including its own escape characters."""
    return (
        '"'
        + str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        + '"'
    )


def connector_arguments(
    prefix: Path, settings: dict[str, Any], python: str
) -> list[str]:
    """What the launch agent runs: the installed ASL, told where to publish.

    The interpreter is named rather than left to the shebang. A launch agent
    starts with the PATH this module hands it, not the operator's, so
    ``/usr/bin/env python3`` there resolves to whatever the system ships --
    which on this Mac is old enough that the worker runtime is a SyntaxError.
    Naming the interpreter makes the one that was verified the one that runs.
    """
    arguments = [
        python,
        str(settings["asl"]),
        "workbench",
        "connect",
        "--target-id",
        str(settings["target_id"]),
        "--connector-id",
        str(settings["connector_id"]),
        "--origin",
        str(settings["settings"]["public_origin"]),
        "--token-file",
        str(prefix / CONNECTOR_TOKEN_PATH),
        "--state-dir",
        str(settings["asl_state"]),
    ]
    for root in settings["plan_roots"]:
        arguments += ["--plan-root", str(root)]
    return arguments


def service_arguments(prefix: Path, python: str) -> list[str]:
    """What the unit runs: the packaged runtime, told which prefix it serves."""
    return [
        python,
        "-E",
        "-s",
        "-B",
        "-m",
        f"{PACKAGE_DIRECTORY}.runtime",
        "--prefix",
        str(prefix),
    ]


def definition(
    prefix: Path, release: Path, python: str, role: str
) -> tuple[str, bytes]:
    """The native definition for one role, as ``(filename, contents)``.

    ``AbandonProcessGroup`` and ``KillMode=control-group`` are the two lines
    that matter and they are deliberately different. The connector spawns runs
    that must outlive it, so its process group is abandoned; the service is
    threads inside one process, so stopping it takes the whole group.
    """
    if role == ROLE_CONNECTOR:
        settings = connector_settings(prefix)
        arguments = connector_arguments(prefix, settings, python)
        environment = service_environment(role, asl=str(settings["asl"]))
        return LABEL + ".plist", plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": arguments,
                "WorkingDirectory": str(prefix),
                "EnvironmentVariables": environment,
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 10,
                "ExitTimeOut": 30,
                "Umask": SHARED_MODE_BITS,
                "AbandonProcessGroup": True,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            },
            sort_keys=True,
        )
    arguments = service_arguments(prefix, python)
    environment = service_environment(role)
    text = "\n".join(
        [
            "[Unit]",
            "Description=ASL Workbench service",
            "StartLimitIntervalSec=60",
            "StartLimitBurst=5",
            "[Service]",
            "Type=simple",
            "ExecStart=" + " ".join(quote_unit(word) for word in arguments),
            "WorkingDirectory=" + str(release).replace("%", "%%"),
            *(
                "Environment=" + quote_unit(key + "=" + value)
                for key, value in environment.items()
            ),
            "Restart=always",
            "RestartSec=10",
            "TimeoutStopSec=30",
            "UMask=0077",
            "NoNewPrivileges=true",
            "KillMode=control-group",
            "StandardOutput=null",
            "StandardError=null",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return UNIT, text.encode("utf-8")


def validate_native(path: Path, role: str) -> None:
    """Ask the native manager's own validator, before installing the file."""
    if role == ROLE_CONNECTOR:
        command(["/usr/bin/plutil", "-lint", str(path)])
        return
    validator = shutil.which("systemd-analyze")
    if not validator:
        raise LifecycleError("systemd-analyze is required on the target host")
    command([validator, "--user", "verify", str(path)])


class NativeManager:
    """One fixed per-user service, through the manager that owns it."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.domain = f"gui/{os.getuid()}"
        self.destination = Path.home() / (
            "Library/LaunchAgents/" + LABEL + ".plist"
            if role == ROLE_CONNECTOR
            else ".config/systemd/user/" + UNIT
        )

    def check_owner(self, prefix: Path) -> None:
        """Refuse to touch a definition that belongs to another installation.

        A launch agent or a unit under this name that points somewhere else is
        somebody else's service. Stopping it would be an outage nobody asked
        for, and replacing it would be an adoption.
        """
        if not self.destination.exists() and not self.destination.is_symlink():
            return
        if (
            self.destination.is_symlink()
            or self.destination.stat().st_uid != os.getuid()
        ):
            raise LifecycleError("native definition is not owned by this user")
        raw = self.destination.read_bytes()
        try:
            if self.role == ROLE_CONNECTOR:
                parsed = plistlib.loads(raw)
                arguments = parsed.get("ProgramArguments", [])
                owned = parsed.get("Label") == LABEL and str(
                    prefix / CONNECTOR_TOKEN_PATH
                ) in list(arguments)
            else:
                owned = any(
                    line.startswith("ExecStart=")
                    and line.endswith('"--prefix" ' + quote_unit(prefix))
                    for line in raw.decode("utf-8").splitlines()
                )
        except (ValueError, plistlib.InvalidFileException):
            owned = False
        if not owned:
            raise LifecycleError("native definition belongs to another installation")

    def status(self) -> bool:
        if self.role == ROLE_CONNECTOR:
            return (
                command(
                    ["/bin/launchctl", "print", self.domain + "/" + LABEL], check=False
                ).returncode
                == 0
            )
        return (
            command(
                ["systemctl", "--user", "is-active", "--quiet", UNIT], check=False
            ).returncode
            == 0
        )

    def check_prerequisites(self) -> None:
        if self.role == ROLE_CONNECTOR:
            command(["/bin/launchctl", "print", self.domain])
            return
        linger = command(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"]
        ).stdout.strip()
        if linger != b"yes":
            raise LifecycleError(
                "systemd user service requires linger for boot recovery"
            )

    def start(self, definition_path: Path) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        atomic(self.destination, definition_path.read_bytes())
        if self.role == ROLE_CONNECTOR:
            command(["/bin/launchctl", "bootstrap", self.domain, str(self.destination)])
        else:
            command(["systemctl", "--user", "daemon-reload"])
            command(["systemctl", "--user", "enable", "--now", UNIT])
        if not self.status():
            raise LifecycleError("native service did not become active")

    def stop(self) -> None:
        if self.role == ROLE_CONNECTOR:
            if self.status():
                command(["/bin/launchctl", "bootout", self.domain + "/" + LABEL])
            return
        command(["systemctl", "--user", "stop", UNIT])

    def remove(self) -> None:
        self.stop()
        if self.role == ROLE_SERVICE and self.destination.exists():
            command(["systemctl", "--user", "disable", UNIT])
        self.destination.unlink(missing_ok=True)
        if self.role == ROLE_SERVICE:
            command(["systemctl", "--user", "daemon-reload"])


# ---------------------------------------------------------------------------
# State


def snapshot(state: Path, target: Path) -> None:
    """Copy one state directory, taking the database through SQLite itself.

    A file copy of an open database copies a moment that never existed. The
    online backup API copies a consistent one, and the integrity check is what
    turns "a file appeared" into "a database a release could open". The lock,
    the readiness note and the write-ahead files are skipped: they describe the
    process that was running, not the observations it recorded.
    """
    target.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    for source in state.iterdir():
        if source.name in (RUNTIME_LOCK_FILE, READY_FILE) or source.name.endswith(
            ("-wal", "-shm")
        ):
            continue
        if source.is_symlink() or not source.is_file():
            raise LifecycleError("unexpected mutable state entry")
        if source.name == DATABASE_FILE:
            atomic(target / source.name, b"")
            with (
                closing(
                    sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                ) as original,
                closing(sqlite3.connect(target / source.name)) as copy,
            ):
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise LifecycleError("snapshot integrity check failed")
            (target / source.name).chmod(PRIVATE_FILE_MODE)
        else:
            atomic(target / source.name, source.read_bytes())


def release_check(prefix: Path, release: Path, python: str, state: Path) -> None:
    """Ask one release, in a child process, whether it can read one state."""
    command(
        [
            *service_arguments(prefix, python),
            "--state",
            str(state),
            "--check",
        ],
        cwd=release,
        env=service_environment(ROLE_SERVICE),
    )


# ---------------------------------------------------------------------------
# The installation


class Installation:
    """One prefix, and every operation that may change what it is running."""

    def __init__(
        self,
        prefix: Path,
        *,
        manager_factory: Any = NativeManager,
        validator: Any = validate_native,
        site: IntegrationSite = PI_SITE,
    ) -> None:
        self.prefix = prefix
        self.manager_factory = manager_factory
        self.validator = validator
        self.site = site

    # -- Reading -----------------------------------------------------------

    def metadata(self) -> dict[str, Any] | None:
        path = self.prefix / DEPLOYMENT_PATH
        return json.loads(path.read_bytes()) if path.exists() else None

    def status(self) -> dict[str, Any]:
        safe_prefix(self.prefix)
        pending = (self.prefix / TRANSACTION_PATH).exists()
        migrating = (self.prefix / MIGRATION_PATH).exists()
        meta = self.metadata()
        if not meta:
            return {
                "installed": False,
                "transaction_pending": pending,
                "migration_pending": migrating,
            }
        return {
            "installed": not meta.get("removed", False),
            "artifact": meta.get("artifact"),
            "attachment": meta.get("attachment"),
            "previous": meta.get("previous"),
            "role": meta["role"],
            "active": self.manager_factory(meta["role"]).status()
            if meta["active"]
            else False,
            "transaction_pending": pending,
            "migration_pending": migrating,
        }

    # -- Installing --------------------------------------------------------

    def install(
        self,
        *,
        python: str,
        artifact: Path | None = None,
        digest: str | None = None,
        activate: bool = False,
        upgrade: bool = False,
        dry_run: bool = False,
        restore: Path | None = None,
    ) -> dict[str, Any]:
        """Put one release, or one attachment, in place -- or change nothing.

        Everything that can refuse refuses before the first durable write, and
        everything after the transaction record is written is either completed
        or undone by :meth:`_restore`. There is no state in between that a
        later operation cannot name.

        ``restore`` names the state snapshot this release should run against
        instead of whatever is in the prefix now, and only :meth:`rollback`
        passes it: reinstalling an older application over data a newer one
        migrated is the one way an installation can end up with a pair that was
        never tested together.
        """
        safe_prefix(self.prefix)
        role = deployment_role(self.prefix)
        raw = self._candidate_bytes(role, artifact, digest)
        old = self.metadata()
        if (self.prefix / TRANSACTION_PATH).exists():
            raise LifecycleError(
                "interrupted transaction; run recover before continuing"
            )
        if upgrade and (not old or old.get("removed")):
            raise LifecycleError("upgrade requires an installed release")
        if restore is not None and (role != ROLE_SERVICE or dry_run):
            # A connector holds no state to pair with, and a rehearsal runs
            # against a copy -- restoring into one would prove nothing and
            # would leave the real prefix's post-upgrade data unretained.
            raise LifecycleError("only a service install can restore paired state")
        if old and old["role"] != role:
            raise LifecycleError("role cannot change during lifecycle")
        if (
            old
            and not upgrade
            and not old.get("removed")
            and old.get("artifact") != digest
        ):
            raise LifecycleError("use upgrade for a different artifact")
        payload = prerequisites(self.prefix, python, site=self.site)
        active = activate or bool(old and old["active"])
        manager = self.manager_factory(role)
        if active:
            manager.check_owner(self.prefix)
            manager.check_prerequisites()
        if dry_run:
            return self._rehearse(
                old, artifact=artifact, digest=digest, python=python, upgrade=upgrade
            )
        with lock(self.prefix / LIFECYCLE_LOCK):
            # Rechecked under the lock: two installers may have raced their
            # prerequisites and both decided they were upgrading the same thing.
            if self.metadata() != old:
                raise LifecycleError("installation changed concurrently")
            for name in INSTALLED_DIRECTORIES:
                (self.prefix / name).mkdir(exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
            release = self._materialize(raw, digest)
            name, content = definition(self.prefix, release, python, role)
            definition_path = self.prefix / DEFINITIONS_DIRECTORY / name
            with tempfile.TemporaryDirectory(dir=self.prefix) as temp:
                native = Path(temp) / name
                atomic(native, content)
                self.validator(native, role)
            candidate = self._candidate_record(
                old, role, python, digest, payload, active
            )
            write_json(
                self.prefix / TRANSACTION_PATH, {"old": old, "candidate": candidate}
            )
            try:
                if old and old["active"]:
                    manager.stop()
                retained = self._switch(
                    old,
                    candidate,
                    release,
                    python,
                    definition_path,
                    content,
                    restore=restore,
                )
                if active:
                    manager.start(definition_path)
                    self.wait_ready(candidate)
            except BaseException:
                self._restore(old, candidate, manager)
                raise
            (self.prefix / TRANSACTION_PATH).unlink()
            status = self.status()
            return {**status, "retained": retained.name} if retained else status

    def _candidate_bytes(
        self, role: str, artifact: Path | None, digest: str | None
    ) -> bytes | None:
        """The artifact this install will unpack, if this role has one."""
        if role == ROLE_CONNECTOR:
            if artifact or digest:
                raise LifecycleError(
                    "the connector attaches to an installation rather than a package"
                )
            return None
        if not artifact or not digest:
            raise LifecycleError("a service release requires an artifact and a SHA-256")
        return artifact_bytes(artifact, digest)

    def _materialize(self, raw: bytes | None, digest: str | None) -> Path:
        """Put the release directory in place, or prove the one there is right.

        A content address is a promise about bytes, so reusing one verifies
        them. Trusting the directory name would make an edited release
        indistinguishable from the artifact it was unpacked from.
        """
        if raw is None or digest is None:
            return self.prefix
        release = self.prefix / RELEASES_DIRECTORY / digest
        if not release.exists():
            with tempfile.TemporaryDirectory(
                dir=self.prefix / RELEASES_DIRECTORY
            ) as temp:
                staged = Path(temp) / "candidate"
                unpack(raw, staged)
                os.replace(staged, release)
        else:
            with tempfile.TemporaryDirectory(dir=self.prefix) as temp:
                expected = Path(temp) / "release"
                unpack(raw, expected)
                installed = {
                    path.relative_to(release)
                    for path in release.rglob("*")
                    if path.is_file()
                }
                candidate = {
                    path.relative_to(expected)
                    for path in expected.rglob("*")
                    if path.is_file()
                }
                if installed != candidate or any(
                    (release / path).read_bytes() != (expected / path).read_bytes()
                    for path in candidate
                ):
                    raise LifecycleError("installed release was modified")
        atomic(self.prefix / RELEASES_DIRECTORY / (digest + ".zip"), raw)
        return release

    def _candidate_record(
        self,
        old: dict[str, Any] | None,
        role: str,
        python: str,
        digest: str | None,
        payload: dict[str, Any],
        active: bool,
    ) -> dict[str, Any]:
        """What ``deployment.json`` will say if this install completes.

        ``previous`` is carried only for the service. A connector's previous
        state is a revision of a checkout that has already moved on, so
        recording it would describe a rollback this installation cannot
        perform.
        """
        changed = bool(old) and old is not None and old.get("artifact") != digest
        return {
            "artifact": digest,
            "python": python,
            "role": role,
            "active": active,
            "previous": (
                old.get("artifact")
                if role == ROLE_SERVICE and changed and old
                else (old or {}).get("previous")
                if role == ROLE_SERVICE
                else None
            ),
            "previous_python": (
                old.get("python")
                if role == ROLE_SERVICE and changed and old
                else (old or {}).get("previous_python")
                if role == ROLE_SERVICE
                else None
            ),
            "removed": False,
            **payload,
        }

    def _switch(
        self,
        old: dict[str, Any] | None,
        candidate: dict[str, Any],
        release: Path,
        python: str,
        definition_path: Path,
        content: bytes,
        *,
        restore: Path | None = None,
    ) -> Path | None:
        """Back the state up, prove both releases can read it, then commit.

        Returns where the post-upgrade data was set aside, when a ``restore``
        displaced it. That snapshot is kept under a name
        :func:`~asl_workbench.migration.paired_backup` will never offer as the
        state a release was paired with: it is evidence from a version this
        installation is no longer running, and it is retained rather than
        discarded because a rollback is often the first half of diagnosing why
        the upgrade went wrong.

        The restore happens here, under the runtime lock and after the caller
        has stopped the service, because it is the only moment at which no
        release is reading the state directory.
        """
        state = self.prefix / STATE_DIRECTORY
        retained: Path | None = None
        with lock(self.prefix / RUNTIME_LOCK):
            if candidate["role"] == ROLE_SERVICE:
                stamp = time.time_ns()
                if restore is not None:
                    retained = (
                        self.prefix
                        / BACKUPS_DIRECTORY
                        / retained_name(stamp, str((old or {}).get("artifact") or ""))
                    )
                    snapshot(state, retained)
                    restore_state(state, restore)
                backup = (
                    self.prefix
                    / BACKUPS_DIRECTORY
                    / f"{stamp}-{str(candidate['artifact'])[:12]}"
                )
                snapshot(state, backup)
                with tempfile.TemporaryDirectory(dir=self.prefix) as temp:
                    trial = Path(temp) / STATE_DIRECTORY
                    snapshot(backup, trial)
                    release_check(self.prefix, release, python, trial)
                    if old and not old.get("removed"):
                        release_check(
                            self.prefix,
                            self.prefix / RELEASES_DIRECTORY / str(old["artifact"]),
                            str(old["python"]),
                            trial,
                        )
                    # Both directions, against the state the incoming release
                    # has already migrated. A downgrade that cannot read it is
                    # a rollback that would have failed after the switch.
                    release_check(self.prefix, release, python, trial)
                release_check(self.prefix, release, python, state)
            atomic(definition_path, content)
            (state / READY_FILE).unlink(missing_ok=True)
            write_json(self.prefix / DEPLOYMENT_PATH, candidate)
        return retained

    def _rehearse(
        self,
        old: dict[str, Any] | None,
        *,
        artifact: Path | None,
        digest: str | None,
        python: str,
        upgrade: bool,
    ) -> dict[str, Any]:
        """Run the whole install against a disposable copy of this prefix.

        Identical filesystem work, no service-manager mutation: the rehearsal's
        record says ``active: False``, so nothing it does can reach the running
        installation. What it answers is the question a dry run is actually
        asked -- would this artifact install here, against this state.
        """
        with tempfile.TemporaryDirectory(prefix="workbench-dry-run-") as temp:
            staging = Installation(
                Path(temp).resolve() / "install",
                manager_factory=self.manager_factory,
                validator=self.validator,
                site=self.site,
            )
            staging.prefix.mkdir(mode=PRIVATE_DIRECTORY_MODE)
            for name in ("config", "credentials", RELEASES_DIRECTORY):
                source = self.prefix / name
                if source.is_dir():
                    shutil.copytree(source, staging.prefix / name)
            snapshot(self.prefix / STATE_DIRECTORY, staging.prefix / STATE_DIRECTORY)
            if old:
                write_json(staging.prefix / DEPLOYMENT_PATH, {**old, "active": False})
            staging.install(
                artifact=artifact, digest=digest, python=python, upgrade=upgrade
            )
        return {"dry_run": True, "artifact": digest, "activate": False}

    # -- Recovering --------------------------------------------------------

    def wait_ready(self, meta: dict[str, Any], *, timeout: float = 10.0) -> None:
        """Wait for the started release to say it is serving, or refuse.

        Three facts, not one: the note names the release that was started, the
        process it names is alive, and the runtime lock is held -- which is
        what distinguishes a running service from a note left behind by one
        that died. A fixed sleep would report a crash loop as a success.
        """
        if meta["role"] != ROLE_SERVICE:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready = json.loads(
                    (self.prefix / STATE_DIRECTORY / READY_FILE).read_bytes()
                )
                if ready["artifact"] == meta["artifact"]:
                    os.kill(ready["pid"], 0)
                    try:
                        with lock(self.prefix / RUNTIME_LOCK):
                            pass
                    except InstallationError:
                        return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        raise LifecycleError("runtime did not become ready")

    def _restore(
        self, old: dict[str, Any] | None, candidate: dict[str, Any], manager: Any
    ) -> None:
        """Put back whatever was running before this transaction started."""
        if candidate["active"]:
            manager.stop()
            if old and not old["active"]:
                manager.remove()
        if old and not old.get("removed"):
            role = old["role"]
            release = (
                self.prefix / RELEASES_DIRECTORY / str(old["artifact"])
                if role == ROLE_SERVICE
                else self.prefix
            )
            with lock(self.prefix / RUNTIME_LOCK):
                if role == ROLE_SERVICE:
                    release_check(
                        self.prefix,
                        release,
                        str(old["python"]),
                        self.prefix / STATE_DIRECTORY,
                    )
                name, content = definition(
                    self.prefix, release, str(old["python"]), role
                )
                atomic(self.prefix / DEFINITIONS_DIRECTORY / name, content)
                (self.prefix / STATE_DIRECTORY / READY_FILE).unlink(missing_ok=True)
                write_json(self.prefix / DEPLOYMENT_PATH, old)
            if old["active"]:
                manager.start(self.prefix / DEFINITIONS_DIRECTORY / name)
                self.wait_ready(old)
        else:
            if candidate["active"]:
                manager.remove()
            if old:
                write_json(self.prefix / DEPLOYMENT_PATH, old)
            else:
                (self.prefix / DEPLOYMENT_PATH).unlink(missing_ok=True)
        (self.prefix / TRANSACTION_PATH).unlink(missing_ok=True)

    def recover(self) -> dict[str, Any]:
        """Finish undoing an interrupted transaction. Safe to repeat.

        An interrupted migration is undone first, and outside the lifecycle
        lock because :meth:`~asl_workbench.migration.Migration.recover` takes
        that lock itself. The order is the one the writes happened in: a
        migration that stopped halfway left a prefix no release can be
        installed over, so restoring an application before restoring the layout
        it reads would put a release back on top of a half-moved installation.
        """
        safe_prefix(self.prefix)
        Migration(self.prefix, site=self.site).recover()
        with lock(self.prefix / LIFECYCLE_LOCK):
            path = self.prefix / TRANSACTION_PATH
            if not path.exists():
                return self.status()
            transaction = json.loads(path.read_bytes())
            manager = self.manager_factory(transaction["candidate"]["role"])
            manager.check_owner(self.prefix)
            self._restore(transaction["old"], transaction["candidate"], manager)
        return self.status()

    def rollback(self, *, data: bool = True) -> dict[str, Any]:
        """Reinstall the previous release, against the state it ran on.

        A release and the data it ran against are one pair. Putting the
        application back on its own would hand an older release a database a
        newer one has migrated, which is the case the store is written to
        refuse -- so by default the snapshot taken just before the current
        artifact was installed goes back with it, and the data written since is
        retained under a separate name rather than discarded.

        ``data`` set aside is the escape hatch for the case where that trade is
        wrong: an operator who would rather keep the newer observations and
        risk the older application says so explicitly. It is also what happens
        on its own when no paired snapshot exists, because an installation that
        predates this is an installation nobody snapshotted.

        Credentials are not part of the pair and never move here. Revocation is
        a decision about who may connect *now*, and an application going back a
        version is not a reason to reinstate anybody.
        """
        safe_prefix(self.prefix)
        meta = self.metadata()
        if not meta or not meta.get("previous") or meta.get("removed"):
            raise LifecycleError("no previous release")
        previous = str(meta["previous"])
        source = paired_backup(self.prefix, str(meta["artifact"])) if data else None
        result = self.install(
            artifact=self.prefix / RELEASES_DIRECTORY / (previous + ".zip"),
            digest=previous,
            python=str(meta["previous_python"]),
            upgrade=True,
            restore=source,
        )
        return {**result, "restored": source.name if source else None}

    # -- Removing ----------------------------------------------------------

    def remove(
        self, *, reset: bool = False, confirm: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Stop and unlink the service; optionally empty what it accumulated.

        Removal keeps configuration and credentials: they were provisioned
        once, the connector token cannot be recovered from the digest that
        stores it, and reinstalling over a removed prefix is the normal way
        back. ``reset`` is the separate, typed decision to discard observations
        -- it requires a removed installation and the prefix spelled out.
        """
        safe_prefix(self.prefix)
        meta = self.metadata()
        if not meta:
            raise LifecycleError("no managed installation")
        if (self.prefix / TRANSACTION_PATH).exists():
            raise LifecycleError("recover interrupted transaction first")
        if reset and (not meta.get("removed") or confirm != str(self.prefix)):
            raise LifecycleError(
                "reset requires removal and --confirm matching the prefix"
            )
        if dry_run:
            return {"dry_run": True, "reset": reset, "credentials_preserved": True}
        with lock(self.prefix / LIFECYCLE_LOCK):
            if self.metadata() != meta:
                raise LifecycleError("installation changed concurrently")
            manager = self.manager_factory(meta["role"])
            if meta["active"]:
                manager.check_owner(self.prefix)
                manager.remove()
            with lock(self.prefix / RUNTIME_LOCK):
                if reset:
                    for path in (self.prefix / STATE_DIRECTORY).iterdir():
                        if path.name != RUNTIME_LOCK_FILE:
                            path.unlink()
                    for name in (BACKUPS_DIRECTORY, LOGS_DIRECTORY):
                        shutil.rmtree(self.prefix / name)
                        (self.prefix / name).mkdir(mode=PRIVATE_DIRECTORY_MODE)
                else:
                    for name in (RELEASES_DIRECTORY, DEFINITIONS_DIRECTORY):
                        shutil.rmtree(self.prefix / name)
                        (self.prefix / name).mkdir(mode=PRIVATE_DIRECTORY_MODE)
                meta.update(
                    active=False, removed=True, previous=None, previous_python=None
                )
                write_json(self.prefix / DEPLOYMENT_PATH, meta)
        return self.status()


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{PACKAGE_DIRECTORY}.lifecycle",
        description="Install, upgrade, inspect and remove a Workbench deployment.",
    )
    parser.add_argument(
        "action",
        choices=(
            "pack",
            "install",
            "upgrade",
            "migrate",
            "rollback",
            "status",
            "remove",
            "reset",
            "recover",
        ),
    )
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--python")
    parser.add_argument("--revision")
    parser.add_argument("--target-id")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--application-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "pack":
        if not args.artifact or not args.revision:
            raise LifecycleError(
                "pack requires --artifact and a full commit --revision"
            )
        digest = pack(args.artifact, args.revision)
        return {
            "artifact": str(args.artifact),
            "sha256": digest,
            "revision": args.revision,
        }
    if not args.prefix:
        raise LifecycleError("--prefix is required")
    installation = Installation(args.prefix)
    if args.action in ("install", "upgrade"):
        if not args.python:
            raise LifecycleError("an absolute Python executable is required")
        return installation.install(
            artifact=args.artifact,
            digest=args.sha256,
            python=args.python,
            activate=args.activate,
            upgrade=args.action == "upgrade",
            dry_run=args.dry_run,
        )
    if args.action in ("remove", "reset"):
        return installation.remove(
            reset=args.action == "reset", confirm=args.confirm, dry_run=args.dry_run
        )
    if args.action == "migrate":
        # The dry run here is the plan itself, which is the whole reason a
        # migration is a separate action: an operator gets to read what would
        # happen to an installation nobody can reprovision before it happens.
        migration = Migration(args.prefix, target_id=args.target_id)
        return migration.plan() if args.dry_run else migration.apply()
    if args.dry_run:
        raise LifecycleError(
            "dry-run is supported for install, upgrade, migrate, remove and reset"
        )
    if args.action == "status":
        return installation.status()
    if args.action == "rollback":
        return installation.rollback(data=not args.application_only)
    return installation.recover()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.umask(SHARED_MODE_BITS)
    try:
        print(json.dumps(run(args), sort_keys=True))
    except Exception as error:  # noqa: BLE001 - CLI diagnostics must not leak state
        print(
            str(error)
            if isinstance(error, InstallationError)
            else "workbench lifecycle failed; check inputs, permissions "
            "and compatibility",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "DEPLOYMENT_ROLES",
    "LABEL",
    "MAX_ARTIFACT",
    "MAX_MEMBERS",
    "REQUIRED_MEMBERS",
    "ROLE_CONNECTOR",
    "ROLE_SERVICE",
    "STATIC_SUFFIXES",
    "UNIT",
    "Installation",
    "LifecycleError",
    "NativeManager",
    "artifact_bytes",
    "artifact_members",
    "attached_root",
    "attachment",
    "build_parser",
    "check_artifact",
    "command",
    "connector_arguments",
    "connector_settings",
    "definition",
    "deployment_role",
    "executable",
    "main",
    "normalize",
    "pack",
    "packable",
    "python_check",
    "quote_unit",
    "release_check",
    "run",
    "service_arguments",
    "service_environment",
    "snapshot",
    "transport_record",
    "unpack",
    "validate_native",
]


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
