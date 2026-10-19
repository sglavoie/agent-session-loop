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
somebody disabled. ``tests/run_suite.py`` deliberately does not list it for the
same reason; deployment contracts invoke the applicable gate explicitly once
its evidence exists. Its rules are proved instead by
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

**Stale.** Production and retirement evidence accept the release the repository
would deploy now, so they become stale when a later commit touching
``asl_workbench/`` lands. Staging is deliberately historical: its rehearsal
remains evidence about the release it actually exercised after later package
work lands. Every stage still requires the named release to be an ancestor of
``HEAD`` (a report from a branch nobody merged accepts nothing), rebuilds that
exact release to verify its artifact, and requires both hosts to name the same
artifact. Reports also may not predate the commit they accept or be dated in
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

__all__ = ["argparse", "json", "math", "os", "re", "subprocess", "sys", "tempfile", "time", "unittest", "Mapping", "Sequence", "Path", "Any", "lifecycle", "REPO_ROOT", "SCHEMA_VERSION", "STAGE_STAGING", "STAGE_PRODUCTION", "STAGE_RETIREMENT", "STAGES", "EVIDENCE_RELATIVE_PATH", "ENV_EVIDENCE_DIR", "ENV_RELEASE_REVISION", "ENV_PI_CHECKOUT", "PI_REPOSITORY_RELATIVE_PATH", "MAX_DOCUMENT_BYTES", "MAX_STRING_LENGTH", "MAX_SEQUENCE_LENGTH", "MAX_CLOCK_SKEW_SECONDS", "SOAK_MINIMUM_SECONDS", "SOAK_PHASES", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "FORBIDDEN_HOST_MARKERS", "HOST_ROLES", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "PROVENANCE_ACTUAL_HOST", "STAGE_CHECKS", "PHONE_CHECKS", "STAGE_EXTRA_KEY", "EvidenceError", "locate_pi_repository", "evidence_directory", "evidence_path", "read_document"]  # fmt: skip
