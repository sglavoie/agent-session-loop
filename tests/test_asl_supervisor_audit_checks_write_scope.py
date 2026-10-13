"""Deterministic checks for harness roles, commit modes, and write scope."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import events, plans, state, supervisor_audit_checks
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import RUN_ID


class DeterministicCheckTests(checks_support.DeterministicCheckFixture):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def _unclassified_external_write(
        self, history: tuple[Path, Path, Path, Path]
    ) -> None:
        """Record the refusal a legacy turn earns for an unclassified write.

        This is the shape the runner writes when a turn touched a path the
        external-write pass could not name: ``violations`` stays empty because
        no repository alias is implicated, and the guard still fails the turn,
        reverts it to pending, and stops the run under its unavailable reason.
        """
        guard = {
            "passed": False,
            "session_id": "01",
            "reason": events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            "phase": "after",
            "reverted_to_pending": True,
            "write_aliases": ["app"],
            "authorized_aliases": ["app"],
            "violations": [],
            "unavailable": {
                "/tmp/asl-audit-scratch": {"reason": "external-write-unclassified"}
            },
        }
        payload = self._read_result(history[3])
        payload["workspace_write_guard"] = guard
        payload["session_id"] = "01"
        self._write_result(history[3] / "turn-0001-01", payload)
        self._write_events(
            history[2],
            [
                ("run_started", {"run_id": RUN_ID}),
                (
                    events.RUN_PAUSED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "reason": events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                        "details": {
                            "session": 1,
                            "workspace_write_guard": dict(guard),
                        },
                    },
                ),
            ],
        )

    def _worker_commit(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        relation: str = "fast-forward",
        commits_added: int = 1,
        refused: bool = True,
        transition: bool = True,
    ) -> None:
        """Record a turn whose worker moved HEAD before the runner could commit.

        The guard's comparison is taken after the harness exits and before the
        runner commits anything, so a tip that moved inside it moved under the
        worker. ``refused`` is what decides whether the commit outlived a turn
        the run kept: verification failing after a clean exit is the shape
        ASI-142 was reported for, and it shows up nowhere but here.
        """
        changes: dict[str, Any] = {
            "head_changed": True,
            "head_before": "a" * 40,
            "head_after": "b" * 40,
        }
        if transition:
            changes["head_transition"] = {
                "before": "a" * 40,
                "after": "b" * 40,
                "relation": relation,
                "commits_added": commits_added,
                "commits_removed": 0,
                "history_rewrite": relation in {"rewrite", "rewind", "deleted"},
            }
        payload = self._read_result(history[3])
        payload["workspace_write_guard"]["changed"] = {"app": changes}
        if refused:
            payload["verification"] = {
                "passed": False,
                "checks": [{"name": "tests", "passed": False, "returncode": 1}],
                "skipped_checks": [],
            }
        self._write_result(history[3] / "turn-0001-01", payload)

    def test_a_harness_reviewing_its_own_work_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_harness"] = payload["harness"]
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["review-independence"].status)
            self.assertIn("its own work", results["review-independence"].detail)
            self.assertEqual(
                "fail",
                supervisor_audit_checks.lane_rollup(results.values())[
                    "verification-review-independence"
                ],
            )

    def test_an_unnamed_implementation_harness_is_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload.pop("harness")
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["review-independence"].status)
            self.assertIn("did the work", results["review-independence"].detail)

    def test_a_registered_turn_without_a_write_guard_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            del payload["workspace_write_guard"]
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["mutation-commit-guards"].status)
            self.assertIn("write-guard", results["mutation-commit-guards"].detail)
            self.assertEqual("unavailable", results["scope-escape"].status)

    def test_repair_commit_mode_authorizes_attempt_under_null_ordinary_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_payload = self._read_state(history[2])
            state_payload["commit_mode"] = None
            state_payload["repair_commit_mode"] = "per-child"
            self._write_state(history[2], state_payload)
            commit_dir = history[3] / "commit-0001-session-01-app"
            commit_dir.mkdir()
            (commit_dir / state.RESULT_FILE_NAME).write_text(
                json.dumps(
                    {
                        "success": True,
                        "returncode": 0,
                        "repository_alias": "app",
                        "session_id": "01",
                    }
                ),
                encoding="utf-8",
            )

            _, results = self._audit(history)

            verdict = results["mutation-commit-guards"]
            self.assertEqual("pass", verdict.status, verdict.detail)

    def test_null_modes_do_not_authorize_commit_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_payload = self._read_state(history[2])
            state_payload["commit_mode"] = None
            state_payload["repair_commit_mode"] = None
            self._write_state(history[2], state_payload)
            commit_dir = history[3] / "commit-0001-session-01-app"
            commit_dir.mkdir()
            (commit_dir / state.RESULT_FILE_NAME).write_text(
                json.dumps(
                    {
                        "success": True,
                        "returncode": 0,
                        "repository_alias": "app",
                        "session_id": "01",
                    }
                ),
                encoding="utf-8",
            )

            _, results = self._audit(history)

            verdict = results["mutation-commit-guards"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("without recording the commit mode", verdict.detail)

    def test_a_legacy_implementation_turn_without_a_guard_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            repository = history[0] / "app"
            (history[1] / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (history[1] / "session-01.md").write_text("# Session 1\n", encoding="utf-8")
            (history[1] / plans.PLAN_METADATA_FILE_NAME).write_text(
                json.dumps({"version": 1, "cwd": str(repository)}),
                encoding="utf-8",
            )
            state_payload = self._read_state(history[2])
            state_payload.pop("repositories")
            state_payload.pop("workspace_root")
            state_payload["commit_mode"] = None
            state_payload["run_config"]["cwd"] = str(repository)
            self._write_state(history[2], state_payload)
            result_payload = self._read_result(history[3])
            del result_payload["workspace_write_guard"]
            self._write_result(history[3] / "turn-0001-01", result_payload)

            _, results = self._audit(history, workspace_root=repository)

            self.assertEqual("fail", results["mutation-commit-guards"].status)
            self.assertIn(
                "implementation work without a write-guard",
                results["mutation-commit-guards"].detail,
            )

    def test_a_worker_head_move_without_transition_evidence_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["workspace_write_guard"]["changed"] = {
                "app": {
                    "head_changed": True,
                    "head_before": "a" * 40,
                    "head_after": "b" * 40,
                }
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["mutation-commit-guards"].status)
            self.assertIn(
                "history transition", results["mutation-commit-guards"].detail
            )

    def test_a_recorded_worker_history_rewrite_is_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["workspace_write_guard"]["changed"] = {
                "app": {
                    "head_changed": True,
                    "head_before": "a" * 40,
                    "head_after": "b" * 40,
                    "head_transition": {
                        "before": "a" * 40,
                        "after": "b" * 40,
                        "relation": "rewrite",
                        "commits_added": 3,
                        "commits_removed": 3,
                        "history_rewrite": True,
                    },
                }
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            verdict = results["mutation-commit-guards"]
            self.assertEqual("pass", verdict.status)
            self.assertIn("1 worker HEAD move(s)", verdict.detail)
            self.assertIn("1 history rewrite(s)", verdict.detail)

    def test_a_widened_write_scope_with_a_passing_repair_is_contained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": RUN_ID,
                            "details": {
                                "session_id": "01",
                                "workspace_write_guard": {
                                    "passed": True,
                                    "write_aliases": ["app"],
                                    "authorized_aliases": ["app"],
                                    "violations": [],
                                    "unavailable": {},
                                },
                            },
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["scope-escape"].status)
            self.assertIn(
                "2 recorded write-guard and repair results",
                results["scope-escape"].detail,
            )

    def test_a_scope_repair_without_a_passing_guard_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": RUN_ID,
                            "details": {
                                "session_id": "01",
                                "workspace_write_guard": {},
                            },
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertIn(
                "did not record a passing guard", results["scope-escape"].detail
            )

    def test_a_write_the_guard_caught_is_medium_and_never_a_containment_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)

            _, results = self._audit(history)

            # A working guard is what produced this evidence, so the critical
            # containment check has nothing to report.
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertIn(
                "none of them reached the tree", results["scope-escape"].detail
            )
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn("session 01", results["scope-guard-catch"].detail)
            self.assertIn("docs", results["scope-guard-catch"].detail)
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit_checks.findings_from_results(
                    results.values()
                )
            }
            self.assertNotIn("packet.write-scope-containment", findings)
            caught = findings["packet.write-scope-guard-catch"]
            self.assertEqual("medium", caught.severity)
            self.assertEqual("task-packet-coherence", caught.lane)
            self.assertIn("durable write scope", caught.recommendation)
            self.assertIn("expansion and preflight", caught.recommendation)
            # Nothing is masked: the two checks share a lane and the rollup
            # keeps the worse of them.
            self.assertEqual(
                "fail",
                supervisor_audit_checks.lane_rollup(results.values())[
                    "task-packet-coherence"
                ],
            )

    def test_one_caught_write_is_reported_once_across_turn_and_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)

            _, results = self._audit(history)

            self.assertEqual(
                1, results["scope-guard-catch"].detail.count("wrote outside")
            )

    def test_a_violation_the_run_never_reverted_still_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, reverted=False)

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("not reverted to pending", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit_checks.findings_from_results(
                    results.values()
                )
            }
            self.assertNotIn("packet.write-scope-guard-catch", findings)
            self.assertEqual(
                "critical", findings["packet.write-scope-containment"].severity
            )

    def test_a_violation_the_run_never_paused_on_still_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, paused=False)

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn(
                "never paused on the violation", results["scope-escape"].detail
            )
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_an_unavailable_pause_carrying_the_violation_is_still_a_pause(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # A guard that both found violations and failed to inventory
            # something stops the run under the unavailable reason, which is the
            # worse of the two. The run still paused on this guard record.
            self._scope_violation(
                history,
                pause_reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            )

            _, results = self._audit(history)

            self.assertNotIn(
                "never paused on the violation", results["scope-escape"].detail
            )
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn(
                "before anything was committed", results["scope-guard-catch"].detail
            )

    def test_an_unavailable_pause_naming_no_violation_corroborates_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # The run stopped because the guard could not look, not because it
            # found this write, so nothing here says the write was held.
            self._scope_violation(
                history,
                pause_reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                pause_violations=[],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn(
                "never paused on the violation", results["scope-escape"].detail
            )

    def test_a_turn_stopped_for_an_unattributable_write_is_not_reported_as_none(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._unclassified_external_write(history)

            _, results = self._audit(history)

            # The guard has nothing this check can attribute to a declared
            # scope, so the critical reading stays scope-escape's and the status
            # here stays clear. What must not survive is the sentence: a session
            # was stopped, and reporting that none was states the opposite of
            # what the run recorded (ASI-140).
            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertEqual("pass", results["scope-guard-catch"].status)
            detail = results["scope-guard-catch"].detail
            self.assertIn("this check could attribute", detail)
            self.assertIn("could not be inventoried", detail)
            self.assertIn("scope-escape", detail)
            findings = {
                finding.invariant_key
                for finding in supervisor_audit_checks.findings_from_results(
                    results.values()
                )
            }
            self.assertNotIn("packet.write-scope-guard-catch", findings)

    def test_a_run_with_nothing_to_report_still_says_so_plainly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            _, results = self._audit(history)

            # The narrowed sentence is for a blind guard only. A run the guard
            # read end to end keeps the flat claim, which is the one an operator
            # should be able to take at face value.
            self.assertEqual("pass", results["scope-guard-catch"].status)
            self.assertEqual(
                "no session had to be stopped for writing outside its declared scope",
                results["scope-guard-catch"].detail,
            )

    def test_a_refused_turn_that_kept_its_own_commits_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._worker_commit(history)

            _, results = self._audit(history)

            result = results["commit-after-verification"]
            self.assertEqual("fail", result.status)
            self.assertIn("turn-0001-01", result.detail)
            self.assertIn("1 commit(s) by fast-forward", result.detail)
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit_checks.findings_from_results(
                    results.values()
                )
            }
            # The fingerprint is the one the agent lane already reports this
            # under, so a run both lanes grade folds into one issue rather than
            # into two spellings of the same defect (ASI-142).
            self.assertEqual(
                "commit-promotion",
                findings["guards.commit-after-verification"].component,
            )
            self.assertEqual(
                "high", findings["guards.commit-after-verification"].severity
            )

    def test_a_commit_the_run_accepted_is_not_a_retained_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._worker_commit(history, refused=False)

            _, results = self._audit(history)

            # The complaint is about a commit outliving a refusal, not about a
            # worker committing. A turn the run kept has its work in the
            # history the run's own verification passed.
            result = results["commit-after-verification"]
            self.assertEqual("pass", result.status)
            self.assertEqual(
                "no turn the run refused left a commit of its own on HEAD",
                result.detail,
            )

    def test_a_rewritten_history_is_named_as_a_rewrite_not_as_new_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._worker_commit(history, relation="rewrite", commits_added=94)

            _, results = self._audit(history)

            # A rewrite's count is every commit the new tip reaches that the old
            # one did not, which is the rewritten range rather than the turn's
            # own work. Both outlive the refusal; reporting them as the same
            # kind of number would overstate one of them.
            self.assertEqual("fail", results["commit-after-verification"].status)
            self.assertIn(
                "94 commit(s) by rewrite",
                results["commit-after-verification"].detail,
            )

    def test_a_head_move_with_no_transition_is_blindness_this_check_says_out_loud(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._worker_commit(history, transition=False)

            _, results = self._audit(history)

            # A guard that saw the tip move and recorded no count for it knows a
            # commit happened and cannot say how many. Reading that as none
            # would turn the one case worth reporting into a clean answer, and
            # grading it `unavailable` here would be a second opinion about a
            # gap the mutation check already owns.
            result = results["commit-after-verification"]
            self.assertEqual("pass", result.status)
            self.assertIn("no readable", result.detail)
            self.assertIn("mutation-and-commit", result.detail)
            findings = {
                finding.invariant_key
                for finding in supervisor_audit_checks.findings_from_results(
                    results.values()
                )
            }
            self.assertNotIn("guards.commit-after-verification", findings)


if __name__ == "__main__":
    unittest.main()
