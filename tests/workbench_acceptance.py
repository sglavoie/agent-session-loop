"""The gate the deployment sessions are accepted through.

Sessions 22, 23 and 24 deploy the Workbench onto the two real hosts and then
have to answer one question that no suite in this repository can answer for
them: *did that actually happen, to this release, on those machines?* Every
other test here proves what the software would do. This module proves what was
done, and it is the only place in the tree whose verdict depends on a file
written by a human-supervised session rather than by a fixture.

It is therefore deliberately not a test of the application. It is a reader for
three sanitized reports kept in the Pi repository::

    sglavoie_raspberry_pi:services/pi-monitoring/docs/workbench/evidence/staging.json
    sglavoie_raspberry_pi:services/pi-monitoring/docs/workbench/evidence/production.json
    sglavoie_raspberry_pi:services/pi-monitoring/docs/workbench/evidence/retirement.json

and three ``unittest`` entry points over them, named so the deployment
contracts can run exactly one each::

    python3 -m unittest -q tests.workbench_acceptance.StagingEvidence
    python3 -m unittest -q tests.workbench_acceptance.ProductionEvidence
    python3 -m unittest -q tests.workbench_acceptance.RetirementEvidence

The module is named so ``unittest discover`` cannot reach it -- the pattern is
``test*.py`` -- because until a deployment session has run there is nothing to
read, and a gate that failed the whole suite for that reason would be a gate
somebody disabled. Its rules are proved instead by
``tests/test_workbench_acceptance.py``, which builds synthetic repositories and
documents and checks that each rejection actually fires.

What it refuses, and why each one is a way of lying about a deployment:

**Missing.** No file, an unreadable file, a file that is not a JSON object, or
a Pi checkout that is not there at all. Reported as a failure rather than a
skip: "we could not check" and "it passed" are different sentences, and only
one of them may end a deployment session.

**Wrong release.** The report names the release it accepts by commit and by
artifact digest, and the digest is checked by *rebuilding* the artifact from
that commit with :func:`asl_workbench.lifecycle.pack` -- the same function the
installer packs with. A digest that was copied from another release, or typed,
does not survive that. Both hosts' recorded release must be that digest too,
because a report that accepts one release while the Pi runs another is the
exact failure this gate exists for.

**Stale.** Staleness is measured against the package's own history rather than
against the calendar: evidence is stale when any commit between the release it
names and ``HEAD`` touched ``asl_workbench/``. A clock-based window would have
to be chosen, would drift, and would call a correct report invalid on a slow
week; "the thing you deployed is no longer the thing in the tree" is the
property that actually matters and it maintains itself. The release must also
be an ancestor of ``HEAD`` (a report from a branch nobody merged accepts
nothing), and the report may not predate the commit it accepts or be dated in
the future.

**Synthetic.** Every host block declares ``kind: "actual-host"`` and carries a
hostname and an operating system that match the machine it claims to be: the
Mac is ``Darwin``, the Pi is ``Linux``, and the two identities must differ.
Hostnames that name a fixture rather than a machine (``localhost``, ``test``,
``fake``, ``synthetic``, and the rest of :data:`FORBIDDEN_HOST_MARKERS`) are
refused. This is what the plan means by "synthetic unit-test results cannot
satisfy these evidence gates": a suite running on one laptop cannot produce a
document that names two distinct real machines and an artifact rebuilt from a
commit, so the only way to pass is to have deployed.

**Unsanitized.** The reports live in a repository that is pushed, so the reader
is also a privacy gate. Keys are allowlisted level by level -- an unrecognized
key is a refusal, not an ignored field -- and every string in the document is
bounded and may not contain a path separator, an ``@``, or a newline. There is
no field in the schema that needs any of them, which is what makes the rule
cheap to hold: a plan path, an operator's identity, or a pasted log cannot be
spelled in a document this reader accepts.

And what each stage must additionally establish, taken from the three sessions'
own completion contracts rather than invented here: staging carries the
60-minute active/idle/reconnect soak and its cleanup, production carries the
operator's explicit phone readiness *followed by* their confirmation for this
release, and retirement carries the one-frontend/one-owner audit. An
unanswered confirmation is a ``pending`` verdict, which is a refusal -- that is
the plan's "an unanswered request leaves acceptance pending", spelled as data.

``python3 -m tests.workbench_acceptance --template staging`` prints the
skeleton of a report, which is how a deployment session finds out what it has
to record without reading this file.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from asl_workbench import lifecycle

#: The ``scripts`` checkout this module is part of.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The document version this reader accepts. A future schema change is a new
#: number and a new reader, not a field this one quietly ignores.
SCHEMA_VERSION = 1

STAGE_STAGING = "staging"
STAGE_PRODUCTION = "production"
STAGE_RETIREMENT = "retirement"
STAGES: tuple[str, ...] = (STAGE_STAGING, STAGE_PRODUCTION, STAGE_RETIREMENT)

#: Where the reports sit inside the Pi repository.
EVIDENCE_RELATIVE_PATH = (
    Path("services") / "pi-monitoring" / "docs" / "workbench" / "evidence"
)

#: Overrides, for a checkout laid out some other way and for accepting a
#: release that is not ``HEAD``. Both exist so this reader can be exercised;
#: neither weakens a rule, because the evidence still has to survive every
#: check against whatever they point at.
ENV_EVIDENCE_DIR = "ASL_WORKBENCH_EVIDENCE_DIR"
ENV_RELEASE_REVISION = "ASL_WORKBENCH_RELEASE_REVISION"
ENV_PI_CHECKOUT = "ASL_WORKBENCH_PI_CHECKOUT"
PI_REPOSITORY_RELATIVE_PATH = Path("1_dev_projects") / "sglavoie_raspberry_pi"

#: Bounds. A report is a few kilobytes of identifiers and timestamps; anything
#: larger is carrying something it should not be.
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_STRING_LENGTH = 200
MAX_SEQUENCE_LENGTH = 64

#: How far ahead of this machine's clock a timestamp may sit before it is read
#: as wrong rather than as skew between two hosts.
MAX_CLOCK_SKEW_SECONDS = 300.0

#: The soak the staging session owes: a full hour, in three named phases.
SOAK_MINIMUM_SECONDS = 3600.0
SOAK_PHASES: tuple[str, ...] = ("active", "idle", "reconnect")

_REVISION = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_HOSTNAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63}){0,4}")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

#: Substrings that name a fixture rather than a machine. A hostname carrying
#: one of these is how a report produced by a test would look, so it is how a
#: report produced by a test is refused.
FORBIDDEN_HOST_MARKERS: tuple[str, ...] = (
    "localhost",
    "example",
    "test",
    "fake",
    "synthetic",
    "simulated",
    "unittest",
    "dummy",
    "sample",
    "stub",
    "mock",
    "fixture",
    "placeholder",
    "invalid",
)

#: The two machines every stage is deployed across, and what each one is.
HOST_ROLES: Mapping[str, str] = {"mac": "Darwin", "pi": "Linux"}

OUTCOME_PASSED = "passed"
VERDICT_ACCEPTED = "accepted"
PROVENANCE_ACTUAL_HOST = "actual-host"

#: What each stage has to have checked, one entry per bullet of that session's
#: own "Done when". Spelled as data so a session can see what it owes, and so a
#: report that simply omits an inconvenient one is a refusal rather than a pass
#: with a smaller list.
STAGE_CHECKS: Mapping[str, tuple[str, ...]] = {
    STAGE_STAGING: (
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
    ),
    STAGE_PRODUCTION: (
        "fresh-backups",
        "installed-versions-recorded",
        "old-connector-stopped",
        "operations-reconciled",
        "migration",
        "unified-deployment-active",
        "workers-preserved",
        "private-url-preserved",
        "catalog-continuity",
        "history-continuity",
        "old-links",
        "authenticated-reads",
        "integrations",
        "single-publisher",
        "isolated-acceptance-fixtures",
        "rollback-available",
    ),
    STAGE_RETIREMENT: (
        "companion-application-removed",
        "independent-dashboard-removed",
        "transferred-tests-removed",
        "bridge-plumbing-removed",
        "duplicate-definitions-removed",
        "deliberate-adapters-only",
        "development-dependencies-pruned",
        "historical-evidence-retained",
        "ownership-documented",
        "pi-monitoring-tests",
        "repository-sync",
        "one-frontend",
        "one-owner-per-operation",
        "deployed-application-verified",
    ),
}

#: What the operator is asked to confirm from the phone itself, in session 23.
PHONE_CHECKS: tuple[str, ...] = (
    "locked-phone-notification",
    "deep-link",
    "live-reply",
    "reply-with-resume",
)

#: Which extra block each stage carries beyond the common ones.
STAGE_EXTRA_KEY: Mapping[str, str | None] = {
    STAGE_STAGING: "soak",
    STAGE_PRODUCTION: "phone",
    STAGE_RETIREMENT: None,
}


class EvidenceError(AssertionError):
    """A report was refused. The message is what the operator has to fix.

    An ``AssertionError`` because every caller here is a test assertion and a
    refusal is a failure rather than an error: there is nothing broken, the
    evidence simply does not establish what it claims.
    """


# ---------------------------------------------------------------------------
# Locating the evidence


def locate_pi_repository(
    scripts_root: Path = REPO_ROOT,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Locate the registered Pi repository without depending on retired source."""
    env = os.environ if environ is None else environ
    candidates = []
    if override := env.get(ENV_PI_CHECKOUT):
        candidates.append(Path(override))
    candidates.append(Path(scripts_root).resolve().parent / PI_REPOSITORY_RELATIVE_PATH)
    for candidate in candidates:
        if (candidate / EVIDENCE_RELATIVE_PATH).is_dir():
            return candidate
    return None


def evidence_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Where the three reports live, or raise saying why nothing can be read."""
    env = os.environ if environ is None else environ
    override = env.get(ENV_EVIDENCE_DIR)
    if override:
        return Path(override)
    repository = locate_pi_repository(environ=env)
    if repository is None:
        raise EvidenceError(
            "no Pi checkout is reachable, so deployment evidence cannot be read; "
            f"set {ENV_EVIDENCE_DIR} or {ENV_PI_CHECKOUT}"
        )
    return repository / EVIDENCE_RELATIVE_PATH


def evidence_path(stage: str, environ: Mapping[str, str] | None = None) -> Path:
    """The report for one stage, by the name its deployment session writes."""
    if stage not in STAGES:
        raise EvidenceError(f"unknown acceptance stage: {stage}")
    return evidence_directory(environ) / f"{stage}.json"


def read_document(path: Path) -> dict[str, Any]:
    """Read one report, refusing everything that is not a bounded JSON object."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise EvidenceError(
            f"no deployment evidence at {path.name}: the stage it accepts has "
            "not been recorded"
        ) from error
    if size > MAX_DOCUMENT_BYTES:
        raise EvidenceError(f"{path.name} exceeds {MAX_DOCUMENT_BYTES} bytes")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{path.name} is not readable as JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise EvidenceError(f"{path.name} is not a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Sanitization
#
# The allowlist is applied before anything is interpreted, because a reader
# that validated the fields it knows and ignored the rest would publish
# whatever was put beside them.


def _fold(where: str, key: str) -> str:
    return key if not where else f"{where}.{key}"


def check_sanitized(document: Mapping[str, Any], where: str = "") -> None:
    """Refuse anything a sanitized report has no reason to carry.

    Structural rather than textual: strings are bounded and may hold no path
    separator, no ``@`` and no newline, numbers must be finite and not
    negative, and nothing may nest deeper or wider than the schema needs.
    """
    for key, value in document.items():
        _check_sanitized_value(_fold(where, str(key)), key, value)


def _check_sanitized_value(where: str, key: Any, value: Any) -> None:
    if not isinstance(key, str) or not key or len(key) > MAX_STRING_LENGTH:
        raise EvidenceError(f"{where}: unusable key")
    if isinstance(value, bool):
        return
    if isinstance(value, str):
        _check_sanitized_string(where, value)
        return
    if isinstance(value, int | float):
        if not math.isfinite(value) or value < 0:
            raise EvidenceError(f"{where}: {value!r} is not a usable number")
        return
    if isinstance(value, dict):
        check_sanitized(value, where)
        return
    if isinstance(value, list):
        if len(value) > MAX_SEQUENCE_LENGTH:
            raise EvidenceError(f"{where}: more than {MAX_SEQUENCE_LENGTH} entries")
        for index, item in enumerate(value):
            _check_sanitized_value(f"{where}[{index}]", f"{index}", item)
        return
    raise EvidenceError(f"{where}: {type(value).__name__} is not allowed in a report")


def _check_sanitized_string(where: str, value: str) -> None:
    if len(value) > MAX_STRING_LENGTH:
        raise EvidenceError(f"{where}: longer than {MAX_STRING_LENGTH} characters")
    for forbidden in ("/", "\\", "@", "\n", "\r", "\t"):
        if forbidden in value:
            raise EvidenceError(
                f"{where}: a sanitized report carries no {forbidden!r}; "
                "paths, identities and pasted output do not belong in it"
            )
    if not value.isprintable():
        raise EvidenceError(f"{where}: unprintable characters")


def require_keys(
    document: Mapping[str, Any], expected: Sequence[str], where: str
) -> None:
    """Exactly these keys: a missing one is a gap and an extra one is a leak."""
    present = set(document)
    wanted = set(expected)
    missing = sorted(wanted - present)
    if missing:
        raise EvidenceError(f"{where}: missing {', '.join(missing)}")
    extra = sorted(present - wanted)
    if extra:
        raise EvidenceError(f"{where}: unrecognized {', '.join(extra)}")


# ---------------------------------------------------------------------------
# Typed readers, each refusing rather than coercing


def _string(document: Mapping[str, Any], key: str, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{_fold(where, key)}: a non-empty string is required")
    return value


def _matching(
    document: Mapping[str, Any], key: str, pattern: re.Pattern[str], where: str
) -> str:
    value = _string(document, key, where)
    if not pattern.fullmatch(value):
        raise EvidenceError(
            f"{_fold(where, key)}: {value!r} is not in the expected form"
        )
    return value


def _timestamp(document: Mapping[str, Any], key: str, where: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvidenceError(f"{_fold(where, key)}: a unix timestamp is required")
    if not math.isfinite(value) or value <= 0:
        raise EvidenceError(f"{_fold(where, key)}: {value!r} is not a unix timestamp")
    return float(value)


def _mapping(document: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise EvidenceError(f"{_fold(where, key)}: an object is required")
    return value


# ---------------------------------------------------------------------------
# Release identity


def git(repository: Path, *arguments: str) -> str:
    """One read-only git question, answered or refused."""
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:  # pragma: no cover - git absent is not a rule
        raise EvidenceError(f"git is unavailable: {error}") from error
    if completed.returncode != 0:
        raise EvidenceError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def accepted_revision(
    repository: Path, environ: Mapping[str, str] | None = None
) -> str:
    """The release under acceptance: ``HEAD``, unless a caller named another."""
    env = os.environ if environ is None else environ
    named = env.get(ENV_RELEASE_REVISION)
    if named:
        if not _REVISION.fullmatch(named):
            raise EvidenceError(f"{ENV_RELEASE_REVISION} is not a full commit revision")
        return named
    return git(repository, "rev-parse", "HEAD")


def rebuild_digest(repository: Path, revision: str) -> str:
    """Pack the named commit the way the installer would, and return its digest.

    This is the whole of the wrong-release check. A digest can be copied from
    another report or typed; bytes rebuilt from a commit cannot be, and the
    packing function is the one an install actually accepts an artifact
    through, so a digest that matches here is a digest that would install.
    """
    with tempfile.TemporaryDirectory(prefix="wb-acceptance-") as raw:
        artifact = Path(raw) / "release.zip"
        try:
            return lifecycle.pack(artifact, revision, repository=repository)
        except lifecycle.LifecycleError as error:
            raise EvidenceError(
                f"the release named by the evidence cannot be rebuilt: {error}"
            ) from error


def check_release_is_ancestor(repository: Path, revision: str, accepted: str) -> None:
    """Require a real commit on the accepted release's history."""
    try:
        git(repository, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    except EvidenceError as error:
        raise EvidenceError(
            f"the evidence accepts {revision[:12]}, which is not a commit in this "
            "repository"
        ) from error
    if revision != accepted:
        merge_base = git(repository, "merge-base", revision, accepted)
        if merge_base != revision:
            raise EvidenceError(
                f"the evidence accepts {revision[:12]}, which is not an ancestor of "
                f"the release under acceptance ({accepted[:12]})"
            )


def check_release_is_current(repository: Path, revision: str, accepted: str) -> None:
    """Refuse a release that is not the one this repository would deploy now.

    Three questions, and they fail differently on purpose. A revision that is
    not a commit here names a release nobody can produce. A revision that is not
    an ancestor of the accepted one is evidence about something else entirely.
    A revision that *is* an ancestor but has package commits after it is
    evidence about a release the tree has since moved past -- stale, which is
    the failure the calendar would have been used to detect and detects worse.

    Existence is asked first so a typed or invented revision is reported as
    what it is, rather than as whatever the ancestry question says about a
    commit that is not there.
    """
    check_release_is_ancestor(repository, revision, accepted)
    changed = git(
        repository,
        "rev-list",
        "--count",
        f"{revision}..{accepted}",
        "--",
        lifecycle.PACKAGE_DIRECTORY,
    )
    if changed != "0":
        raise EvidenceError(
            f"the evidence is stale: {changed} commit(s) changed "
            f"{lifecycle.PACKAGE_DIRECTORY}/ after {revision[:12]}, so what was "
            "deployed is no longer what this repository holds"
        )


def commit_timestamp(repository: Path, revision: str) -> float:
    """When the accepted commit was made, which is the floor for every fact."""
    return float(git(repository, "show", "-s", "--format=%ct", revision))


# ---------------------------------------------------------------------------
# The stage-independent half of a report


def check_provenance(host: Mapping[str, Any], role: str, where: str) -> str:
    """One host block: a real machine, of the right kind, named once."""
    require_keys(host, ("kind", "host", "system", "release", "observed_at"), where)
    kind = _string(host, "kind", where)
    if kind != PROVENANCE_ACTUAL_HOST:
        raise EvidenceError(
            f"{_fold(where, 'kind')}: {kind!r} is not {PROVENANCE_ACTUAL_HOST!r}; "
            "acceptance is only ever recorded from the machine it happened on"
        )
    name = _matching(host, "host", _HOSTNAME, where)
    folded = name.lower()
    for marker in FORBIDDEN_HOST_MARKERS:
        if marker in folded:
            raise EvidenceError(
                f"{_fold(where, 'host')}: {name!r} names a fixture rather than a "
                "machine"
            )
    system = _string(host, "system", where)
    if system != HOST_ROLES[role]:
        raise EvidenceError(
            f"{_fold(where, 'system')}: the {role} runs {HOST_ROLES[role]}, "
            f"not {system}"
        )
    return name


def check_common(document: Mapping[str, Any], stage: str, repository: Path) -> None:
    """Everything the three stages answer alike: identity, release, hosts."""
    extra = STAGE_EXTRA_KEY[stage]
    expected = ["schema", "stage", "recorded_at", "release", "hosts", "checks"]
    if extra:
        expected.append(extra)
    require_keys(document, expected, stage)
    check_sanitized(document, stage)

    if document.get("schema") != SCHEMA_VERSION:
        raise EvidenceError(
            f"{stage}.schema: this reader accepts schema {SCHEMA_VERSION}"
        )
    if document.get("stage") != stage:
        raise EvidenceError(
            f"{stage}.stage: the report names {document.get('stage')!r}; a report "
            "read as one stage may not describe another"
        )

    release = _mapping(document, "release", stage)
    require_keys(release, ("revision", "artifact"), f"{stage}.release")
    revision = _matching(release, "revision", _REVISION, f"{stage}.release")
    artifact = _matching(release, "artifact", _DIGEST, f"{stage}.release")

    accepted = accepted_revision(repository)
    check_release_is_current(repository, revision, accepted)
    rebuilt = rebuild_digest(repository, revision)
    if rebuilt != artifact:
        raise EvidenceError(
            f"{stage}.release.artifact: {revision[:12]} packs to {rebuilt[:12]}, "
            f"not {artifact[:12]}; this evidence is about another release"
        )

    recorded_at = _timestamp(document, "recorded_at", stage)
    committed_at = commit_timestamp(repository, revision)
    if recorded_at < committed_at:
        raise EvidenceError(
            f"{stage}.recorded_at: the report predates the release it accepts"
        )
    if recorded_at > time.time() + MAX_CLOCK_SKEW_SECONDS:
        raise EvidenceError(f"{stage}.recorded_at: the report is dated in the future")

    hosts = _mapping(document, "hosts", stage)
    require_keys(hosts, tuple(HOST_ROLES), f"{stage}.hosts")
    names: dict[str, str] = {}
    for role in HOST_ROLES:
        where = f"{stage}.hosts.{role}"
        host = _mapping(hosts, role, f"{stage}.hosts")
        names[role] = check_provenance(host, role, where)
        running = _matching(host, "release", _DIGEST, where)
        if running != artifact:
            raise EvidenceError(
                f"{where}.release: this host ran {running[:12]}, not the release "
                f"being accepted ({artifact[:12]})"
            )
        observed_at = _timestamp(host, "observed_at", where)
        _check_window(observed_at, committed_at, recorded_at, f"{where}.observed_at")
    if names["mac"] == names["pi"]:
        raise EvidenceError(
            f"{stage}.hosts: both halves name {names['mac']!r}; a two-host "
            "deployment recorded from one machine is not a two-host deployment"
        )

    check_checks(document, stage, committed_at, recorded_at)


def _check_window(value: float, floor: float, ceiling: float, where: str) -> None:
    if value < floor:
        raise EvidenceError(f"{where}: predates the release it is evidence for")
    if value > ceiling + MAX_CLOCK_SKEW_SECONDS:
        raise EvidenceError(f"{where}: is later than the report that carries it")


def check_checks(
    document: Mapping[str, Any], stage: str, floor: float, ceiling: float
) -> None:
    """Every check that stage owes, each passed, each observed in the window."""
    checks = _mapping(document, "checks", stage)
    require_keys(checks, STAGE_CHECKS[stage], f"{stage}.checks")
    for name in STAGE_CHECKS[stage]:
        where = f"{stage}.checks.{name}"
        entry = _mapping(checks, name, f"{stage}.checks")
        require_keys(entry, ("outcome", "observed_at", "host"), where)
        outcome = _string(entry, "outcome", where)
        if outcome != OUTCOME_PASSED:
            raise EvidenceError(f"{where}: recorded as {outcome!r}")
        host = _string(entry, "host", where)
        if host not in (*HOST_ROLES, "both"):
            raise EvidenceError(f"{where}.host: {host!r} is not a deployed host")
        _check_window(
            _timestamp(entry, "observed_at", where),
            floor,
            ceiling,
            f"{where}.observed_at",
        )


# ---------------------------------------------------------------------------
# The stage-specific halves


def check_soak(document: Mapping[str, Any], floor: float, ceiling: float) -> None:
    """Staging's hour: long enough, in all three phases, inside the window."""
    where = f"{STAGE_STAGING}.soak"
    soak = _mapping(document, "soak", STAGE_STAGING)
    require_keys(soak, ("started_at", "ended_at", "phases"), where)
    started_at = _timestamp(soak, "started_at", where)
    ended_at = _timestamp(soak, "ended_at", where)
    _check_window(started_at, floor, ceiling, f"{where}.started_at")
    _check_window(ended_at, floor, ceiling, f"{where}.ended_at")
    if ended_at - started_at < SOAK_MINIMUM_SECONDS:
        raise EvidenceError(
            f"{where}: {ended_at - started_at:.0f}s is short of the "
            f"{SOAK_MINIMUM_SECONDS:.0f}s soak the stage owes"
        )
    phases = _mapping(soak, "phases", where)
    require_keys(phases, SOAK_PHASES, f"{where}.phases")
    for phase in SOAK_PHASES:
        seconds = phases.get(phase)
        if isinstance(seconds, bool) or not isinstance(seconds, int | float):
            raise EvidenceError(f"{where}.phases.{phase}: seconds are required")
        if seconds <= 0:
            raise EvidenceError(f"{where}.phases.{phase}: never happened")
    total = sum(float(phases[phase]) for phase in SOAK_PHASES)
    if total > (ended_at - started_at) + MAX_CLOCK_SKEW_SECONDS:
        raise EvidenceError(
            f"{where}.phases: the phases add up to more than the soak lasted"
        )


def check_phone(
    document: Mapping[str, Any],
    artifact: str,
    hosts: Mapping[str, str],
    floor: float,
    ceiling: float,
) -> None:
    """Production's human half: readiness first, then a confirmed answer.

    The order is the rule that matters. A confirmation that predates the
    readiness it was supposedly granted by is a confirmation of something the
    operator was never asked about, and an unanswered request is a ``pending``
    verdict rather than a missing field -- the plan asks for acceptance to stay
    pending in that case, and a refusal here is what keeps it pending.
    """
    where = f"{STAGE_PRODUCTION}.phone"
    phone = _mapping(document, "phone", STAGE_PRODUCTION)
    require_keys(phone, ("readiness", "confirmation"), where)

    readiness = _mapping(phone, "readiness", where)
    ready_where = f"{where}.readiness"
    require_keys(readiness, ("requested_at", "granted_at", "by"), ready_where)
    requested = _timestamp(readiness, "requested_at", ready_where)
    granted = _timestamp(readiness, "granted_at", ready_where)
    _check_window(requested, floor, ceiling, f"{ready_where}.requested_at")
    _check_window(granted, floor, ceiling, f"{ready_where}.granted_at")
    if granted < requested:
        raise EvidenceError(f"{ready_where}: granted before it was asked for")
    _matching(readiness, "by", _NAME, ready_where)

    confirmation = _mapping(phone, "confirmation", where)
    confirm_where = f"{where}.confirmation"
    require_keys(
        confirmation,
        (
            "requested_at",
            "answered_at",
            "verdict",
            "release",
            "kind",
            "host",
            "by",
            "checks",
        ),
        confirm_where,
    )
    kind = _string(confirmation, "kind", confirm_where)
    if kind != PROVENANCE_ACTUAL_HOST:
        raise EvidenceError(
            f"{confirm_where}.kind: {kind!r} is not {PROVENANCE_ACTUAL_HOST!r}; a "
            "phone acceptance is only ever recorded from the phone"
        )
    device = _matching(confirmation, "host", _HOSTNAME, confirm_where)
    folded = device.lower()
    for marker in FORBIDDEN_HOST_MARKERS:
        if marker in folded:
            raise EvidenceError(
                f"{confirm_where}.host: {device!r} names a fixture rather than a phone"
            )
    if device in hosts.values():
        raise EvidenceError(
            f"{confirm_where}.host: {device!r} is a deployed host; a phone "
            "acceptance confirmed from the Mac or the Pi is not a phone acceptance"
        )
    _matching(confirmation, "by", _NAME, confirm_where)

    verdict = _string(confirmation, "verdict", confirm_where)
    if verdict != VERDICT_ACCEPTED:
        raise EvidenceError(
            f"{confirm_where}.verdict: {verdict!r}; acceptance stays pending until "
            "the operator answers"
        )
    release = _matching(confirmation, "release", _DIGEST, confirm_where)
    if release != artifact:
        raise EvidenceError(
            f"{confirm_where}.release: the operator confirmed {release[:12]}, not "
            f"the release being accepted ({artifact[:12]})"
        )

    asked = _timestamp(confirmation, "requested_at", confirm_where)
    answered = _timestamp(confirmation, "answered_at", confirm_where)
    _check_window(asked, floor, ceiling, f"{confirm_where}.requested_at")
    _check_window(answered, floor, ceiling, f"{confirm_where}.answered_at")
    if asked < granted:
        raise EvidenceError(
            f"{confirm_where}.requested_at: the confirmation was asked for before "
            "the operator declared the phone ready"
        )
    if answered < asked:
        raise EvidenceError(
            f"{confirm_where}.answered_at: answered before it was asked"
        )

    checks = _mapping(confirmation, "checks", confirm_where)
    require_keys(checks, PHONE_CHECKS, f"{confirm_where}.checks")
    for name in PHONE_CHECKS:
        if checks.get(name) is not True:
            raise EvidenceError(f"{confirm_where}.checks.{name}: not confirmed")


def check_retirement_release(
    document: Mapping[str, Any], report_path: Path, repository: Path
) -> None:
    """Bind retirement to the deployment and phone evidence production proved."""
    production = read_document(report_path.with_name(f"{STAGE_PRODUCTION}.json"))
    require_keys(
        production,
        ("schema", "stage", "recorded_at", "release", "hosts", "checks", "phone"),
        STAGE_PRODUCTION,
    )
    check_sanitized(production, STAGE_PRODUCTION)
    if production.get("schema") != SCHEMA_VERSION:
        raise EvidenceError(
            "production.schema: retirement requires the accepted production schema"
        )
    if production.get("stage") != STAGE_PRODUCTION:
        raise EvidenceError(
            "production.stage: retirement requires the accepted production report"
        )
    retirement_release = _mapping(document, "release", STAGE_RETIREMENT)
    production_release = _mapping(production, "release", STAGE_PRODUCTION)
    require_keys(production_release, ("revision", "artifact"), "production.release")
    for field in ("revision", "artifact"):
        if retirement_release.get(field) != production_release.get(field):
            raise EvidenceError(
                f"retirement.release.{field}: does not match the accepted "
                "production deployment"
            )
    revision = _matching(
        production_release, "revision", _REVISION, "production.release"
    )
    artifact = _matching(production_release, "artifact", _DIGEST, "production.release")
    floor = commit_timestamp(repository, revision)
    ceiling = _timestamp(production, "recorded_at", STAGE_PRODUCTION)
    hosts = _mapping(production, "hosts", STAGE_PRODUCTION)
    require_keys(hosts, tuple(HOST_ROLES), "production.hosts")
    names: dict[str, str] = {}
    for role in HOST_ROLES:
        where = f"production.hosts.{role}"
        host = _mapping(hosts, role, "production.hosts")
        names[role] = check_provenance(host, role, where)
        running = _matching(host, "release", _DIGEST, where)
        if running != artifact:
            raise EvidenceError(
                f"{where}.release: does not match the accepted production deployment"
            )
        _check_window(
            _timestamp(host, "observed_at", where),
            floor,
            ceiling,
            f"{where}.observed_at",
        )
    if names["mac"] == names["pi"]:
        raise EvidenceError("production.hosts: one machine cannot prove two hosts")
    check_checks(production, STAGE_PRODUCTION, floor, ceiling)
    check_phone(production, artifact, names, floor, ceiling)


# ---------------------------------------------------------------------------
# The whole of one stage


def verify(
    stage: str, *, repository: Path | None = None, path: Path | None = None
) -> dict[str, Any]:
    """Read and accept one stage's evidence, or raise saying what is wrong."""
    root = repository or REPO_ROOT
    report_path = path or evidence_path(stage)
    document = read_document(report_path)
    check_common(document, stage, root)
    release = _mapping(document, "release", stage)
    revision = str(release["revision"])
    artifact = str(release["artifact"])
    floor = commit_timestamp(root, revision)
    ceiling = _timestamp(document, "recorded_at", stage)
    if stage == STAGE_STAGING:
        check_soak(document, floor, ceiling)
    elif stage == STAGE_PRODUCTION:
        hosts = _mapping(document, "hosts", stage)
        names = {role: str(_mapping(hosts, role, stage)["host"]) for role in HOST_ROLES}
        check_phone(document, artifact, names, floor, ceiling)
    elif stage == STAGE_RETIREMENT:
        check_retirement_release(document, report_path, root)
    return document


# ---------------------------------------------------------------------------
# The entry points the deployment contracts name


class _EvidenceCase:
    """Shared body of the three gates. Not a test case on its own.

    One assertion per rejection class rather than one that reads everything,
    because the failure a deployment session gets back is the only instruction
    it has, and "missing production.checks.single-publisher" is a different
    instruction from "this evidence is stale".
    """

    stage = ""

    def _path(self) -> Path:
        return evidence_path(self.stage)

    def _document(self) -> dict[str, Any]:
        return read_document(self._path())

    def test_the_report_exists_and_is_a_sanitized_object(self) -> None:
        document = self._document()
        check_sanitized(document, self.stage)

    def test_the_report_accepts_the_release_this_repository_deploys(self) -> None:
        document = self._document()
        release = _mapping(document, "release", self.stage)
        revision = _matching(release, "revision", _REVISION, f"{self.stage}.release")
        artifact = _matching(release, "artifact", _DIGEST, f"{self.stage}.release")
        accepted = accepted_revision(REPO_ROOT)
        check_release_is_current(REPO_ROOT, revision, accepted)
        if self.stage == STAGE_RETIREMENT:
            check_retirement_release(document, self._path(), REPO_ROOT)
        rebuilt = rebuild_digest(REPO_ROOT, revision)
        if rebuilt != artifact:
            raise EvidenceError(
                f"{self.stage}.release.artifact: {revision[:12]} packs to "
                f"{rebuilt[:12]}, not {artifact[:12]}"
            )

    def test_the_report_was_recorded_on_the_actual_hosts(self) -> None:
        document = self._document()
        hosts = _mapping(document, "hosts", self.stage)
        require_keys(hosts, tuple(HOST_ROLES), f"{self.stage}.hosts")
        for role in HOST_ROLES:
            check_provenance(
                _mapping(hosts, role, f"{self.stage}.hosts"),
                role,
                f"{self.stage}.hosts.{role}",
            )

    def test_every_check_this_stage_owes_passed(self) -> None:
        verify(self.stage)


class StagingEvidence(_EvidenceCase, unittest.TestCase):
    """Session 22's gate: the staging rehearsal on the two real hosts."""

    stage = STAGE_STAGING

    def test_the_soak_ran_for_a_full_hour_in_all_three_phases(self) -> None:
        document = self._document()
        release = _mapping(document, "release", self.stage)
        floor = commit_timestamp(REPO_ROOT, str(release["revision"]))
        check_soak(document, floor, _timestamp(document, "recorded_at", self.stage))


class ProductionEvidence(_EvidenceCase, unittest.TestCase):
    """Session 23's gate: the cutover, plus the operator's own confirmation."""

    stage = STAGE_PRODUCTION

    def test_the_operator_declared_the_phone_ready_and_then_confirmed(self) -> None:
        document = self._document()
        release = _mapping(document, "release", self.stage)
        hosts = _mapping(document, "hosts", self.stage)
        names = {
            role: str(_mapping(hosts, role, self.stage)["host"]) for role in HOST_ROLES
        }
        check_phone(
            document,
            str(release["artifact"]),
            names,
            commit_timestamp(REPO_ROOT, str(release["revision"])),
            _timestamp(document, "recorded_at", self.stage),
        )


class RetirementEvidence(_EvidenceCase, unittest.TestCase):
    """Session 24's gate: one frontend, one owner, one deployed application."""

    stage = STAGE_RETIREMENT


# ---------------------------------------------------------------------------
# The skeleton a deployment session fills in


def template(stage: str) -> dict[str, Any]:
    """A report with every field this reader requires, and no values.

    Printed rather than written: the session that deploys is the one that knows
    the hostnames, the digests and the times, and a file this module wrote
    would be a file this module then verified against itself.
    """
    if stage not in STAGES:
        raise EvidenceError(f"unknown acceptance stage: {stage}")
    document: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "stage": stage,
        "recorded_at": 0,
        "release": {"revision": "<40-hex commit>", "artifact": "<64-hex sha256>"},
        "hosts": {
            role: {
                "kind": PROVENANCE_ACTUAL_HOST,
                "host": f"<{role} hostname>",
                "system": system,
                "release": "<64-hex sha256>",
                "observed_at": 0,
            }
            for role, system in HOST_ROLES.items()
        },
        "checks": {
            name: {"outcome": OUTCOME_PASSED, "observed_at": 0, "host": "both"}
            for name in STAGE_CHECKS[stage]
        },
    }
    if stage == STAGE_STAGING:
        document["soak"] = {
            "started_at": 0,
            "ended_at": 0,
            "phases": dict.fromkeys(SOAK_PHASES, 0),
        }
    elif stage == STAGE_PRODUCTION:
        document["phone"] = {
            "readiness": {"requested_at": 0, "granted_at": 0, "by": "owner"},
            "confirmation": {
                "requested_at": 0,
                "answered_at": 0,
                "verdict": VERDICT_ACCEPTED,
                "release": "<64-hex sha256>",
                "kind": PROVENANCE_ACTUAL_HOST,
                "host": "<phone hostname>",
                "by": "owner",
                "checks": dict.fromkeys(PHONE_CHECKS, True),
            },
        }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m tests.workbench_acceptance",
        description="Read or template the deployment acceptance evidence.",
    )
    parser.add_argument("--template", choices=STAGES, help="print an empty report")
    parser.add_argument("--verify", choices=STAGES, help="read one stage's report")
    arguments = parser.parse_args(argv)
    if arguments.template:
        print(json.dumps(template(arguments.template), indent=2, sort_keys=True))
        return 0
    if arguments.verify:
        try:
            verify(arguments.verify)
        except EvidenceError as error:
            print(f"refused: {error}")
            return 1
        print(f"{arguments.verify}: accepted")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover - a convenience entry point
    sys.exit(main())
