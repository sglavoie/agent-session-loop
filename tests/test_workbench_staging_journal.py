"""Prove a staging journal only renders a report of what was observed.

Split from ``tests/test_workbench_staging.py``; see it for the rationale.
"""

from __future__ import annotations

import time
import unittest
from typing import Any

from tests import workbench_acceptance as acceptance
from tests import workbench_staging as staging
from tests.workbench_staging_support import (
    ARTIFACT,
    COMMITTED_AT,
    MAC,
    PI,
    RECORDED_AT,
    SOAK_ENDED_AT,
    SOAK_STARTED_AT,
    TemporaryCase,
    complete_journal,
)


class JournalRefusals(TemporaryCase):
    """A report is what was observed, or it is not a report."""

    def journal(self, **overrides: Any) -> staging.Journal:
        return complete_journal(self.root / "journal.json", **overrides)

    def render(self, journal: staging.Journal) -> dict[str, Any]:
        return journal.render(committed_at=COMMITTED_AT, recorded_at=RECORDED_AT)

    def test_a_complete_rehearsal_renders(self) -> None:
        document = self.render(self.journal())
        self.assertEqual(document["stage"], acceptance.STAGE_STAGING)
        self.assertEqual(document["release"]["artifact"], ARTIFACT)

    def test_the_rendered_report_is_one_the_gate_accepts(self) -> None:
        # The gate's own structural rules, run against the harness's output.
        # Everything the gate checks against git -- the revision, the rebuilt
        # digest, the staleness rule -- needs a real repository and belongs to
        # tests/test_workbench_acceptance.py; everything else is proved here,
        # so a drift between the two modules fails before a rehearsal, not
        # after one.
        document = self.render(self.journal())
        acceptance.check_sanitized(document, acceptance.STAGE_STAGING)
        acceptance.require_keys(
            document,
            ("schema", "stage", "recorded_at", "release", "hosts", "checks", "soak"),
            acceptance.STAGE_STAGING,
        )
        for role in acceptance.HOST_ROLES:
            acceptance.check_provenance(
                document["hosts"][role], role, f"staging.hosts.{role}"
            )
        acceptance.check_checks(
            document, acceptance.STAGE_STAGING, COMMITTED_AT, RECORDED_AT
        )
        acceptance.check_soak(document, COMMITTED_AT, RECORDED_AT)

    def test_the_journal_survives_the_process_that_wrote_it(self) -> None:
        # The soak is an hour long and the rehearsal spans two hosts; a journal
        # that only existed in memory would lose its first half to any
        # interruption.
        self.journal()
        reopened = staging.Journal(self.root / "journal.json")
        self.assertEqual(reopened.missing(), [])
        self.render(reopened)

    def test_an_unrecorded_check_is_named_rather_than_assumed(self) -> None:
        journal = self.journal()
        del journal.data["checks"]["rollback"]
        journal.save()
        self.assertEqual(journal.missing(), ["rollback"])
        with self.assertRaises(staging.StagingError) as caught:
            self.render(journal)
        self.assertIn("rollback", str(caught.exception))

    def test_a_check_that_did_not_pass_cannot_be_rendered(self) -> None:
        journal = self.journal(recovery={"outcome": "failed"})
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_check_the_stage_does_not_owe_is_refused_at_the_point_of_writing(
        self,
    ) -> None:
        journal = staging.Journal(self.root / "journal.json")
        with self.assertRaises(staging.StagingError):
            journal.record("everything-worked", host="both")
        with self.assertRaises(staging.StagingError):
            journal.record("rollback", host="laptop")
        with self.assertRaises(staging.StagingError):
            journal.host("router", host="r", system="Linux", release=ARTIFACT)

    def test_a_rehearsal_without_a_release_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["release"] = None
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_host_that_ran_another_release_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.host(
            "pi",
            host=PI,
            system="Linux",
            release="c" * 64,
            observed_at=COMMITTED_AT + 60.0,
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_host_of_the_wrong_kind_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.host(
            "pi",
            host=PI,
            system="Darwin",
            release=ARTIFACT,
            observed_at=COMMITTED_AT + 60.0,
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_one_machine_cannot_be_both_halves(self) -> None:
        journal = self.journal()
        journal.host(
            "pi",
            host=MAC,
            system="Linux",
            release=ARTIFACT,
            observed_at=COMMITTED_AT + 60.0,
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_missing_host_cannot_be_rendered(self) -> None:
        journal = self.journal()
        del journal.data["hosts"]["mac"]
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_an_observation_outside_the_window_cannot_be_rendered(self) -> None:
        for observed_at in (COMMITTED_AT - 1.0, RECORDED_AT + 3600.0):
            journal = self.journal()
            journal.record("rollback", host="pi", observed_at=observed_at)
            with (
                self.subTest(observed_at=observed_at),
                self.assertRaises(staging.StagingError),
            ):
                self.render(journal)

    def test_an_unparseable_moment_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["checks"]["rollback"]["observed_at"] = "yesterday"
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_short_soak_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.soak(
            started_at=SOAK_STARTED_AT,
            ended_at=SOAK_ENDED_AT - 1.0,
            phases={"active": 1800.0, "idle": 1200.0, "reconnect": 599.0},
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_phase_that_never_happened_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.soak(
            started_at=SOAK_STARTED_AT,
            ended_at=SOAK_ENDED_AT,
            phases={"active": 3600.0, "idle": 0.0, "reconnect": 0.0},
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_phases_that_outlast_the_soak_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.soak(
            started_at=SOAK_STARTED_AT,
            ended_at=SOAK_ENDED_AT,
            phases={"active": 3600.0, "idle": 3600.0, "reconnect": 3600.0},
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_soak_missing_a_phase_is_refused_at_the_point_of_writing(self) -> None:
        journal = self.journal()
        with self.assertRaises(staging.StagingError):
            journal.soak(
                started_at=SOAK_STARTED_AT,
                ended_at=SOAK_ENDED_AT,
                phases={"active": 1800.0, "idle": 1800.0},
            )
        with self.assertRaises(staging.StagingError):
            journal.soak(
                started_at=SOAK_STARTED_AT,
                ended_at=SOAK_ENDED_AT,
                phases={
                    "active": 1800.0,
                    "idle": 1200.0,
                    "reconnect": 600.0,
                    "extra": 1.0,
                },
            )

    def test_a_rehearsal_without_a_soak_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["soak"] = None
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_the_report_is_dated_by_the_clock_rather_than_by_its_author(self) -> None:
        journal = self.journal()
        document = journal.render(committed_at=COMMITTED_AT)
        self.assertEqual(document["recorded_at"], RECORDED_AT)
        self.assertIs(staging.Journal(self.root / "fresh.json").clock, time.time)


if __name__ == "__main__":  # pragma: no cover - the discovered suite runs these
    unittest.main()
