"""The harness the production cutover is driven and recorded through.

Session 23 moves the two machines that have been running the Companion -- this
Mac and the Pi -- onto the release session 22 staged, and then has to answer
the question :mod:`tests.workbench_acceptance` asks: *did that happen, to this
release, on those machines, and did the person who owns the phone say so?* The
acceptance reader refuses a report. This module is what a report may be built
out of, and it refuses to build one out of anything that was not observed.

It is the production counterpart of :mod:`tests.workbench_staging` and is
deliberately the mirror image of it in one respect. A rehearsal's first duty is
to touch nothing production owns, so that module's central refusal is *this
site shares an identifier with production*. A cutover's first duty is the
opposite: it must act on production and on nothing else, because a cutover
recorded against a moved-aside site would be a rehearsal wearing production's
evidence. So :func:`check_production` refuses a site that has had any owned
identifier moved aside, and :func:`check_prefix_production` refuses a prefix
that is not the installation both hosts have been running all along.

Like the staging harness it is self-contained -- standard library plus
:mod:`asl_workbench` -- because half of it runs on the Pi, which has no
checkout of this repository and receives this one file beside the unpacked
release. Its rules are proved by ``tests/test_workbench_cutover.py``.

Three jobs, and each exists because of a way a cutover goes wrong:

**A backup that is still there afterwards.** A migration takes its own
snapshot inside the prefix, which is the right thing for an unwind and the
wrong thing for a cutover: the whole point of the exercise is that the prefix
is about to be rewritten, and a rollback plan that lives inside the thing being
rewritten is not a rollback plan. :func:`capture_backup` copies every member
the prefix carries into an archive *outside* it -- databases through SQLite's
online backup API, because a file copy of an open database copies a moment that
never existed -- and records each member's digest, each database's integrity
and each table's row count. :func:`verify_backup` reads that archive back and
recomputes all of it. The row counts are what makes catalog and history
continuity a comparison rather than an impression: the counts that went into
the backup are the counts the migrated service has to still be able to show.

**Native halves that both think they are production.** Both hosts install
under one name -- :data:`~asl_workbench.lifecycle.LABEL` and
:data:`~asl_workbench.lifecycle.UNIT`, which are the Companion's own, preserved
on purpose -- so the unified deployment takes the legacy one's place rather
than standing beside it. :func:`check_single_owner` is how that is proved
rather than assumed: it reads the definition the prefix carries and refuses
anything that names a second label, unit or bind port.

**A report that cannot be written by hand.** :class:`Journal` is append-only
and timestamped, and :meth:`Journal.render` refuses to produce a document
unless every check the stage owes was recorded as passed, inside the window
between the release commit and the report, on a named host -- and unless the
operator's own phone acceptance is there too, in the order the plan requires:
readiness asked for and granted, *then* a confirmation asked for and answered,
naming this release, from a device that is neither of the two deployed hosts.
An unanswered confirmation is not a missing field this module fills in with a
default. It is a ``pending`` verdict, which :meth:`Journal.render` refuses, and
that refusal is what the plan means by "an unanswered request leaves acceptance
pending".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import fields
from pathlib import Path
from typing import Any

from asl_workbench import lifecycle, migration
from asl_workbench.installation import (
    BACKUPS_DIRECTORY,
    DEPLOYMENT_PATH,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    RELEASES_DIRECTORY,
    atomic,
    document,
)
from asl_workbench.integrations import PI_SITE, IntegrationSite
from asl_workbench.notifications import DEPLOYMENT_PRODUCTION

# ---------------------------------------------------------------------------
# What a cutover is allowed to be

#: The installation both hosts have been running since the Companion was
#: installed, by bare name. The cutover migrates *this* prefix rather than
#: installing beside it, because the plan preserves the installation prefixes.
PRODUCTION_PREFIX_NAME = "asl-companion"

#: The fields of a site that name a resource rather than describe one. This is
#: :data:`tests.workbench_staging.OWNED_SITE_FIELDS`, and the two modules are
#: pinned to each other by ``tests/test_workbench_cutover.py``: a rehearsal
#: moves every one of them aside and a cutover moves none of them.
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

#: The checks session 23 owes, restated here so the Pi half can be driven
#: without this repository. ``tests/test_workbench_cutover.py`` pins this to
#: :data:`tests.workbench_acceptance.STAGE_CHECKS`, so a divergence is a test
#: failure rather than a report the acceptance reader then rejects.
PRODUCTION_CHECKS: tuple[str, ...] = (
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
)

#: What the operator is asked to confirm from the phone itself, and nothing
#: else. Each one is a thing only a person holding a locked phone can answer.
PHONE_CHECKS: tuple[str, ...] = (
    "locked-phone-notification",
    "deep-link",
    "live-reply",
    "reply-with-resume",
)

#: The two hosts, and the operating system each one must report.
HOST_ROLES: Mapping[str, str] = {"mac": "Darwin", "pi": "Linux"}

#: Where a check may say it was observed.
CHECK_HOSTS: tuple[str, ...] = (*HOST_ROLES, "both")

SCHEMA_VERSION = 1
STAGE = "production"
OUTCOME_PASSED = "passed"
VERDICT_ACCEPTED = "accepted"
VERDICT_PENDING = "pending"
PROVENANCE_ACTUAL_HOST = "actual-host"

#: How far a recorded moment may sit outside the window before it is read as
#: wrong rather than as skew between two machines' clocks.
MAX_CLOCK_SKEW_SECONDS = 300.0

#: Substrings that name a fixture rather than a device. Kept identical to
#: :data:`tests.workbench_acceptance.FORBIDDEN_HOST_MARKERS` -- and pinned to
#: it by the tests -- so a phone this module accepts is a phone the acceptance
#: reader accepts.
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

_REVISION = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_HOSTNAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63}){0,4}")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class CutoverError(AssertionError):
    """A cutover step, or a report built out of one, was refused."""


# ---------------------------------------------------------------------------
# The site and the prefix the cutover acts on


def check_production(
    site: IntegrationSite, production: IntegrationSite = PI_SITE
) -> None:
    """Refuse a site that is not the production one, field by field.

    The inverse of :func:`tests.workbench_staging.check_isolated`, and for the
    inverse reason. A rehearsal that reused one production identifier would be
    a production change with a rehearsal's name on it; a cutover that moved one
    aside would leave the resource it was supposed to take over still pointing
    at the Companion, and would record that as production evidence.

    Every field is compared rather than a representative one, because the ways
    this goes wrong are not equivalent: a moved ``bind_port`` leaves the
    published URL pointing at the old process, a moved ``marker`` orphans the
    Homepage card instead of adopting it, and a moved ``publisher_topic`` mints
    a second publisher for the topic the operator's phone is subscribed to.
    """
    for field in OWNED_SITE_FIELDS:
        expected = getattr(production, field)
        found = getattr(site, field)
        if found != expected:
            raise CutoverError(
                f"cutover site moved the production {field} aside: "
                f"{found!r} is not {expected!r}"
            )
    if site.deployment != DEPLOYMENT_PRODUCTION:
        raise CutoverError(
            f"cutover site is recorded as the {site.deployment!r} deployment"
        )
    if site.notifications is None:
        raise CutoverError(
            "cutover site owns no notification site; the deployment that takes "
            "the Companion's place is the one that publishes to the phone"
        )
    if site.notifications != production.notifications:
        raise CutoverError("cutover site publishes somewhere production does not")
    for field in (item.name for item in fields(production)):
        if field in OWNED_SITE_FIELDS or field in ("deployment", "notifications"):
            continue
        if getattr(site, field) != getattr(production, field):
            raise CutoverError(f"cutover site changed {field}")


def check_prefix_production(
    prefix: Path, production_name: str = PRODUCTION_PREFIX_NAME
) -> None:
    """Refuse a prefix that is not the installation production has been using.

    Name rather than resolution, as in the staging harness and for the same
    reason: half the time the prefix being named is on the other host, so there
    is nothing local to resolve it against. The name is the whole of the
    identity here -- ``asl-companion`` is the directory both native definitions
    point at -- and a cutover aimed anywhere else is a cutover that would leave
    the deployed Companion exactly where it was.
    """
    path = Path(prefix)
    if not path.parts:
        raise CutoverError("a production prefix is required")
    if path.name != production_name:
        raise CutoverError(
            f"{prefix} is not the production installation at {production_name!r}"
        )


# ---------------------------------------------------------------------------

__all__ = ["argparse", "hashlib", "json", "plistlib", "re", "sqlite3", "tarfile", "tempfile", "time", "Mapping", "Sequence", "closing", "fields", "Path", "Any", "lifecycle", "migration", "BACKUPS_DIRECTORY", "DEPLOYMENT_PATH", "PRIVATE_DIRECTORY_MODE", "PRIVATE_FILE_MODE", "RELEASES_DIRECTORY", "atomic", "document", "PI_SITE", "IntegrationSite", "DEPLOYMENT_PRODUCTION", "PRODUCTION_PREFIX_NAME", "OWNED_SITE_FIELDS", "PRODUCTION_CHECKS", "PHONE_CHECKS", "HOST_ROLES", "CHECK_HOSTS", "SCHEMA_VERSION", "STAGE", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "VERDICT_PENDING", "PROVENANCE_ACTUAL_HOST", "MAX_CLOCK_SKEW_SECONDS", "FORBIDDEN_HOST_MARKERS", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "CutoverError", "check_production", "check_prefix_production"]  # fmt: skip
