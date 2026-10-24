# ruff: noqa: F403, F405
"""Staging-site isolation and native service-definition rewrites."""

from __future__ import annotations

from . import lifecycle_common

# ruff: noqa: F403, F405
from .staging_rehearsal_models import *


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
                lifecycle_common.await_unloaded(self.status)
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
