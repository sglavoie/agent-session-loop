"""Focused portion of tests/workbench_cutover.py."""

from .workbench_cutover_backup import *

# ---------------------------------------------------------------------------
# One production deployment, not two


def check_single_owner(
    prefix: Path, *, role: str, site: IntegrationSite = PI_SITE
) -> dict[str, Any]:
    """Refuse a definition that would leave two production processes behind.

    The release writes one launch agent on the Mac and one unit on the Pi, both
    under the Companion's own names, which is what makes the unified deployment
    a replacement rather than a neighbour. That is only true if the definition
    the prefix carries really is the one the release produced for *this*
    prefix: a definition left over from a rehearsal, or copied in with a
    migrated prefix, names a label this host answers to for a directory that is
    not there any more.

    What is returned is the identity the definition actually carries, so a
    caller can record it rather than restate it.
    """
    check_prefix_production(prefix)
    path = Path(prefix) / lifecycle.DEFINITIONS_DIRECTORY
    label, unit = lifecycle.LABEL, lifecycle.UNIT
    if role == lifecycle.ROLE_CONNECTOR:
        definition = path / f"{label}.plist"
        if not definition.is_file():
            raise CutoverError(f"{definition} is not there; nothing is installed")
        parsed = plistlib.loads(definition.read_bytes())
        found = str(parsed.get("Label") or "")
        if found != label:
            raise CutoverError(f"the launch agent names {found!r}, not {label!r}")
        arguments = [str(item) for item in parsed.get("ProgramArguments") or ()]
        # The prefix appears in the arguments as the directory the credential
        # lives under rather than as a word of its own, so this matches the way
        # the release actually writes the agent: the working directory *is* the
        # prefix, and at least one argument has to be inside it.
        if str(parsed.get("WorkingDirectory") or "") != str(prefix):
            raise CutoverError("the launch agent does not run in this prefix")
        if not any(item.startswith(f"{prefix}/") for item in arguments):
            raise CutoverError("the launch agent reads nothing out of this prefix")
        origin = next(
            (
                value
                for flag, value in zip(arguments, arguments[1:], strict=False)
                if flag == "--origin"
            ),
            "",
        )
        if origin != site.public_origin:
            # This is the private URL, in the one place a cutover could lose it
            # without noticing: a connector pointed somewhere else keeps
            # working, against the wrong service.
            raise CutoverError(
                f"the launch agent connects to {origin!r}, not the preserved "
                f"private URL {site.public_origin!r}"
            )
        return {"label": found, "origin": origin, "arguments": arguments}
    definition = path / unit
    if not definition.is_file():
        raise CutoverError(f"{definition} is not there; nothing is installed")
    content = definition.read_text(encoding="utf-8")
    start = next(
        (line for line in content.splitlines() if line.startswith("ExecStart=")), ""
    )
    if not start:
        raise CutoverError(f"{unit} declares no ExecStart")
    # Quoted rather than as a substring: ``asl-companion`` is a prefix of
    # ``asl-companion-old``, so a plain containment test accepts a unit that
    # still starts the installation this one was supposed to replace.
    if lifecycle.quote_unit(prefix) not in start:
        raise CutoverError(f"{unit} does not point at this prefix")
    if str(site.bind_port) in start or "--site" in start:
        raise CutoverError(
            f"{unit} moves its site aside; the production unit serves "
            f"{site.target}, which it takes from the release rather than its "
            "command line"
        )
    return {"unit": unit, "exec_start": start}


# ---------------------------------------------------------------------------
# The journal, and the report it may become


class Journal:
    """Every observation the cutover made, in the order it made them.

    Append-only and persisted after every write, because a cutover spans two
    hosts and an unbounded wait for a person to pick up their phone: a journal
    held in memory would lose the technical half to any interruption, and the
    technical half is the part that cannot simply be redone once the legacy
    installation has been migrated.
    """

    def __init__(self, path: Path, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.data: dict[str, Any] = {
            "release": None,
            "hosts": {},
            "checks": {},
            "phone": {"readiness": None, "confirmation": None},
            "notes": [],
        }
        if self.path.exists():
            stored = json.loads(self.path.read_bytes())
            if not isinstance(stored, dict):
                raise CutoverError(f"{self.path.name} is not a JSON object")
            self.data.update(stored)

    # -- writing -----------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic(self.path, json.dumps(self.data, indent=2, sort_keys=True).encode())

    def release(self, revision: str, artifact: str) -> None:
        if not _REVISION.fullmatch(revision):
            raise CutoverError(f"{revision!r} is not a full commit revision")
        if not _DIGEST.fullmatch(artifact):
            raise CutoverError(f"{artifact!r} is not an artifact digest")
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
            raise CutoverError(f"{role!r} is not a deployed host role")
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

        Replacing rather than appending is what makes a cutover step repeatable
        after it failed: the failing record is the one that has to stop the
        report, and it does, right up until the step is actually redone.
        """
        if name not in PRODUCTION_CHECKS:
            raise CutoverError(f"{name!r} is not a check this stage owes")
        if host not in CHECK_HOSTS:
            raise CutoverError(f"{host!r} is not a deployed host")
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

    def phone_readiness(
        self,
        *,
        requested_at: float,
        granted_at: float | None = None,
        by: str = "owner",
    ) -> None:
        """The request that the operator lock their phone, and their answer.

        Recorded in two steps deliberately: ``granted_at`` is ``None`` until
        the person answers, and no fixture may be started while it is. The plan
        forbids starting an alert from an unanswered readiness request, and a
        field that could only be filled in after the fact is how that is kept.
        """
        if not _NAME.fullmatch(by):
            raise CutoverError(f"{by!r} is not a usable name")
        if granted_at is not None and float(granted_at) < float(requested_at):
            raise CutoverError("readiness was granted before it was asked for")
        self.data["phone"]["readiness"] = {
            "requested_at": float(requested_at),
            "granted_at": None if granted_at is None else float(granted_at),
            "by": by,
        }
        self.save()

    def granted(self) -> float:
        """When the operator declared the phone ready, or a refusal."""
        readiness = (self.data.get("phone") or {}).get("readiness")
        if not isinstance(readiness, dict) or readiness.get("granted_at") is None:
            raise CutoverError(
                "the operator has not declared the phone ready; a fixture "
                "started now would be an alert nobody agreed to receive"
            )
        return float(readiness["granted_at"])

    def phone_confirmation(
        self,
        *,
        requested_at: float,
        answered_at: float | None = None,
        verdict: str = VERDICT_PENDING,
        release: str,
        host: str,
        by: str = "owner",
        checks: Mapping[str, bool] | None = None,
    ) -> None:
        """What the operator was asked about the phone, and what they answered.

        The confirmation may only be asked for after readiness was granted --
        :meth:`granted` is called rather than trusted -- and until an answer
        arrives the verdict is ``pending``, which :meth:`render` refuses. That
        is the whole of "an unanswered request leaves acceptance pending": it is
        not a state this module can be talked out of, because the only way past
        it is a timestamp and four answers that did not exist before.
        """
        granted = self.granted()
        if float(requested_at) < granted:
            raise CutoverError(
                "the confirmation was asked for before the operator declared "
                "the phone ready"
            )
        if verdict not in (VERDICT_ACCEPTED, VERDICT_PENDING):
            raise CutoverError(f"{verdict!r} is not a verdict a person gave")
        if verdict == VERDICT_ACCEPTED and answered_at is None:
            raise CutoverError("an accepted confirmation has a moment it was answered")
        if answered_at is not None and float(answered_at) < float(requested_at):
            raise CutoverError("the confirmation was answered before it was asked")
        if not _DIGEST.fullmatch(release):
            raise CutoverError(f"{release!r} is not an artifact digest")
        self._check_device(host)
        if not _NAME.fullmatch(by):
            raise CutoverError(f"{by!r} is not a usable name")
        answers = dict.fromkeys(PHONE_CHECKS, False)
        for name, value in (checks or {}).items():
            if name not in PHONE_CHECKS:
                raise CutoverError(f"{name!r} is not a check the phone answers")
            answers[name] = bool(value)
        if verdict == VERDICT_ACCEPTED and not all(answers.values()):
            raise CutoverError(
                "an accepted confirmation answers all four phone checks: "
                + ", ".join(name for name, value in answers.items() if not value)
                + " was not confirmed"
            )
        self.data["phone"]["confirmation"] = {
            "requested_at": float(requested_at),
            "answered_at": None if answered_at is None else float(answered_at),
            "verdict": verdict,
            "release": release,
            "kind": PROVENANCE_ACTUAL_HOST,
            "host": host,
            "by": by,
            "checks": answers,
        }
        self.save()

    def _check_device(self, host: str) -> None:
        if not _HOSTNAME.fullmatch(host):
            raise CutoverError(f"{host!r} is not a device name")
        folded = host.lower()
        for marker in FORBIDDEN_HOST_MARKERS:
            if marker in folded:
                raise CutoverError(f"{host!r} names a fixture rather than a phone")
        deployed = {
            str(entry.get("host"))
            for entry in (self.data.get("hosts") or {}).values()
            if isinstance(entry, dict)
        }
        if host in deployed:
            raise CutoverError(
                f"{host!r} is a deployed host; a phone acceptance confirmed from "
                "the Mac or the Pi is not a phone acceptance"
            )

    # -- reading -----------------------------------------------------------

    def missing(self) -> list[str]:
        """Which checks have not been recorded as passed yet."""
        checks = self.data.get("checks") or {}
        return [
            name
            for name in PRODUCTION_CHECKS
            if (checks.get(name) or {}).get("outcome") != OUTCOME_PASSED
        ]

    def render(
        self, *, committed_at: float, recorded_at: float | None = None
    ) -> dict[str, Any]:
        """The report, or a refusal naming what was never observed.

        ``committed_at`` is the timestamp of the commit the evidence accepts.
        Every moment in the document has to sit between it and the report, and
        the check is here rather than only in the acceptance reader because a
        cutover that recorded a step before the release existed recorded a step
        about another release.
        """
        recorded = float(recorded_at if recorded_at is not None else self.clock())
        release = self.data.get("release")
        if not isinstance(release, dict) or set(release) != {"revision", "artifact"}:
            raise CutoverError("the cutover recorded no release")
        hosts = self.data.get("hosts") or {}
        for role in HOST_ROLES:
            if role not in hosts:
                raise CutoverError(f"the cutover recorded no {role} host")
            entry = hosts[role]
            if entry.get("system") != HOST_ROLES[role]:
                raise CutoverError(
                    f"the {role} reported {entry.get('system')!r}, "
                    f"not {HOST_ROLES[role]}"
                )
            if entry.get("release") != release["artifact"]:
                raise CutoverError(
                    f"the {role} ran a release this report does not accept"
                )
            _window(entry.get("observed_at"), committed_at, recorded, f"hosts.{role}")
        if hosts["mac"]["host"] == hosts["pi"]["host"]:
            raise CutoverError(
                "both halves name the same machine; a two-host cutover recorded "
                "from one host is not a two-host cutover"
            )
        missing = self.missing()
        if missing:
            raise CutoverError("the cutover did not record: " + ", ".join(missing))
        checks = self.data["checks"]
        for name in PRODUCTION_CHECKS:
            _window(
                checks[name].get("observed_at"),
                committed_at,
                recorded,
                f"checks.{name}",
            )
        phone = self._rendered_phone(release["artifact"], committed_at, recorded)
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
                for name in PRODUCTION_CHECKS
            },
            "phone": phone,
        }

    def _rendered_phone(
        self, artifact: str, committed_at: float, recorded: float
    ) -> dict[str, Any]:
        """The human half of the report, or the refusal that keeps it pending."""
        phone = self.data.get("phone") or {}
        readiness = phone.get("readiness")
        if not isinstance(readiness, dict):
            raise CutoverError("the operator was never asked to ready the phone")
        if readiness.get("granted_at") is None:
            raise CutoverError(
                "the operator has not declared the phone ready; acceptance stays "
                "pending until they answer"
            )
        requested = _window(
            readiness.get("requested_at"),
            committed_at,
            recorded,
            "phone.readiness.requested_at",
        )
        granted = _window(
            readiness.get("granted_at"),
            committed_at,
            recorded,
            "phone.readiness.granted_at",
        )
        if granted < requested:
            raise CutoverError("readiness was granted before it was asked for")
        confirmation = phone.get("confirmation")
        if not isinstance(confirmation, dict):
            raise CutoverError("the operator was never asked to confirm the phone")
        if confirmation.get("verdict") != VERDICT_ACCEPTED:
            raise CutoverError(
                "the operator has not confirmed the phone; acceptance stays "
                "pending until they answer"
            )
        if confirmation.get("release") != artifact:
            raise CutoverError(
                "the operator confirmed a release this report does not accept"
            )
        asked = _window(
            confirmation.get("requested_at"),
            committed_at,
            recorded,
            "phone.confirmation.requested_at",
        )
        answered = _window(
            confirmation.get("answered_at"),
            committed_at,
            recorded,
            "phone.confirmation.answered_at",
        )
        if asked < granted:
            raise CutoverError(
                "the confirmation was asked for before the phone was declared ready"
            )
        if answered < asked:
            raise CutoverError("the confirmation was answered before it was asked")
        checks = confirmation.get("checks") or {}
        unconfirmed = [name for name in PHONE_CHECKS if checks.get(name) is not True]
        if unconfirmed:
            raise CutoverError(
                "the operator did not confirm: " + ", ".join(unconfirmed)
            )
        return {
            "readiness": {
                "requested_at": requested,
                "granted_at": granted,
                "by": str(readiness.get("by") or "owner"),
            },
            "confirmation": {
                "requested_at": asked,
                "answered_at": answered,
                "verdict": VERDICT_ACCEPTED,
                "release": artifact,
                "kind": PROVENANCE_ACTUAL_HOST,
                "host": str(confirmation.get("host")),
                "by": str(confirmation.get("by") or "owner"),
                "checks": dict.fromkeys(PHONE_CHECKS, True),
            },
        }


def _window(value: Any, floor: float, ceiling: float, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CutoverError(f"{where}: a unix timestamp is required")
    moment = float(value)
    if moment < floor:
        raise CutoverError(f"{where}: predates the release it is evidence for")
    if moment > ceiling + MAX_CLOCK_SKEW_SECONDS:
        raise CutoverError(f"{where}: is later than the report that carries it")
    return moment


# ---------------------------------------------------------------------------
# The half that runs on the Pi
#
# Driven over ssh, one bounded command at a time, so every mutation the cutover
# makes to the other host is a line somebody can read back.

__all__ = ["argparse", "hashlib", "json", "plistlib", "re", "sqlite3", "tarfile", "tempfile", "time", "Mapping", "Sequence", "closing", "fields", "Path", "Any", "lifecycle", "migration", "BACKUPS_DIRECTORY", "DEPLOYMENT_PATH", "PRIVATE_DIRECTORY_MODE", "PRIVATE_FILE_MODE", "RELEASES_DIRECTORY", "atomic", "document", "PI_SITE", "IntegrationSite", "DEPLOYMENT_PRODUCTION", "PRODUCTION_PREFIX_NAME", "OWNED_SITE_FIELDS", "PRODUCTION_CHECKS", "PHONE_CHECKS", "HOST_ROLES", "CHECK_HOSTS", "SCHEMA_VERSION", "STAGE", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "VERDICT_PENDING", "PROVENANCE_ACTUAL_HOST", "MAX_CLOCK_SKEW_SECONDS", "FORBIDDEN_HOST_MARKERS", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "CutoverError", "check_production", "check_prefix_production", "BACKUP_EXCLUDED", "backup_members", "BACKUP_SCHEMA_VERSION", "BACKUP_MANIFEST", "MAX_BACKUP_BYTES", "_digest_file", "table_counts", "_installed_artifact", "capture_backup", "verify_backup", "check_single_owner", "Journal", "_window"]  # fmt: skip
