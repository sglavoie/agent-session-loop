"""Shared fixtures for the ``test_workbench_staging*`` modules."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import workbench_staging as staging

MAC = "sglavoies-mac"
PI = "pi"
ARTIFACT = "a" * 64
REVISION = "b" * 40

#: A moment the rehearsal could plausibly have happened at, and the release
#: commit it would have been evidence for. Fixed rather than sampled: a test
#: that reads the clock proves a different thing every time it runs.
COMMITTED_AT = 1_700_000_000.0
SOAK_STARTED_AT = COMMITTED_AT + 600.0
SOAK_ENDED_AT = SOAK_STARTED_AT + staging.SOAK_MINIMUM_SECONDS
RECORDED_AT = SOAK_ENDED_AT + 600.0


def complete_journal(path: Path, **overrides: Any) -> staging.Journal:
    """A journal that recorded the whole rehearsal, for a test to then break."""
    journal = staging.Journal(path, clock=lambda: RECORDED_AT)
    journal.release(REVISION, ARTIFACT)
    journal.host(
        "mac",
        host=MAC,
        system="Darwin",
        release=ARTIFACT,
        observed_at=COMMITTED_AT + 60.0,
    )
    journal.host(
        "pi", host=PI, system="Linux", release=ARTIFACT, observed_at=COMMITTED_AT + 60.0
    )
    for index, name in enumerate(staging.STAGING_CHECKS):
        journal.record(
            name,
            host="both",
            observed_at=COMMITTED_AT + 120.0 + index,
            **overrides.get(name, {}),
        )
    journal.soak(
        started_at=SOAK_STARTED_AT,
        ended_at=SOAK_ENDED_AT,
        phases={"active": 1800.0, "idle": 1200.0, "reconnect": 600.0},
    )
    return journal


class TemporaryCase(unittest.TestCase):
    """A case with a directory of its own to write journals and units into."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
