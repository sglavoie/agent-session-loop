"""Worker verification accepts complete evidence and rejects unsupported claims."""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from typing import Any

from .support_asl_worker_verification import (
    SyntheticTrial,
    TempRootMixin,
    _accepting_cache,
    _clean_trial_arguments,
    _gate_event,
    _invocation,
    evaluate,
)

# --------------------------------------------------------------------------
# Evidence validation
# --------------------------------------------------------------------------


class AcceptanceTests(TempRootMixin):
    def test_a_complete_consistent_set_of_trials_is_accepted(self) -> None:
        _accepting_cache(self.root)
        report = evaluate.check_cache(self.root)
        self.assertTrue(report["accepted"], report["problems"])
        self.assertEqual([], report["problems"])

    def test_an_empty_cache_is_not_accepted(self) -> None:
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("0 receipt(s)" in problem for problem in report["problems"]),
            report["problems"],
        )

    def test_one_trial_per_scenario_is_not_enough(self) -> None:
        for scenario in evaluate.SCENARIOS:
            SyntheticTrial(
                self.root,
                **_clean_trial_arguments(
                    scenario.key, 1, conversation=f"conv-{scenario.key}"
                ),
            )
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("1 receipt(s), 2 required" in p for p in report["problems"]),
            report["problems"],
        )

    def test_two_trials_that_share_a_conversation_are_not_fresh(self) -> None:
        _accepting_cache(self.root)
        SyntheticTrial(
            self.root,
            **{
                **_clean_trial_arguments(
                    "focused-final-gate", 2, conversation="conv-focused-final-gate-1"
                )
            },
        )
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("share conversation id" in p for p in report["problems"]),
            report["problems"],
        )

    def test_a_trial_with_no_recorded_conversation_is_not_accepted(self) -> None:
        _accepting_cache(self.root)
        arguments = _clean_trial_arguments(
            "focused-final-gate", 2, conversation="conv-x"
        )
        arguments["conversations"] = []
        for row in arguments["invocations"]:
            row["worker_conversation_id"] = None
        SyntheticTrial(self.root, **arguments)
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("no conversation was recorded" in p for p in report["problems"]),
            report["problems"],
        )

    def test_an_unreadable_receipt_is_a_problem_rather_than_a_skip(self) -> None:
        _accepting_cache(self.root)
        target = self.root / "trials" / "focused-retry" / "1" / "receipt.json"
        target.write_text("{not json", encoding="utf-8")
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("unreadable receipt" in p for p in report["problems"]),
            report["problems"],
        )


class RejectedEvidenceTests(TempRootMixin):
    """``check`` refuses missing, incomplete, contradictory and stale evidence."""

    def _trial(self, scenario: str = "focused-final-gate", **changes: Any) -> Any:
        arguments = _clean_trial_arguments(scenario, 1, conversation="conv-1")
        arguments.update(changes)
        return SyntheticTrial(self.root, **arguments)

    def test_a_written_passed_claim_is_not_evidence(self) -> None:
        trial = self._trial(
            overrides={
                "passed": True,
                "verdict": "accepted",
                "notes": "All criteria satisfied.",
            }
        )
        shutil.rmtree(Path(trial.receipt["workspace"]["root"]))
        criteria = self.criteria(trial)
        self.assertFalse(criteria["evidence-present"].passed)
        self.assertIn("invocation log is absent", criteria["evidence-present"].detail)

    def test_a_missing_invocation_log_is_missing_evidence(self) -> None:
        trial = self._trial()
        Path(trial.receipt["workspace"]["invocations"]).unlink()
        criteria = self.criteria(trial)
        self.assertFalse(criteria["evidence-present"].passed)

    def test_an_incomplete_receipt_is_rejected(self) -> None:
        trial = self._trial()
        receipt = dict(trial.receipt)
        receipt.pop("final_state")
        receipt.pop("candidate")
        evidence = evaluate.load_evidence(receipt)
        criteria = {
            criterion.name: criterion
            for criterion in evaluate.evaluate_trial(
                receipt,
                evidence,
                expected_policy_digest=receipt["policy_digest"],
                expected_fixture_digest=receipt["fixture_digest"],
            )
        }
        self.assertFalse(criteria["receipt-complete"].passed)
        self.assertIn("final_state", criteria["receipt-complete"].detail)

    def test_a_receipt_from_another_schema_is_rejected(self) -> None:
        trial = self._trial(overrides={"schema": evaluate.RECEIPT_VERSION + 1})
        self.assertFalse(self.criteria(trial)["receipt-complete"].passed)

    def test_a_stale_policy_digest_is_rejected(self) -> None:
        trial = self._trial(overrides={"policy_digest": "0" * 64})
        criteria = self.criteria(trial)
        self.assertFalse(criteria["policy-current"].passed)
        self.assertIn("!= current", criteria["policy-current"].detail)

    def test_a_stale_fixture_digest_is_rejected(self) -> None:
        trial = self._trial(overrides={"fixture_digest": "0" * 64})
        self.assertFalse(self.criteria(trial)["fixture-current"].passed)

    def test_a_trial_run_before_the_policy_existed_is_rejected(self) -> None:
        surfaces: dict[str, list[str]] = {
            "packet:implementation": [],
            "packet:review": [],
            "skill:session-run": [],
        }
        trial = self._trial(
            overrides={
                "policy_surfaces": surfaces,
                "policy_digest": evaluate.policy_digest(surfaces),
            }
        )
        criteria = self.criteria(trial)
        self.assertFalse(criteria["policy-stated"].passed)
        self.assertFalse(criteria["policy-current"].passed)

    def test_a_fake_harness_receipt_is_rejected(self) -> None:
        trial = self._trial(overrides={"live": False})
        criteria = self.criteria(trial)
        self.assertFalse(criteria["live-harness"].passed)
        self.assertIn("injected harness", criteria["live-harness"].detail)

    def test_gate_events_without_matching_invocations_are_contradictory(self) -> None:
        """The run says it ran the gate; nothing recorded the gate running."""
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["invocations"] = [
            row for row in arguments["invocations"] if row["actor"] != "runner"
        ]
        trial = SyntheticTrial(self.root, **arguments)
        criteria = self.criteria(trial)
        self.assertFalse(criteria["gate-record-consistent"].passed)
        self.assertIn("0 runner-attributed", criteria["gate-record-consistent"].detail)

    def test_invocations_without_matching_gate_events_are_contradictory(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["events"] = []
        trial = SyntheticTrial(self.root, **arguments)
        criteria = self.criteria(trial)
        self.assertFalse(criteria["runner-gate-executed"].passed)
        self.assertFalse(criteria["gate-record-consistent"].passed)

    def test_a_failing_runner_gate_is_not_a_pass(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["events"] = [_gate_event(status="fail")]
        trial = SyntheticTrial(self.root, **arguments)
        self.assertFalse(self.criteria(trial)["runner-gate-executed"].passed)

    def test_an_unfinished_session_is_rejected(self) -> None:
        trial = self._trial(handoff_status="pending")
        self.assertFalse(self.criteria(trial)["session-complete"].passed)

    def test_a_defect_still_present_afterwards_is_rejected(self) -> None:
        trial = self._trial(
            final_state={
                "focused_exit": 1,
                "contract_exit": 1,
                "changed_paths": [],
                "head": "2" * 40,
            }
        )
        self.assertFalse(self.criteria(trial)["defect-fixed"].passed)

    def test_a_worker_that_ran_no_focused_check_is_rejected(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["invocations"] = [
            row for row in arguments["invocations"] if row["actor"] != "worker"
        ]
        trial = SyntheticTrial(self.root, **arguments)
        self.assertFalse(self.criteria(trial)["focused-check-used"].passed)


class BroadGateTests(TempRootMixin):
    def _with_worker_contract(self, notes: str) -> Any:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["invocations"].append(
            _invocation(
                check=evaluate.CONTRACT_CHECK_NAME,
                actor="worker",
                started_at="2026-09-12T00:00:00.5+00:00",
                conversation="c",
            )
        )
        arguments["handoff_notes"] = notes
        return SyntheticTrial(self.root, **arguments)

    def test_a_worker_running_the_broad_gate_without_a_reason_is_rejected(self) -> None:
        trial = self._with_worker_contract("Fixed the conversion.")
        criterion = self.criteria(trial)["no-unjustified-broad-gate"]
        self.assertFalse(criterion.passed)
        self.assertIn("no rationale", criterion.detail)

    def test_a_recorded_reason_makes_a_repeat_acceptable(self) -> None:
        trial = self._with_worker_contract(
            "Ran the contract gate once after the second edit because the "
            "focused check could not cover the import it moved."
        )
        self.assertTrue(self.criteria(trial)["no-unjustified-broad-gate"].passed)

    def test_naming_the_gate_is_not_a_reason_for_having_run_it(self) -> None:
        trial = self._with_worker_contract("Ran checks/contract.py. All green.")
        self.assertFalse(self.criteria(trial)["no-unjustified-broad-gate"].passed)

    def test_leaving_the_broad_gate_alone_needs_no_note(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["no-unjustified-broad-gate"]
        self.assertTrue(criterion.passed)
        self.assertIn("never ran the broad gate", criterion.detail)


class ScenarioCriteriaTests(TempRootMixin):
    def test_a_retry_needs_a_failure_before_it(self) -> None:
        arguments = _clean_trial_arguments("focused-retry", 1, conversation="c")
        arguments["invocations"] = [
            row
            for row in arguments["invocations"]
            if not (row["actor"] == "worker" and row["exit_code"] != 0)
        ]
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["focused-retry-after-failure"]
        self.assertFalse(criterion.passed)
        self.assertIn("0 failing", criterion.detail)

    def test_a_retry_that_never_came_good_is_rejected(self) -> None:
        arguments = _clean_trial_arguments("focused-retry", 1, conversation="c")
        for row in arguments["invocations"]:
            if row["actor"] == "worker":
                row["exit_code"] = 1
        trial = SyntheticTrial(self.root, **arguments)
        self.assertFalse(self.criteria(trial)["focused-retry-after-failure"].passed)

    def test_a_failure_then_a_pass_is_the_justified_retry(self) -> None:
        arguments = _clean_trial_arguments("focused-retry", 1, conversation="c")
        trial = SyntheticTrial(self.root, **arguments)
        self.assertTrue(self.criteria(trial)["focused-retry-after-failure"].passed)

    def test_running_the_other_groups_check_breaks_scope(self) -> None:
        arguments = _clean_trial_arguments("scope-preserved", 1, conversation="c")
        arguments["invocations"].append(
            _invocation(check="legacy", actor="worker", exit_code=1, conversation="c")
        )
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["no-unrelated-check"]
        self.assertFalse(criterion.passed)
        self.assertIn("1 worker invocation", criterion.detail)

    def test_editing_the_other_groups_package_breaks_scope(self) -> None:
        arguments = _clean_trial_arguments("scope-preserved", 1, conversation="c")
        arguments["final_state"] = {
            "focused_exit": 0,
            "contract_exit": 0,
            "changed_paths": ["toolkit.py", "legacy_report/report.py"],
            "head": "3" * 40,
        }
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["no-unrelated-edit"]
        self.assertFalse(criterion.passed)
        self.assertIn("legacy_report/report.py", criterion.detail)

    def test_scope_criteria_apply_only_to_the_scope_scenario(self) -> None:
        trial = SyntheticTrial(
            self.root,
            **_clean_trial_arguments("focused-final-gate", 1, conversation="c"),
        )
        criteria = self.criteria(trial)
        self.assertNotIn("no-unrelated-check", criteria)
        self.assertNotIn("focused-retry-after-failure", criteria)


if __name__ == "__main__":
    unittest.main()
