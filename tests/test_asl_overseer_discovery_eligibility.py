"""Eligibility and incomplete-evidence rules of fleet discovery."""

from __future__ import annotations

import json
import socket
import unittest

from agent_session_loop import events, overseer_discovery, state
from tests.asl_overseer_discovery_support import (
    STARTED_AT,
    _Corpus,
    _repositories,
)

from .support import _WORKBENCH_DEAD_PID

ASL_TEST_PORTABLE = True


class EligibilityTests(unittest.TestCase):
    """Eligible means askable, and every refusal is nameable."""

    def test_a_published_record_is_eligible(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")

        candidate = corpus.only()

        self.assertTrue(candidate.eligible)
        self.assertEqual((), candidate.ineligible_reasons)
        self.assertEqual(str(plan_dir), candidate.plan_dir)
        self.assertEqual("20260619T130000Z-1", candidate.run_id)
        self.assertEqual(str(corpus.root), candidate.workspace_root)
        self.assertEqual("d" * 64, candidate.registry_digest)
        self.assertEqual(
            (
                overseer_discovery.RepositoryDeclaration(
                    alias="scripts",
                    path="scripts",
                    origin="github.com/sglavoie/scripts",
                    resolved_root=str(corpus.root / "scripts"),
                ),
            ),
            candidate.repositories,
        )
        self.assertEqual(STARTED_AT, candidate.run_started_at)

    def test_a_legacy_record_that_predates_the_contract_is_ineligible(self) -> None:
        """No published version means old, and old identity is not trustworthy."""
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-legacy")
        corpus.write_raw_record(
            plan_dir,
            json.dumps(
                {
                    "plan_dir": str(plan_dir),
                    "run_id": "20260101T000000Z-1",
                    "paused": False,
                    "run_started_at": STARTED_AT,
                }
            ),
        )

        candidate = corpus.only()

        self.assertFalse(candidate.eligible)
        self.assertIn("legacy_record", candidate.ineligible_reasons)
        self.assertIn("repositories_undeclared", candidate.ineligible_reasons)
        self.assertIn("registry_digest_missing", candidate.ineligible_reasons)
        self.assertTrue(candidate.record_observed)

    def test_a_record_from_a_newer_contract_is_refused_rather_than_guessed_at(
        self,
    ) -> None:
        corpus = _Corpus(self)
        corpus.write_run(
            "plan-future",
            **{
                overseer_discovery.DISCOVERY_VERSION_KEY: (
                    overseer_discovery.DISCOVERY_CONTRACT_VERSION + 1
                )
            },
        )

        candidate = corpus.only()

        self.assertFalse(candidate.eligible)
        self.assertEqual(("unsupported_contract",), candidate.ineligible_reasons)

    def test_a_record_naming_another_plan_is_ambiguous_identity(self) -> None:
        """The state directory name is the hash of the plan it belongs to."""
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        other = corpus.plan_dir("plan-beta")
        corpus.write_raw_record(
            plan_dir,
            json.dumps(corpus.published_record(plan_dir=str(other))),
        )

        candidate = corpus.only()

        self.assertFalse(candidate.eligible)
        self.assertEqual(("plan_identity_mismatch",), candidate.ineligible_reasons)

    def test_a_record_with_no_run_identity_is_ineligible(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", run_id="")

        candidate = corpus.only()

        self.assertEqual(("run_identity_missing",), candidate.ineligible_reasons)

    def test_a_run_that_declared_no_repositories_is_ineligible(self) -> None:
        """Every repository in a plan must be permitted, so none is not a grant."""
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", workspace_root=None, repositories=None)

        candidate = corpus.only()

        self.assertEqual(("repositories_undeclared",), candidate.ineligible_reasons)
        self.assertEqual((), candidate.repositories)

    def test_a_declaration_missing_its_resolved_root_refuses_the_whole_run(
        self,
    ) -> None:
        """Authorizing the readable subset would be the bypass, not the fix."""
        corpus = _Corpus(self)
        declarations = _repositories(corpus.root)
        declarations.append({"alias": "agentmux", "path": "1_dev/agentmux"})
        corpus.write_run("plan-alpha", repositories=declarations)

        candidate = corpus.only()

        self.assertEqual(
            ("repository_declaration_malformed",), candidate.ineligible_reasons
        )
        self.assertEqual(("scripts",), tuple(r.alias for r in candidate.repositories))

    def test_a_dry_run_is_not_a_fleet_member(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", dry_run=True)

        candidate = corpus.only()

        self.assertEqual(("dry_run",), candidate.ineligible_reasons)

    def test_every_named_reason_is_in_the_published_vocabulary(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-ok")
        corpus.write_run("plan-dry", dry_run=True)
        corpus.write_run("plan-nolabel", run_id="")

        for candidate in corpus.inventory().candidates:
            for reason in candidate.ineligible_reasons:
                with self.subTest(reason=reason):
                    self.assertIn(reason, overseer_discovery.INELIGIBILITY_REASONS)


class IncompleteEvidenceTests(unittest.TestCase):
    """A gap in observation is never read as a run that ended."""

    def test_a_malformed_record_is_unknown_rather_than_finished(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        corpus.write_raw_record(plan_dir, "{not json")

        inventory = corpus.inventory()
        candidate = inventory.candidates[0]

        self.assertEqual("unknown", candidate.lifecycle)
        self.assertFalse(candidate.record_observed)
        self.assertFalse(inventory.complete)
        self.assertEqual(("record_unobserved",), candidate.ineligible_reasons)
        self.assertEqual(1, len(inventory.warnings))
        self.assertIn("is malformed", inventory.warnings[0])

    def test_an_undecodable_record_or_lock_is_unobserved_not_a_crash(self) -> None:
        """Non-UTF-8 bytes and nesting deep enough to exhaust the decoder."""
        for raw in (b'{"run_id": "\xff"}', b"[" * 1_000_000):
            for file_name in (events.STATE_FILE_NAME, state.LOCK_FILE_NAME):
                with self.subTest(file=file_name, raw=raw[:8]):
                    corpus = _Corpus(self)
                    plan_dir = corpus.write_run("plan-alpha")
                    plan_state = state.plan_state_dir(plan_dir, corpus.state_root)
                    (plan_state / file_name).write_bytes(raw)

                    inventory = corpus.inventory()

                    self.assertEqual(1, len(inventory.candidates))
                    self.assertEqual(1, len(inventory.warnings))
                    self.assertIn("is malformed", inventory.warnings[0])
                    if file_name == events.STATE_FILE_NAME:
                        self.assertEqual(
                            ("record_unobserved",),
                            inventory.candidates[0].ineligible_reasons,
                        )

    def test_a_state_directory_with_no_record_yet_is_unknown(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        state.plan_state_dir(plan_dir, corpus.state_root).mkdir(parents=True)

        inventory = corpus.inventory()

        self.assertEqual("unknown", inventory.candidates[0].lifecycle)
        self.assertFalse(inventory.complete)
        # An absent record is an ordinary state of a directory a run has only
        # just created, so it is a gap without being news.
        self.assertEqual((), inventory.warnings)

    def test_a_json_document_that_is_not_an_object_is_unobserved(self) -> None:
        corpus = _Corpus(self)
        corpus.write_raw_record(corpus.plan_dir("plan-alpha"), "[]")

        inventory = corpus.inventory()

        self.assertFalse(inventory.candidates[0].record_observed)
        self.assertIn("is not a JSON object", inventory.warnings[0])

    def test_a_run_with_no_finish_and_no_live_owner_is_unproven(self) -> None:
        """A killed runner leaves exactly this record, and it is not a finish."""
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha", run_finished_at=None)
        corpus.write_lock(
            plan_dir, pid=_WORKBENCH_DEAD_PID, hostname=socket.gethostname()
        )

        candidate = corpus.only()

        self.assertEqual("unknown", candidate.lifecycle)
        self.assertTrue(candidate.eligible)

    def test_an_unreadable_state_root_yields_an_incomplete_empty_inventory(
        self,
    ) -> None:
        corpus = _Corpus(self)
        corpus.state_root.write_text("not a directory", encoding="utf-8")

        inventory = corpus.inventory()

        self.assertEqual((), inventory.candidates)
        self.assertFalse(inventory.complete)
        self.assertIn("state corpus is unreadable", inventory.warnings[0])

    def test_a_missing_state_root_scans_as_an_empty_but_complete_fleet(self) -> None:
        corpus = _Corpus(self)

        inventory = corpus.inventory()

        self.assertEqual((), inventory.candidates)
        self.assertTrue(inventory.complete)


if __name__ == "__main__":
    unittest.main()
