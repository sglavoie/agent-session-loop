# mypy: ignore-errors
# ruff: noqa
"""Prove the cutover harness acts only on production and invents no acceptance.

``tests/workbench_cutover.py`` does its real work against the two machines that
run the Companion, and against a person holding a phone. Neither can happen
here. What can be proved here is everything that would make the cutover
dangerous or its evidence worthless: that it refuses to act on anything but the
production installation, that a backup it says is good really can be read back,
and that the report it renders is one it observed rather than one it was handed.

The shape follows ``tests/test_workbench_staging.py``, which does this for the
rehearsal on the other side of the same document, and the rules the two harness
modules share with the acceptance reader are *pinned* to it rather than
restated. The cutover carries its own copy of the stage's checks so the Pi half
can be driven without this repository, and a copy that drifted would build
reports the gate rejects -- after the production service had been migrated
rather than before it.

The phone tests are the ones worth being stubborn about. Every refusal in
:meth:`tests.workbench_cutover.Journal.render` around the phone exists so that
the only way to a passing production report is a person actually answering, and
a test that could be satisfied by a default would quietly remove that.
"""

from __future__ import annotations

import json
import plistlib
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from asl_workbench import lifecycle, lifecycle_attachment, migration
from asl_workbench.installation import (
    CONFIG_PATH,
    DATABASE_PATH,
    DEPLOYMENT_PATH,
    RECEIPTS_DIRECTORY,
    SECRETS_PATH,
    atomic,
)
from asl_workbench.integrations import PI_SITE
from tests import workbench_acceptance as acceptance
from tests import workbench_cutover as cutover
from tests import workbench_staging as staging

MAC = "sglavoies-mac"
PI = "pi"
PHONE = "pixel"
ARTIFACT = "a" * 64
REVISION = "b" * 40

#: A moment the cutover could plausibly have happened at, and the release commit
#: it is evidence for. Fixed rather than sampled, so a failure means the same
#: thing every time it appears.
COMMITTED_AT = 1_700_000_000.0
READINESS_ASKED_AT = COMMITTED_AT + 3600.0
READINESS_GRANTED_AT = READINESS_ASKED_AT + 300.0
CONFIRMATION_ASKED_AT = READINESS_GRANTED_AT + 120.0
CONFIRMATION_ANSWERED_AT = CONFIRMATION_ASKED_AT + 240.0
RECORDED_AT = CONFIRMATION_ANSWERED_AT + 300.0


def production_prefix(root: Path, *, artifact: str | None = ARTIFACT) -> Path:
    """A prefix shaped like the one the cutover backs up and migrates."""
    prefix = root / cutover.PRODUCTION_PREFIX_NAME
    for directory in ("config", "credentials", "state", RECEIPTS_DIRECTORY):
        (prefix / directory).mkdir(parents=True, exist_ok=True)
    atomic(prefix / CONFIG_PATH, json.dumps({"target_id": "pi"}).encode())
    atomic(prefix / SECRETS_PATH, json.dumps({"credentials": []}).encode())
    atomic(
        prefix / f"{RECEIPTS_DIRECTORY}/homepage.json",
        json.dumps({"component": "homepage"}).encode(),
    )
    if artifact is not None:
        atomic(prefix / DEPLOYMENT_PATH, json.dumps({"artifact": artifact}).encode())
    database = prefix / DATABASE_PATH
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE plans (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO plans VALUES (?)", [("one",), ("two",), ("three",)]
        )
        connection.execute("INSERT INTO runs VALUES ('run')")
    database.chmod(0o600)
    return prefix


def complete_journal(path: Path, **overrides: Any) -> cutover.Journal:
    """A journal that recorded the whole cutover, for a test to then break."""
    journal = cutover.Journal(path, clock=lambda: RECORDED_AT)
    journal.release(REVISION, ARTIFACT)
    journal.host(
        "mac",
        host=MAC,
        system="Darwin",
        release=ARTIFACT,
        observed_at=COMMITTED_AT + 60,
    )
    journal.host(
        "pi", host=PI, system="Linux", release=ARTIFACT, observed_at=COMMITTED_AT + 60
    )
    for index, name in enumerate(cutover.PRODUCTION_CHECKS):
        journal.record(
            name,
            host="both",
            observed_at=COMMITTED_AT + 120.0 + index,
            **overrides.get(name, {}),
        )
    journal.phone_readiness(
        requested_at=READINESS_ASKED_AT, granted_at=READINESS_GRANTED_AT
    )
    confirmation: dict[str, Any] = {
        "requested_at": CONFIRMATION_ASKED_AT,
        "answered_at": CONFIRMATION_ANSWERED_AT,
        "verdict": cutover.VERDICT_ACCEPTED,
        "release": ARTIFACT,
        "host": PHONE,
        "checks": dict.fromkeys(cutover.PHONE_CHECKS, True),
    }
    journal.phone_confirmation(**{**confirmation, **overrides.get("phone", {})})
    return journal


class TemporaryCase(unittest.TestCase):
    """A case with a directory of its own to write prefixes and journals into."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)


def _digest_of(path: Path) -> dict[str, Any]:
    digest, size = cutover._digest_file(path)  # noqa: SLF001 - rewriting a fixture
    return {"sha256": digest, "bytes": size}


__all__ = [name for name in globals() if not name.startswith("__")]
