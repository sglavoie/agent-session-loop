"""Pause-suffix, violation-summary and review-base tests for `workspace_evidence`.

Payload-only, portable tests; see `test_asl_workspace_evidence_boundary.py`.
"""

from __future__ import annotations

import unittest

from agent_session_loop import (
    runner,
    workspace_evidence,
)
from tests.asl_workspace_evidence_support import _workspace

ASL_TEST_PORTABLE = True


class WorkspaceUnavailableSummaryTests(unittest.TestCase):
    def test_a_repository_is_named_by_its_alias_and_failing_node(self) -> None:
        summary = workspace_evidence._workspace_unavailable_summary(
            {"svc": {"after": {"path": "src/main.py", "reason": "unreadable"}}},
            workspace=_workspace("SVC"),
        )

        self.assertEqual(": SVC:src/main.py (unreadable)", summary)

    def test_the_after_phase_outranks_the_before_phase(self) -> None:
        # Both phases can fail; the after-state is the one the guard is judging,
        # so it is the reason an operator is shown.
        summary = workspace_evidence._workspace_unavailable_summary(
            {
                "svc": {
                    "before": {"reason": "before-reason"},
                    "after": {"reason": "after-reason"},
                }
            },
            workspace=_workspace("SVC"),
        )

        self.assertEqual(": SVC (after-reason)", summary)

    def test_an_unregistered_repository_is_named_by_its_path(self) -> None:
        summary = workspace_evidence._workspace_unavailable_summary(
            {"vendor/other": {"reason": "repository-unavailable"}},
            workspace=_workspace("SVC"),
        )

        self.assertEqual(": vendor/other (repository-unavailable)", summary)

    def test_the_suffix_is_bounded_to_three_subjects_and_a_count(self) -> None:
        summary = workspace_evidence._workspace_unavailable_summary(
            {f"repo{index}": {"reason": "unreadable"} for index in range(5)},
            workspace=_workspace("SVC"),
        )

        self.assertEqual(3, summary.count("(unreadable)"))
        self.assertTrue(summary.endswith("(+2 more)"), summary)

    def test_nothing_unavailable_makes_no_suffix(self) -> None:
        self.assertEqual(
            "",
            workspace_evidence._workspace_unavailable_summary(
                {}, workspace=_workspace("SVC")
            ),
        )


class WorkspaceUnavailableMessageTests(unittest.TestCase):
    def test_external_authority_is_not_a_repository_capture_failure(self) -> None:
        message = workspace_evidence._workspace_unavailable_message(
            {"pi-gradle.py": {"reason": "external-write-unclassified"}},
            workspace=_workspace("SVC"),
        )
        self.assertIn("external paths have unresolved write authority", message)
        self.assertIn("pi-gradle.py (external-write-unclassified)", message)
        self.assertNotIn("repository", message)

    def test_repository_failures_still_name_the_capture_failure(self) -> None:
        message = workspace_evidence._workspace_unavailable_message(
            {"svc": {"after": {"reason": "unreadable"}}},
            workspace=_workspace("SVC"),
        )
        self.assertIn("repository evidence could not be captured safely", message)
        self.assertIn("SVC (unreadable)", message)
        self.assertNotIn("external paths", message)

    def test_mixed_failures_explain_both_surfaces(self) -> None:
        message = workspace_evidence._workspace_unavailable_message(
            {
                "svc": {"before": {"reason": "unreadable"}},
                "external.py": {"reason": "external-write-unclassified"},
            },
            workspace=_workspace("SVC"),
        )
        self.assertIn("external paths have unresolved write authority", message)
        self.assertIn("repository evidence could not be captured safely", message)


class LegacyWriteGuardFailureMessageTests(unittest.TestCase):
    # The unavailable row the legacy guard recorded in run
    # 20260925T235115074418Z-36414 (ASI-153).
    EXTERNAL_ROW = {
        "[outside-approved-roots:32c8ea14cfd9ac96]": {
            "after": {
                "kind": "file",
                "mtime_ns": 1790380682505749000,
                "present": True,
                "sha256": (
                    "a9b2e5e08c1dce6e64276ea5365fe804f633d1a9e165a72fb4e8f02ec705803f"
                ),
                "size": 26380,
            },
            "reason": "external-write-unclassified",
            "surface": "absolute-path",
        }
    }

    def test_an_external_write_is_not_a_repository_or_history_failure(self) -> None:
        message = workspace_evidence._legacy_write_guard_failure_message(
            {"unavailable": self.EXTERNAL_ROW, "violations": []}
        )
        self.assertEqual(
            "external paths have unresolved write authority: "
            "[outside-approved-roots:32c8ea14cfd9ac96] (external-write-unclassified)",
            message,
        )

    def test_a_repository_capture_failure_is_still_named(self) -> None:
        message = workspace_evidence._legacy_write_guard_failure_message(
            {"unavailable": {".": {"after": {"reason": "unreadable"}}}}
        )
        self.assertEqual(
            "repository evidence could not be captured safely: . (unreadable)",
            message,
        )

    def test_repository_set_violations_are_named_rather_than_called_blindness(
        self,
    ) -> None:
        message = workspace_evidence._legacy_write_guard_failure_message(
            {
                "unavailable": {},
                "violations": [
                    {"kind": "repository-added", "repository": "vendor/lib"}
                ],
            }
        )
        self.assertEqual(
            "repository set changed outside the legacy scope: "
            "vendor/lib (repository-added)",
            message,
        )
        self.assertNotIn("could not be captured", message)

    def test_evidence_naming_no_cause_keeps_the_generic_explanation(self) -> None:
        self.assertIn(
            "could not be captured safely",
            workspace_evidence._legacy_write_guard_failure_message({}),
        )


class WorkspaceGuardViolationSummaryTests(unittest.TestCase):
    def test_qualified_paths_are_preferred_over_the_repository_name(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {
                        "kind": "read-only-repository",
                        "alias": "OAK",
                        "repository": "oak",
                        "qualified_paths": ["OAK:src/a.py", "OAK:src/b.py"],
                    }
                ]
            }
        )

        self.assertEqual(": OAK:src/a.py, OAK:src/b.py", summary)

    def test_a_violation_with_no_paths_falls_back_to_its_alias(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {
                        "kind": "repository-added",
                        "alias": "OAK",
                        "repository": "oak",
                        "qualified_paths": [],
                    }
                ]
            }
        )

        self.assertEqual(": OAK", summary)

    def test_a_violation_with_neither_paths_nor_alias_names_the_repository(
        self,
    ) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {
                        "kind": "outside-read-scope",
                        "alias": None,
                        "repository": "vendor/other",
                        "qualified_paths": [],
                    }
                ]
            }
        )

        self.assertEqual(": vendor/other", summary)

    def test_the_same_path_named_by_two_violations_is_listed_once(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {"qualified_paths": ["OAK:src/a.py"]},
                    {"qualified_paths": ["OAK:src/a.py"]},
                ]
            }
        )

        self.assertEqual(": OAK:src/a.py", summary)

    def test_the_suffix_is_bounded_to_three_paths_and_a_count(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {"violations": [{"qualified_paths": [f"OAK:{i}.py" for i in range(5)]}]}
        )

        self.assertEqual(": OAK:0.py, OAK:1.py, OAK:2.py (+2 more)", summary)

    def test_a_passing_evidence_payload_makes_no_suffix(self) -> None:
        self.assertEqual(
            "",
            workspace_evidence._workspace_guard_violation_summary({"violations": []}),
        )


class WorkspaceReviewBaseTests(unittest.TestCase):
    def test_a_registered_repository_with_no_fingerprint_raises(self) -> None:
        # An empty base would silently exempt the repository from the review,
        # so the absence is an error rather than a projection.
        with self.assertRaises(runner.RunError):
            workspace_evidence._workspace_review_bases(_workspace("SVC"), {})

    def test_a_missing_registered_repository_is_reported_unavailable(self) -> None:
        unavailable = workspace_evidence._workspace_snapshot_unavailable(
            {}, required_repository_paths=("svc",)
        )

        self.assertEqual(
            {"svc": {"reason": "registered-repository-missing"}}, unavailable
        )


if __name__ == "__main__":
    unittest.main()
