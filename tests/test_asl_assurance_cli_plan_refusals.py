"""Test asl assurance cli plan refusals."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests import support_asl_assurance_cli as fixtures


class AssurancePlanTests(fixtures.AssurancePlanCase):
    def test_an_unknown_issue_is_refused_before_a_harness_runs(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv("ASI-99"))

        self.assertEqual(1, code)
        self.assertEqual("unknown-issue", payload["reason"])
        self.assertEqual([], calls)
        self.assertFalse(self.output.exists())

    def test_an_unusable_issue_id_is_refused_before_the_ledger_is_read(self) -> None:
        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv("not-an-id"))

        self.assertEqual(1, code)
        self.assertEqual("invalid-issue-id", payload["reason"])
        self.assertEqual([], calls)

    def test_an_existing_output_path_is_never_overwritten(self) -> None:
        self.open_three_issues()
        self.output.write_text("a proposal a human is reading\n", encoding="utf-8")

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("output-exists", payload["reason"])
        self.assertEqual(
            "a proposal a human is reading\n",
            self.output.read_text(encoding="utf-8"),
        )
        self.assertEqual([], calls)
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_an_output_path_with_no_directory_is_refused(self) -> None:
        self.open_three_issues()
        self.output = Path(self._tmp.name) / "nowhere" / "proposal.md"

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("output-unwritable", payload["reason"])
        self.assertEqual([], calls)

    def test_an_unknown_harness_is_refused(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            code, payload, _ = self.run_json(
                [
                    "assurance",
                    "plan",
                    "ASI-1",
                    "--harness",
                    "nope",
                    "--output",
                    str(self.output),
                ]
            )

        self.assertEqual(1, code)
        self.assertEqual("input-error", payload["reason"])
        self.assertEqual([], calls)

    def test_a_harness_that_cannot_run_read_only_is_refused(self) -> None:
        self.open_three_issues()

        with self.drafting(unavailable="harness-not-installed") as calls:
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("harness-unavailable", payload["reason"])
        self.assertIn("harness-not-installed", payload["error"])
        self.assertEqual([], calls)
        self.assertFalse(self.output.exists())

    def test_a_non_positive_timeout_is_refused_before_anything_runs(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv(timeout="0"))

        self.assertEqual(1, code)
        self.assertEqual("input-error", payload["reason"])
        self.assertEqual([], calls)

    def test_a_timed_out_turn_writes_nothing(self) -> None:
        self.open_three_issues()

        with self.drafting(timed_out=True, returncode=124):
            code, payload, _ = self.run_json(self.plan_argv(timeout="45"))

        self.assertEqual(1, code)
        self.assertEqual("timeout", payload["reason"])
        self.assertIn("45 seconds", payload["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_a_failed_turn_writes_nothing(self) -> None:
        self.open_three_issues()

        with self.drafting(returncode=3):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("harness-failed", payload["reason"])
        self.assertFalse(self.output.exists())

    def test_a_reply_missing_a_required_section_is_not_a_proposal(self) -> None:
        self.open_three_issues()
        sections = dict(fixtures.DRAFT_SECTIONS)
        del sections["Test Plan"]

        with self.drafting(last_message=fixtures._draft_document(sections)):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("malformed-output", payload["reason"])
        self.assertIn("Test Plan", payload["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_an_empty_reply_is_not_a_proposal(self) -> None:
        self.open_three_issues()

        with self.drafting(last_message="   \n"):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("malformed-output", payload["reason"])
        self.assertFalse(self.output.exists())

    def test_a_section_named_only_inside_a_code_fence_does_not_count(self) -> None:
        self.open_three_issues()
        sections = dict(fixtures.DRAFT_SECTIONS)
        sections["Proposed Changes"] = (
            "```markdown\n## Test Plan\nnot a real section\n```"
        )
        del sections["Test Plan"]

        with self.drafting(last_message=fixtures._draft_document(sections)):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("malformed-output", payload["reason"])
        self.assertIn("Test Plan", payload["error"])

    def test_a_proposal_wrapped_in_one_fence_is_still_a_proposal(self) -> None:
        self.open_three_issues()
        fenced = f"```markdown\n{fixtures._draft_document()}```\n"

        with self.drafting(last_message=fenced):
            code, payload, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        self.assertEqual(
            fixtures._draft_document(),
            self.output.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
