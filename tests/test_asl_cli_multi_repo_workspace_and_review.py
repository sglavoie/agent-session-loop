"""Multi-repository dry-run workspace and review tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    handoff_guards,
    plans_metadata,
    plans_registry,
    review,
    review_continuation,
    review_delta,
    review_session_body,
    task_packet,
    verification,
    workspace_evidence,
)
from tests.support_asl_cli_multi_repo import MultiRepoRunnerHelpers


class MultiRepoWorkspaceAndReviewTests(MultiRepoRunnerHelpers, unittest.TestCase):
    """Multi-repository dry-run workspace and review tests."""

    def test_multi_repo_dry_run_persists_workspace_and_scoped_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
            )
            metadata_path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
            legacy = json.loads(metadata_path.read_text(encoding="utf-8"))
            legacy["workspace_inventory_excludes"] = ["Library/Protected.dv"]
            metadata_path.write_text(json.dumps(legacy), encoding="utf-8")
            metadata_before = metadata_path.read_bytes()
            completed = self._run(
                plan_dir, workspace_root, tmp_dir / "state", "--dry-run"
            )

            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            root = str(workspace_root.resolve())
            self.assertEqual(root, payload["cwd"])
            self.assertEqual(root, payload["workspace_root"])
            self.assertEqual(root, payload["run_config"]["workspace_root"])
            self.assertEqual(root, payload["planned"]["workspace_root"])
            self.assertNotIn("workspace_inventory_excludes", payload)
            self.assertNotIn("workspace_inventory_excludes", payload["run_config"])
            invocation = payload["planned"]["invocation"]
            self.assertEqual(root, invocation["cwd"])
            self.assertEqual(root, invocation["workspace_root"])
            prompt = payload["planned"]["prompt"]
            self.assertTrue(prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX))
            self.assertIn("Expected read aliases: SVC", prompt)
            self.assertIn("Writable aliases: SVC", prompt)
            self.assertIn("Alias:path/to/file", prompt)
            self.assertIn("OAK: path=tundra_OAK", prompt)
            self.assertIn(str(repositories["SVC"].resolve()), prompt)
            self.assertNotIn(str(repositories["OAK"].resolve()), prompt)
            self.assertEqual(
                metadata_before,
                (plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME).read_bytes(),
            )

    def test_multi_repo_review_combines_bases_and_uses_read_only_guard(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            dirty = repositories["OAK"] / "scratch.txt"
            dirty.write_text("pre-existing\n", encoding="utf-8")

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                env_overrides=self._fake_review_env(
                    tmp_dir, FAKE_CODEX_MODE="success-with-review"
                ),
            )

            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            review_turn = payload["turns"][-1]
            guard = review_turn["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual(["SVC", "OAK"], guard["read_aliases"])
            self.assertEqual([], guard["write_aliases"])
            bases = review_turn["review_repository_bases"]
            self.assertEqual(["SVC", "OAK"], [row["alias"] for row in bases])
            self.assertEqual(["OAK:scratch.txt"], bases[1]["dirty_paths"])
            review_path = plan_dir / f"session-{payload['review_session_id']}.md"
            body = review_path.read_text(encoding="utf-8")
            self.assertIn(
                review.review_session_version_marker(
                    review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
                ),
                body,
            )
            self.assertIn("**Reads repositories**: SVC, OAK", body)
            self.assertIn("**Writes repositories**: (none)", body)
            self.assertIn("## Ordered repository review base", body)
            artifact = json.loads(
                (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_text()
            )
            self.assertEqual(
                review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                artifact["version"],
            )
            cycle = payload["review_cycles"][-1]
            self.assertEqual(
                ["SVC", "OAK"],
                [row["alias"] for row in cycle["repositories"]],
            )

    def test_multi_repo_review_retains_qualified_repository_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            target = repositories["OAK"] / "README.md"

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                env_overrides=self._fake_review_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="success-with-review",
                    FAKE_CODEX_REVIEW_MUTATION_PATH=str(target),
                    FAKE_CODEX_REVIEW_MUTATION_TEXT="retained review edit\n",
                ),
            )

            self.assertEqual(
                1, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                payload["pause_reason"],
            )
            guard = payload["turns"][-1]["workspace_write_guard"]
            self.assertEqual([], guard["write_aliases"])
            self.assertEqual(
                ["OAK:README.md"], guard["violations"][0]["qualified_paths"]
            )
            self.assertTrue(guard["reverted_to_pending"])
            self.assertIn("retained review edit", target.read_text())
            review_path = plan_dir / f"session-{payload['review_session_id']}.md"
            self.assertIn(
                "- **Status**: pending", review_path.read_text(encoding="utf-8")
            )

    def test_multi_repo_review_accepts_repository_attributed_finding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                "--max-review-fix-cycles",
                "0",
                env_overrides=self._fake_review_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="success-with-review",
                    FAKE_CODEX_REVIEW_OUTCOMES="findings",
                ),
            )

            # Cycles are disabled, so the finding stays open and the run says so.
            self.assertEqual(
                1, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            finding = payload["turns"][-1]["review_verdict"]["findings"][0]
            self.assertEqual("SVC", finding["repo"])
            self.assertTrue(finding["evidence"][0]["path"].startswith("SVC:"))
            self.assertEqual("SVC", finding["verification_checks"][0]["repo"])

    def test_multi_repo_review_repairs_persist_scopes_and_qualified_lineage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                "--max-review-fix-cycles",
                "1",
                env_overrides=self._fake_review_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="success-with-review",
                    FAKE_CODEX_REVIEW_OUTCOMES="findings,all-clear",
                ),
            )

            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            first_cycle = payload["review_cycles"][0]
            self.assertEqual(
                review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                first_cycle["disposition"],
            )
            self.assertEqual("SVC", first_cycle["findings"][0]["repo"])
            self.assertTrue(
                first_cycle["findings"][0]["evidence"][0]["path"].startswith("SVC:")
            )
            group = first_cycle["repair_groups"][0]
            self.assertEqual(["SVC", "OAK"], group["read_repositories"])
            self.assertEqual(["SVC"], group["write_repositories"])
            child = plan_dir / f"session-{group['repair_session_id']}.md"
            child_text = child.read_text(encoding="utf-8")
            self.assertIn("**Reads repositories**: SVC, OAK", child_text)
            self.assertIn("**Writes repositories**: SVC", child_text)
            self.assertIn("`SVC:", child_text)
            repair_turn = next(
                turn
                for turn in payload["turns"]
                if turn["session_id"] == group["repair_session_id"]
            )
            self.assertEqual(
                ["SVC"],
                repair_turn["workspace_write_guard"]["write_aliases"],
            )

    def test_multi_repo_rereview_delta_keeps_repository_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            context = verification.VerificationContext.from_plan(
                plan_dir, workspace_root
            )
            assert context.workspace is not None
            snapshot = handoff_guards._capture_workspace_write_guard_snapshot(
                workspace=context.workspace,
                plan_dir=plan_dir,
                exclude=(),
                include_ignored=False,
            )
            bases = workspace_evidence._workspace_review_bases(
                context.workspace, snapshot
            )
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "## Findings\nStatus: 1 Findings (0 high, 1 med, 0 low)\n",
                encoding="utf-8",
            )
            artifact = {
                "version": review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                "outcome": "findings",
                "findings": [
                    {
                        "id": "cross-repo",
                        "repo": "SVC",
                        "severity": "med",
                        "title": "Cross repository mismatch",
                        "evidence": [{"path": "OAK:README.md"}],
                        "explanation": "The repositories disagree.",
                        "repair_group": "cross-repo",
                        "done_criteria": ["Both repositories agree."],
                        "verification_checks": [
                            {
                                "type": "command",
                                "name": "cross-check",
                                "repo": "SVC",
                                "argv": ["python3", "-c", "pass"],
                            }
                        ],
                    }
                ],
            }
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(artifact), encoding="utf-8"
            )
            event_path = tmp_dir / "events.jsonl"
            event_path.write_text(
                json.dumps(
                    {
                        "name": events.REVIEW_CYCLE_EVENT,
                        "payload": {
                            "disposition": (
                                review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                            ),
                            "repositories": bases,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            for alias in ("SVC", "OAK"):
                (repositories[alias] / "shared.txt").write_text(
                    f"{alias}\n", encoding="utf-8"
                )

            delta, fallback = review_delta._compute_registered_review_delta_context(
                event_path=event_path,
                plan_dir=plan_dir,
                workspace=context.workspace,
            )

            self.assertIsNone(fallback)
            assert delta is not None
            self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
            self.assertEqual(
                ["SVC:shared.txt", "OAK:shared.txt"],
                delta["changed_files"],
            )
            rendered = review_session_body._review_scope_detail_lines(delta)
            text = "\n".join(rendered)
            self.assertIn("git -C tundra_svc diff", text)
            self.assertIn("git -C tundra_OAK diff", text)

            event_path.write_text(
                json.dumps(
                    {
                        "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                        "payload": {
                            "materialized": True,
                            "current_repositories": bases,
                            "current_findings": artifact["findings"],
                            "repair_session_ids": ["2.1"],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            recovered_delta, recovered_fallback = (
                review_delta._compute_registered_review_delta_context(
                    event_path=event_path,
                    plan_dir=plan_dir,
                    workspace=context.workspace,
                )
            )

            self.assertIsNone(recovered_fallback)
            assert recovered_delta is not None
            self.assertEqual(delta["repositories"], recovered_delta["repositories"])
            self.assertEqual(delta["changed_files"], recovered_delta["changed_files"])


if __name__ == "__main__":
    unittest.main()
