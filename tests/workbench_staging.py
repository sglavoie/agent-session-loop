"""The harness the deployed staging rehearsal is driven and recorded through.

Session 22 stages the candidate release on the two machines that already run
the Companion -- this Mac and the Pi -- and then has to answer the question
:mod:`tests.workbench_acceptance` asks: *did that happen, to this release, on
those machines?* This module is the other half of that pair. The acceptance
reader refuses a report; this one is what a report may be built out of, and it
refuses to build one out of anything that was not observed.

It is deliberately not a test of the application, for the same reason the
acceptance reader is not: what it does is run against the real hosts. It is
also deliberately self-contained -- standard library plus
:mod:`asl_workbench` -- because half of it runs on the Pi, which has no
checkout of this repository and receives this one file beside the unpacked
release. Its rules are proved by ``tests/test_workbench_staging.py``.

Three jobs, and each exists because of a way a staging rehearsal lies:

**Isolation, as a refusal rather than as care.** A rehearsal that reuses one
production identifier is a production change with a rehearsal's name on it.
:func:`staging_site` builds the site the rehearsal runs against and
:func:`check_isolated` refuses it if it shares *any* owned identifier with
:data:`~asl_workbench.integrations.PI_SITE` -- the Serve port, the loopback
port, the published origin, the publisher, the compose marker, the monitor.
The site carries ``deployment="staging"`` and no notification site at all, so
:func:`~asl_workbench.notifications.select_publisher` raises rather than mints
a second publisher for the topic a real phone is subscribed to.

**Native names, restaged rather than reused.** The release names one launch
agent and one unit per user (:data:`~asl_workbench.lifecycle.LABEL`,
:data:`~asl_workbench.lifecycle.UNIT`), because in production there is one
deployment per user per host. A rehearsal is the second one, so
:class:`StagingNativeManager` installs the definition the release produced
under staging names, having first rewritten exactly the two things that carry a
production identity: the launch agent's ``Label`` and the unit's ``ExecStart``.
The rewrite is verified against what the release itself would have written --
:func:`restage_plist` and :func:`restage_unit` refuse content they did not
recognise -- so a definition that changed shape upstream fails here instead of
being silently re-emitted from memory.

The ``ExecStart`` rewrite is the first of the two substantive deviations this
rehearsal makes, and it is recorded rather than hidden: ``asl_workbench.runtime``'s
``main`` binds :data:`~asl_workbench.integrations.PI_SITE`'s fixed
``127.0.0.1:8766``, which the production service is already holding, so the
staging unit starts the same ``runtime.run`` with the staging site passed as
the keyword argument it already accepts. Everything the started process then
does -- the prefix it reads, the release it reports, the lock it takes, the
store it opens, the application it serves, the readiness note the installer
waits on -- is the candidate release's own code.

The second is the staged connector's environment. The release fixes it -- no
inherited variables at all, which is right, because a launch agent starts from
a session nobody chose -- and two facts about a *rehearsal* cannot be spelled
anywhere the release would read them: which certificate terminates a staging
origin that Serve is not allowed to publish, and where the default plan roots
are, given that the real ones hold the operator's actual plans and a rehearsal
that published one of them would not have a fixture corpus.
:func:`staging_environment` names the three variables and
:func:`restage_plist` adds them, refusing any the release already set and any
whose value is not a path under the rehearsal's own state. Nothing about what
the connector does with a frame changes.

**A report that cannot be written by hand.** :class:`Journal` is append-only
and timestamped, and :meth:`Journal.render` refuses to produce a document
unless every check the stage owes was recorded as passed, inside the window
between the release commit and the report, on a named host, with a soak that
actually ran for its full hour in all three phases. The rendered document is
then whatever the acceptance reader will accept, or nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from asl_workbench import lifecycle
from asl_workbench.installation import atomic
from asl_workbench.integrations import PI_SITE, IntegrationSite
from asl_workbench.notifications import DEPLOYMENT_STAGING

# ---------------------------------------------------------------------------
# What the rehearsal is allowed to be

#: The Serve port, the loopback port and the origin the rehearsal publishes on.
#: Chosen beside the production pair rather than over it: 9443 and 8766 belong
#: to the deployed Companion for the whole of this session.
STAGING_PORT = "9444"
STAGING_BIND_PORT = 8767

#: The prefixes, by what each one rehearses. Three on the Pi and two on the
#: Mac, because "install a release" and "migrate the installation that is
#: already there" cannot be rehearsed in the same directory: the second needs a
#: copy of a Companion prefix as its starting state.
STAGING_PREFIX_NAMES: Mapping[str, str] = {
    "service": "asl-workbench-staging",
    "service_rollback": "asl-workbench-staging-rollback",
    "service_migration": "asl-workbench-staging-migration",
    "connector": "asl-workbench-staging",
    "connector_migration": "asl-workbench-staging-migration",
}

#: The prefix of every native name this rehearsal installs under.
STAGING_LABEL_DOMAIN = "com.sglavoie."

#: The prefixes this rehearsal may not touch, by bare name. Both hosts install
#: the Companion under the same one.
PRODUCTION_PREFIX_NAME = "asl-companion"

#: Where the fixture corpus and the rehearsal's own state live on the Mac.
#: Outside every prefix, because they are the rehearsal's rather than any
#: installation's, and removing an installation must not remove the evidence.
STAGING_FIXTURE_NAME = "asl-workbench-staging-fixtures"

#: The environment variable that names a trust store to the connector's
#: ``ssl`` module. The rehearsal's staging origin is terminated by a
#: certificate no public CA signed, so this is how the staged connector is
#: told to trust exactly it -- and, because it replaces the default store
#: rather than adding to it, exactly nothing else.
TRUST_VARIABLE = "SSL_CERT_FILE"

#: The checks session 22 owes, restated here so the Pi half can be driven
#: without this repository. ``tests/test_workbench_staging.py`` pins this to
#: :data:`tests.workbench_acceptance.STAGE_CHECKS`, so a divergence is a test
#: failure rather than a report the acceptance reader then rejects.
STAGING_CHECKS: tuple[str, ...] = (
    "deployed-versions-inspected",
    "isolated-prefixes",
    "isolated-fixtures",
    "fresh-install",
    "migration-from-copied-state",
    "native-restart",
    "rollback",
    "recovery",
    "two-host-detail-reads",
    "two-host-controls",
    "production-routing-unchanged",
    "production-publishing-unchanged",
    "unrelated-service-health",
    "staging-cleanup",
)

#: The soak, and the three phases it is made of.
SOAK_MINIMUM_SECONDS = 3600.0
SOAK_PHASES: tuple[str, ...] = ("active", "idle", "reconnect")

#: The two hosts, and the operating system each one must report.
HOST_ROLES: Mapping[str, str] = {"mac": "Darwin", "pi": "Linux"}

#: Where a check may say it was observed.
CHECK_HOSTS: tuple[str, ...] = (*HOST_ROLES, "both")

SCHEMA_VERSION = 1
STAGE = "staging"
OUTCOME_PASSED = "passed"
PROVENANCE_ACTUAL_HOST = "actual-host"

#: How far a recorded moment may sit outside the window before it is read as
#: wrong rather than as skew between two machines' clocks.
MAX_CLOCK_SKEW_SECONDS = 300.0


class StagingError(AssertionError):
    """A rehearsal step, or a report built out of one, was refused."""


# ---------------------------------------------------------------------------
# The site the rehearsal runs against


def staging_site(site: IntegrationSite = PI_SITE) -> IntegrationSite:
    """The production site with every owned identifier moved aside.

    Everything a resource is *recognised* by changes -- the ports, the origin,
    the publisher, the compose marker, the monitor names -- so an integration
    run against this site cannot find, adopt, rename or remove anything the
    Companion installed. The host does not change, because
    :meth:`~asl_workbench.integrations.Integrations.identity` checks it against
    the machine the command is running on: a staging rehearsal on the Pi is
    still on the Pi.

    ``legacy_monitor_names`` and ``legacy_group_headings`` are emptied rather
    than renamed. They exist so an apply can adopt a resource this application
    installed under an older name, which is exactly the adoption a rehearsal
    must not be able to perform.
    """
    return replace(
        site,
        port=STAGING_PORT,
        bind_port=STAGING_BIND_PORT,
        public_origin=f"https://{site.host}:{STAGING_PORT}",
        publisher_user="asl-workbench-staging",
        publisher_topic="asl-workbench-staging",
        marker="# ASL workbench staging rehearsal v1",
        group_heading="ASL Workbench staging",
        entry_name="ASL staging",
        monitor_name="ASL Workbench staging service health",
        legacy_monitor_names=(),
        legacy_group_headings=(),
        notifications=None,
        deployment=DEPLOYMENT_STAGING,
        # Production's bridge forwards to production's bind port only.
        container_origin=None,
    )


#: The fields of a site that name a resource rather than describe one. A
#: rehearsal that shares one of these with production shares the resource.
OWNED_SITE_FIELDS: tuple[str, ...] = (
    "port",
    "bind_port",
    "public_origin",
    "publisher_user",
    "publisher_topic",
    "marker",
    "group_heading",
    "monitor_name",
)


def check_isolated(
    staging: IntegrationSite, production: IntegrationSite = PI_SITE
) -> None:
    """Refuse a staging site that shares anything production is known by.

    The publisher is checked twice over: the topic has to differ *and* the site
    has to carry no notification site at all, because a rehearsal that held a
    production credential would be a second publisher for the topic a phone is
    subscribed to even if it never chose to use it.
    """
    for field in OWNED_SITE_FIELDS:
        if getattr(staging, field) == getattr(production, field):
            raise StagingError(
                f"staging site reuses the production {field}: "
                f"{getattr(staging, field)!r}"
            )
    if staging.authority == production.authority:
        raise StagingError("staging site reuses the production Serve authority")
    if staging.target == production.target:
        raise StagingError("staging site reuses the production loopback target")
    if staging.notifications is not None:
        raise StagingError(
            "a staging site carries no notification site; a rehearsal that "
            "could mint a publisher would be a second publisher"
        )
    if staging.deployment != DEPLOYMENT_STAGING:
        raise StagingError(
            f"staging site is recorded as the {staging.deployment!r} deployment"
        )
    adoptable = (*staging.legacy_monitor_names, *staging.legacy_group_headings)
    if adoptable:
        raise StagingError(
            "staging site could adopt a resource installed under an older "
            f"name: {', '.join(repr(name) for name in adoptable)}"
        )


def check_prefix_isolated(
    prefix: Path, production_name: str = PRODUCTION_PREFIX_NAME
) -> None:
    """Refuse a staging prefix that is, contains, or sits inside production's.

    Name containment rather than resolution: the production prefix is on the
    other host half the time, so there is nothing local to compare against, and
    ``asl-companion`` appearing anywhere in the path is the mistake this
    catches either way.
    """
    parts = Path(prefix).parts
    if not parts:
        raise StagingError("a staging prefix is required")
    if any(part == production_name for part in parts):
        raise StagingError(
            f"{prefix} reaches the production installation at {production_name!r}"
        )
    if Path(prefix).name not in set(STAGING_PREFIX_NAMES.values()):
        raise StagingError(f"{prefix} is not one of this rehearsal's prefixes")


def staging_names(prefix: Path) -> tuple[str, str]:
    """The launch agent label and unit name one rehearsal prefix installs under.

    Derived from the prefix rather than fixed, because the rehearsal runs three
    prefixes on the Pi at once -- one soaking, one being interrupted and
    recovered, one being migrated -- and the release's own ``check_owner``
    correctly refuses to act on a definition that points at another prefix. One
    name for all three would make that refusal fire between two halves of the
    same rehearsal instead of between the rehearsal and production, which is
    how this was found: an interrupted install could not be recovered because
    the unit of the prefix that was soaking answered to its name.
    """
    check_prefix_isolated(prefix)
    name = Path(prefix).name
    return STAGING_LABEL_DOMAIN + name, name + ".service"


#: The names the rehearsal's primary prefix -- the one that soaks -- runs under.
STAGING_LABEL = STAGING_LABEL_DOMAIN + STAGING_PREFIX_NAMES["service"]
STAGING_UNIT = STAGING_PREFIX_NAMES["service"] + ".service"


# ---------------------------------------------------------------------------
# Native definitions, restaged


def staging_serve_program(site: IntegrationSite) -> str:
    """The one-line program the staging unit starts instead of ``-m runtime``.

    ``asl_workbench.runtime``'s ``main`` binds the production site's fixed
    ``127.0.0.1:8766``, which the deployed Companion holds for the whole of the
    rehearsal, and it takes no argument that would move it. ``runtime.run``
    does take one, so the staging unit calls it with the staging site and the
    release serves the same everything on another port.

    One line, and no file: a script inside the release directory would make the
    installed release differ from the artifact it was unpacked from, which the
    next install refuses -- correctly.

    The overrides are derived from the site rather than listed, so the process
    the unit starts is bound to the same site :func:`check_isolated` passed and
    not to a subset of it that happens to be enough to move the port.
    """
    overrides = ", ".join(
        f"{name}={value!r}" for name, value in site_overrides(site).items()
    )
    return (
        "import sys;"
        "from dataclasses import replace;"
        "from pathlib import Path;"
        "from asl_workbench import runtime;"
        "from asl_workbench.integrations import PI_SITE;"
        f"runtime.run(Path(sys.argv[1]), site=replace(PI_SITE, {overrides}))"
    )


def site_overrides(
    site: IntegrationSite, production: IntegrationSite = PI_SITE
) -> dict[str, Any]:
    """Every field in which this site differs from the production one."""
    return {
        field.name: getattr(site, field.name)
        for field in fields(production)
        if getattr(site, field.name) != getattr(production, field.name)
    }


def staging_service_arguments(
    prefix: Path, python: str, site: IntegrationSite
) -> list[str]:
    """What the staging unit runs: the shipped flags, the staging site."""
    return [python, "-E", "-s", "-B", "-c", staging_serve_program(site), str(prefix)]


def staging_environment(fixtures: Path) -> dict[str, str]:
    """The variables the staged connector needs and the release cannot name.

    Three, and each one is a resource the rehearsal must not share:

    * :data:`TRUST_VARIABLE` -- the rehearsal terminates its own TLS, so the
      staging origin is presented by a certificate no public CA signed and the
      connector, which builds ``ssl.create_default_context()``, has to be told
      which one to trust.
    * ``XDG_DATA_HOME`` and ``TMPDIR`` -- the connector's observation source
      scans its named plan roots *and* the default ones, which is right for a
      deployment that is meant to see everything on the machine and wrong for a
      rehearsal: those defaults are where the operator's real plans are, and
      publishing one of them to a staging store would make the fixture corpus
      not a fixture. Pointed at directories that exist and are empty, so the
      scan runs and finds nothing rather than being skipped.

    The release cannot name any of the three, because all three are facts about
    a rehearsal rather than about a deployment. They are still checked the way
    a prefix is: every value lives under the rehearsal's own state, so cleanup
    that removes that directory removes these too.
    """
    root = Path(fixtures)
    return {
        TRUST_VARIABLE: str(root / "tls" / "bundle.pem"),
        "XDG_DATA_HOME": str(root / "xdg"),
        "TMPDIR": str(root / "tmp"),
    }


def restage_plist(
    content: bytes,
    *,
    label: str = STAGING_LABEL,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    """The launch agent the release wrote, under a name launchd has free.

    Only the ``Label`` and, when the rehearsal has variables of its own, the
    environment move. The label is the one field that is an identity rather
    than a description, and launchd keys the service on the value inside the
    file rather than on the file's name -- so renaming the file alone would
    bootstrap a second definition of the running Companion.

    ``environment`` is the second staging deviation, and it exists for the same
    reason the ``ExecStart`` override exists on the Pi side: the rehearsal has
    to move something the release fixes, and the release is not the thing under
    rehearsal's control. It *extends* the environment the release wrote and
    never replaces it -- a variable the release already sets is refused rather
    than overwritten, because at that point the rehearsal would be changing a
    decision the release made -- and every value has to be a path under the
    rehearsal's own state, so nothing it names outlives cleanup. See
    :func:`staging_environment` for what is passed and why.
    """
    parsed = plistlib.loads(content)
    if parsed.get("Label") != lifecycle.LABEL:
        raise StagingError(
            "this is not the launch agent the release writes; its Label is "
            f"{parsed.get('Label')!r}"
        )
    if label == lifecycle.LABEL:
        raise StagingError("the staging label may not be the production label")
    staged: dict[str, Any] = {**parsed, "Label": label}
    if environment:
        shipped = dict(parsed.get("EnvironmentVariables") or {})
        for name, value in sorted(environment.items()):
            if name in shipped:
                raise StagingError(
                    f"the release sets {name} itself; a rehearsal extends the "
                    "environment it was given and replaces nothing in it"
                )
            path = Path(value)
            if not path.is_absolute() or STAGING_FIXTURE_NAME not in path.parts:
                raise StagingError(
                    f"{name} must name a path under {STAGING_FIXTURE_NAME}, "
                    "which is what cleanup removes"
                )
            if not path.exists():
                raise StagingError(f"{name} names {path}, which does not exist")
        staged["EnvironmentVariables"] = {**shipped, **environment}
    return plistlib.dumps(staged, sort_keys=True)


#: The escapes :func:`~asl_workbench.lifecycle.quote_unit` writes, read back.
_UNIT_ESCAPES: Mapping[str, str] = {"\\": "\\", '"': '"', "n": "\n", "r": "\r"}


def unquote_unit(line: str) -> list[str]:
    """The words behind one quoted systemd command line.

    The inverse of :func:`~asl_workbench.lifecycle.quote_unit`, and strict
    about it: an unquoted word or an escape that function never writes means
    this is not a line this module produced, which is exactly when guessing at
    its shape would be wrong.
    """
    words: list[str] = []
    index = 0
    while index < len(line):
        if line[index] == " ":
            index += 1
            continue
        if line[index] != '"':
            raise StagingError("unquoted word in a systemd command line")
        index += 1
        buffer: list[str] = []
        while True:
            if index >= len(line):
                raise StagingError("unterminated word in a systemd command line")
            char = line[index]
            if char == '"':
                index += 1
                break
            if char == "\\":
                escape = line[index + 1 : index + 2]
                if escape not in _UNIT_ESCAPES:
                    raise StagingError(f"unknown escape in a command line: \\{escape}")
                buffer.append(_UNIT_ESCAPES[escape])
                index += 2
                continue
            if line[index : index + 2] in ("%%", "$$"):
                buffer.append(char)
                index += 2
                continue
            buffer.append(char)
            index += 1
        words.append("".join(buffer))
    return words


def restage_unit(content: bytes, *, site: IntegrationSite) -> bytes:
    """The unit the release wrote, starting the release against the staging site.

    The shipped ``ExecStart`` is read back and checked against what
    :func:`~asl_workbench.lifecycle.service_arguments` would have produced for
    the prefix and interpreter named in it, so a unit whose shape changed
    upstream fails here rather than being quietly reconstructed out of what
    this module remembers of it. Nothing else in the unit moves: the working
    directory, the environment, the restart policy and the kill mode are the
    release's own.
    """
    lines = content.decode("utf-8").splitlines()
    shipped = [line for line in lines if line.startswith("ExecStart=")]
    if len(shipped) != 1:
        raise StagingError(f"a unit has one ExecStart; this one has {len(shipped)}")
    words = unquote_unit(shipped[0].removeprefix("ExecStart="))
    if len(words) < 2 or words[-2] != "--prefix":
        raise StagingError("this is not the unit the release writes for a prefix")
    python, prefix = words[0], Path(words[-1])
    if words != lifecycle.service_arguments(prefix, python):
        raise StagingError("this ExecStart is not the one service_arguments produces")
    staged = "ExecStart=" + " ".join(
        lifecycle.quote_unit(word)
        for word in staging_service_arguments(prefix, python, site)
    )
    return (
        "\n".join(staged if line == shipped[0] else line for line in lines) + "\n"
    ).encode("utf-8")


class StagingNativeManager(lifecycle.NativeManager):
    """The release's native manager, pointed at names production is not using.

    Subclassed rather than reimplemented: every command, every check and every
    ordering is the one the release performs, and what changes is the name the
    definition is installed under and the content rewrite that makes that name
    honest.
    """

    def __init__(
        self,
        role: str,
        *,
        prefix: Path,
        site: IntegrationSite | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.role = role
        self.prefix = Path(prefix)
        self.site = site or staging_site()
        self.environment = dict(environment) if environment else None
        self.label, self.unit = staging_names(self.prefix)
        self.domain = f"gui/{os.getuid()}"
        self.destination = Path.home() / (
            "Library/LaunchAgents/" + self.label + ".plist"
            if role == lifecycle.ROLE_CONNECTOR
            else ".config/systemd/user/" + self.unit
        )

    @property
    def name(self) -> str:
        """The staging service's name, in its own manager's vocabulary."""
        return self.label if self.role == lifecycle.ROLE_CONNECTOR else self.unit

    def check_owner(self, prefix: Path) -> None:
        if Path(prefix) != self.prefix:
            raise StagingError("this manager was built for another staging prefix")
        if not self.destination.exists() and not self.destination.is_symlink():
            return
        if (
            self.destination.is_symlink()
            or self.destination.stat().st_uid != os.getuid()
        ):
            raise StagingError("staging definition is not owned by this user")
        raw = self.destination.read_bytes()
        try:
            if self.role == lifecycle.ROLE_CONNECTOR:
                parsed = plistlib.loads(raw)
                owned = parsed.get("Label") == self.label and str(
                    prefix / lifecycle.CONNECTOR_TOKEN_PATH
                ) in list(parsed.get("ProgramArguments", []))
            else:
                owned = any(
                    line.startswith("ExecStart=")
                    and line.endswith(" " + lifecycle.quote_unit(prefix))
                    for line in raw.decode("utf-8").splitlines()
                )
        except (ValueError, plistlib.InvalidFileException):
            owned = False
        if not owned:
            raise StagingError("staging definition belongs to another rehearsal")

    def status(self) -> bool:
        if self.role == lifecycle.ROLE_CONNECTOR:
            return (
                lifecycle.command(
                    ["/bin/launchctl", "print", self.domain + "/" + self.label],
                    check=False,
                ).returncode
                == 0
            )
        return (
            lifecycle.command(
                ["systemctl", "--user", "is-active", "--quiet", self.unit],
                check=False,
            ).returncode
            == 0
        )

    def start(self, definition_path: Path) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        atomic(self.destination, self.restaged(definition_path))
        if self.role == lifecycle.ROLE_CONNECTOR:
            lifecycle.command(
                ["/bin/launchctl", "bootstrap", self.domain, str(self.destination)]
            )
        else:
            lifecycle.command(["systemctl", "--user", "daemon-reload"])
            lifecycle.command(["systemctl", "--user", "enable", "--now", self.unit])
        if not self.status():
            raise StagingError("staging service did not become active")

    def restaged(self, definition_path: Path) -> bytes:
        """The definition the installation produced, under staging names."""
        content = definition_path.read_bytes()
        if self.role == lifecycle.ROLE_CONNECTOR:
            return restage_plist(
                content, label=self.label, environment=self.environment
            )
        return restage_unit(content, site=self.site)

    def stop(self) -> None:
        if self.role == lifecycle.ROLE_CONNECTOR:
            if self.status():
                lifecycle.command(
                    ["/bin/launchctl", "bootout", self.domain + "/" + self.label]
                )
            return
        lifecycle.command(["systemctl", "--user", "stop", self.unit])

    def remove(self) -> None:
        self.stop()
        if self.role == lifecycle.ROLE_SERVICE and self.destination.exists():
            lifecycle.command(["systemctl", "--user", "disable", self.unit])
        self.destination.unlink(missing_ok=True)
        if self.role == lifecycle.ROLE_SERVICE:
            lifecycle.command(["systemctl", "--user", "daemon-reload"])

    def restart(self) -> None:
        """Stop and start through the service manager, as an operator would."""
        if self.role == lifecycle.ROLE_CONNECTOR:
            lifecycle.command(
                ["/bin/launchctl", "kickstart", "-k", self.domain + "/" + self.label]
            )
        else:
            lifecycle.command(["systemctl", "--user", "restart", self.unit])
        if not self.status():
            raise StagingError("staging service did not come back")


def staging_manager_factory(
    prefix: Path,
    site: IntegrationSite | None = None,
    environment: Mapping[str, str] | None = None,
) -> Any:
    """A ``manager_factory`` for :class:`~asl_workbench.lifecycle.Installation`."""
    resolved = site or staging_site()

    def factory(role: str) -> StagingNativeManager:
        return StagingNativeManager(
            role, prefix=prefix, site=resolved, environment=environment
        )

    return factory


# ---------------------------------------------------------------------------
# The journal, and the report it may become


class Journal:
    """Every observation the rehearsal made, in the order it made them.

    Append-only and persisted after every write, because the rehearsal spans an
    hour of soak and two hosts: a journal held in memory would lose its first
    half to any interruption, and a rehearsal whose evidence is gone is a
    rehearsal that has to be run again.
    """

    def __init__(self, path: Path, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.data: dict[str, Any] = {
            "release": None,
            "hosts": {},
            "checks": {},
            "soak": None,
            "notes": [],
        }
        if self.path.exists():
            stored = json.loads(self.path.read_bytes())
            if not isinstance(stored, dict):
                raise StagingError(f"{self.path.name} is not a JSON object")
            self.data.update(stored)

    # -- writing -----------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic(self.path, json.dumps(self.data, indent=2, sort_keys=True).encode())

    def release(self, revision: str, artifact: str) -> None:
        self.data["release"] = {"revision": revision, "artifact": artifact}
        self.save()

    def host(
        self,
        role: str,
        *,
        host: str,
        system: str,
        release: str,
        observed_at: float | None = None,
    ) -> None:
        if role not in HOST_ROLES:
            raise StagingError(f"{role!r} is not a deployed host role")
        self.data["hosts"][role] = {
            "kind": PROVENANCE_ACTUAL_HOST,
            "host": host,
            "system": system,
            "release": release,
            "observed_at": float(
                observed_at if observed_at is not None else self.clock()
            ),
        }
        self.save()

    def record(
        self,
        name: str,
        *,
        host: str,
        outcome: str = OUTCOME_PASSED,
        observed_at: float | None = None,
        note: str | None = None,
    ) -> None:
        """One check, as it was observed. A second record replaces the first.

        Replacing rather than appending is what makes a rehearsal repeatable
        after a step failed: the failing record is the one that has to stop the
        report, and it does, right up until the step is actually redone.
        """
        if name not in STAGING_CHECKS:
            raise StagingError(f"{name!r} is not a check this stage owes")
        if host not in CHECK_HOSTS:
            raise StagingError(f"{host!r} is not a deployed host")
        self.data["checks"][name] = {
            "outcome": outcome,
            "host": host,
            "observed_at": float(
                observed_at if observed_at is not None else self.clock()
            ),
        }
        if note:
            self.data["notes"].append({"check": name, "note": note})
        self.save()

    def soak(
        self, *, started_at: float, ended_at: float, phases: Mapping[str, float]
    ) -> None:
        missing = [phase for phase in SOAK_PHASES if phase not in phases]
        if missing:
            raise StagingError(f"the soak has no {', '.join(missing)} phase")
        extra = sorted(set(phases) - set(SOAK_PHASES))
        if extra:
            raise StagingError(f"unknown soak phase: {', '.join(extra)}")
        self.data["soak"] = {
            "started_at": float(started_at),
            "ended_at": float(ended_at),
            "phases": {phase: float(phases[phase]) for phase in SOAK_PHASES},
        }
        self.save()

    # -- reading -----------------------------------------------------------

    def missing(self) -> list[str]:
        """Which checks have not been recorded as passed yet."""
        checks = self.data.get("checks") or {}
        return [
            name
            for name in STAGING_CHECKS
            if (checks.get(name) or {}).get("outcome") != OUTCOME_PASSED
        ]

    def render(
        self, *, committed_at: float, recorded_at: float | None = None
    ) -> dict[str, Any]:
        """The report, or a refusal naming what was never observed.

        ``committed_at`` is the timestamp of the commit the evidence accepts.
        Every moment in the document has to sit between it and the report, and
        the check is here rather than only in the acceptance reader because a
        rehearsal that recorded a step before the release existed recorded a
        step about another release.
        """
        recorded = float(recorded_at if recorded_at is not None else self.clock())
        release = self.data.get("release")
        if not isinstance(release, dict) or set(release) != {"revision", "artifact"}:
            raise StagingError("the rehearsal recorded no release")
        hosts = self.data.get("hosts") or {}
        for role in HOST_ROLES:
            if role not in hosts:
                raise StagingError(f"the rehearsal recorded no {role} host")
            entry = hosts[role]
            if entry.get("system") != HOST_ROLES[role]:
                raise StagingError(
                    f"the {role} reported {entry.get('system')!r}, "
                    f"not {HOST_ROLES[role]}"
                )
            if entry.get("release") != release["artifact"]:
                raise StagingError(
                    f"the {role} ran a release this report does not accept"
                )
            _window(entry.get("observed_at"), committed_at, recorded, f"hosts.{role}")
        if hosts["mac"]["host"] == hosts["pi"]["host"]:
            raise StagingError(
                "both halves name the same machine; a two-host rehearsal "
                "recorded from one host is not a two-host rehearsal"
            )
        missing = self.missing()
        if missing:
            raise StagingError("the rehearsal did not record: " + ", ".join(missing))
        checks = self.data["checks"]
        for name in STAGING_CHECKS:
            _window(
                checks[name].get("observed_at"),
                committed_at,
                recorded,
                f"checks.{name}",
            )
        soak = self.data.get("soak")
        if not isinstance(soak, dict):
            raise StagingError("the rehearsal recorded no soak")
        _window(soak["started_at"], committed_at, recorded, "soak.started_at")
        _window(soak["ended_at"], committed_at, recorded, "soak.ended_at")
        lasted = soak["ended_at"] - soak["started_at"]
        if lasted < SOAK_MINIMUM_SECONDS:
            raise StagingError(
                f"the soak lasted {lasted:.0f}s, short of {SOAK_MINIMUM_SECONDS:.0f}s"
            )
        for phase in SOAK_PHASES:
            if soak["phases"][phase] <= 0:
                raise StagingError(f"the soak's {phase} phase never happened")
        if sum(soak["phases"].values()) > lasted + MAX_CLOCK_SKEW_SECONDS:
            raise StagingError("the soak's phases add up to more than it lasted")
        return {
            "schema": SCHEMA_VERSION,
            "stage": STAGE,
            "recorded_at": recorded,
            "release": dict(release),
            "hosts": {role: dict(hosts[role]) for role in HOST_ROLES},
            "checks": {
                name: {
                    "outcome": checks[name]["outcome"],
                    "observed_at": checks[name]["observed_at"],
                    "host": checks[name]["host"],
                }
                for name in STAGING_CHECKS
            },
            "soak": {
                "started_at": soak["started_at"],
                "ended_at": soak["ended_at"],
                "phases": {phase: soak["phases"][phase] for phase in SOAK_PHASES},
            },
        }


def _window(value: Any, floor: float, ceiling: float, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StagingError(f"{where}: a unix timestamp is required")
    moment = float(value)
    if moment < floor:
        raise StagingError(f"{where}: predates the release it is evidence for")
    if moment > ceiling + MAX_CLOCK_SKEW_SECONDS:
        raise StagingError(f"{where}: is later than the report that carries it")
    return moment


# ---------------------------------------------------------------------------
# The half that runs on the Pi
#
# Driven over ssh, one bounded command at a time, so every mutation the
# rehearsal makes to the other host is a line somebody can read back.


def _installation(
    prefix: Path,
    site: IntegrationSite,
    environment: Mapping[str, str] | None = None,
) -> lifecycle.Installation:
    check_prefix_isolated(prefix)
    return lifecycle.Installation(
        prefix,
        manager_factory=staging_manager_factory(prefix, site, environment),
        site=site,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """One rehearsal operation, named, against the staging site."""
    parser = argparse.ArgumentParser(
        prog="workbench_staging",
        description="Drive one step of the deployed staging rehearsal.",
    )
    parser.add_argument(
        "action",
        choices=(
            "site",
            "install",
            "upgrade",
            "status",
            "rollback",
            "recover",
            "restart",
            "remove",
            "migrate",
        ),
    )
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--python")
    parser.add_argument("--target-id")
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--role", default=lifecycle.ROLE_SERVICE)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--application-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    site = staging_site()
    check_isolated(site)
    if args.action == "site":
        label, unit = (
            staging_names(args.prefix)
            if args.prefix
            else (
                STAGING_LABEL,
                STAGING_UNIT,
            )
        )
        print(
            json.dumps(
                {
                    "port": site.port,
                    "bind_port": site.bind_port,
                    "public_origin": site.public_origin,
                    "deployment": site.deployment,
                    "label": label,
                    "unit": unit,
                    "serve": shlex.join(
                        [
                            "tailscale",
                            "serve",
                            "--bg",
                            f"--https={site.port}",
                            site.target,
                        ]
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    if not args.prefix:
        parser.error("--prefix is required")
    # Only the Mac half has an environment of its own: the Pi's staging unit
    # moves its site in the ``ExecStart`` line instead, and nothing about the
    # service depends on where a connector's plan roots are.
    environment = staging_environment(args.fixtures) if args.fixtures else None
    if args.action == "restart":
        StagingNativeManager(
            args.role, prefix=args.prefix, site=site, environment=environment
        ).restart()
        print(json.dumps({"restarted": args.role}, sort_keys=True))
        return 0
    if args.action == "migrate":
        from asl_workbench.migration import Migration  # noqa: PLC0415

        check_prefix_isolated(args.prefix)
        migration = Migration(args.prefix, target_id=args.target_id, site=site)
        print(
            json.dumps(
                migration.plan() if args.dry_run else migration.apply(), sort_keys=True
            )
        )
        return 0
    installation = _installation(args.prefix, site, environment)
    if args.action in ("install", "upgrade"):
        if not args.python:
            parser.error("--python is required; a release runs on a named interpreter")
        result = installation.install(
            artifact=args.artifact,
            digest=args.sha256,
            python=args.python,
            activate=args.activate,
            upgrade=args.action == "upgrade",
            dry_run=args.dry_run,
        )
    elif args.action == "status":
        result = installation.status()
    elif args.action == "rollback":
        result = installation.rollback(data=not args.application_only)
    elif args.action == "recover":
        result = installation.recover()
    else:
        result = installation.remove(confirm=args.confirm, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - the rehearsal's entry point
    sys.exit(main())
