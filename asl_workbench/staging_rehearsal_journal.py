# ruff: noqa: F401, F403, F405, I001
"""Append-only evidence journal for a staging rehearsal."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from .staging_rehearsal_models import *


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
