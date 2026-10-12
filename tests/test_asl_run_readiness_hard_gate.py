"""Run readiness hard gate: refusals the gate raises before a turn launches."""

from __future__ import annotations

import json
import unittest

from agent_session_loop import (
    events,
    plans,
)
from tests.support import (
    _contract_block,
    init_git_repo,
)
from tests.support_asl_run_readiness import RunReadinessGateFixture


class RunReadinessHardGateTests(RunReadinessGateFixture):
    """The hard gate refuses unsafe plans and redirects before a harness launches."""

    def test_allow_not_loop_ready_lets_run_proceed_and_reports_findings(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertTrue(payload["allow_not_loop_ready"])
        gate = payload["readiness_gate"]
        self.assertFalse(gate["ok"])
        self.assertIsNone(gate["readiness_state"])
        self.assertTrue(
            any(
                finding["category"] == "manifest-readiness"
                for finding in gate["findings"]
            )
        )
        # No readiness existed, so the lifecycle never engages.
        self.assertIsNone(payload["readiness_state_initial"])
        self.assertIsNone(payload["readiness_state_final"])
        self.assertIsNone(plans.read_readiness_metadata(self.plan_dir))

    def test_allow_not_loop_ready_does_not_bypass_repository_footprint(
        self,
    ) -> None:
        # A target above the bound root but inside the same repository escapes
        # without adding a second repository, so the readiness gate — not the
        # hard intent gate — is what has to refuse it. A target owned by no
        # repository at all is released deliberately: no registry row could
        # describe it.
        escaping = self.repo_root / "README.md"
        self.repo_root = self.repo_root / "src"
        self.repo_root.mkdir()
        self._write_plan(
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(escaping),
                            "name": "out-of-root",
                        }
                    ),
                    session="1",
                )
            },
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        self.assertEqual(
            ["repository-footprint"],
            payload["pause_details"]["blocker"],
        )
        self.assertEqual([], payload["turns"])

    def test_allow_not_loop_ready_does_not_bypass_repository_intent(
        self,
    ) -> None:
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self._write_plan(
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(other_repo / "README.md"),
                            "name": "cross-repo",
                        }
                    ),
                    session="1",
                )
            },
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])
        self.assertEqual(
            sorted(
                str(plans.canonicalize_path(path))
                for path in (self.repo_root, other_repo)
            ),
            sorted(report["git_roots"]),
        )
        self.assertEqual([], payload["turns"])
        self.assertFalse(payload["pause_policy"]["recoverable"])

    def test_hard_gate_rejects_sole_git_target_outside_non_git_root(self) -> None:
        target_repository = self.tmp_path / "target"
        init_git_repo(target_repository)
        self.repo_root = self.tmp_path / "operator-root"
        self.repo_root.mkdir()
        self._write_plan(
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(target_repository / "README.md"),
                            "name": "outside-bound-root",
                        }
                    ),
                    session="1",
                )
            },
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        report = payload["pause_details"]["repository_intent"]
        self.assertIsNone(report["bound_git_root"])
        self.assertEqual(1, report["git_root_count"])
        self.assertEqual(
            [str(plans.canonicalize_path(target_repository))],
            report["git_roots"],
        )

    def test_hard_gate_applies_the_registry_before_the_first_session(
        self,
    ) -> None:
        self._add_origin(self.repo_root, "git@github.com:tundra/repo.git")
        other_repo = self._write_cross_repo_plan()
        self._add_origin(other_repo, "git@github.com:tundra/other.git")

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        registration = payload["pause_details"]["repository_registration"]
        self.assertEqual("applied", registration["status"])
        self.assertEqual(
            str(plans.canonicalize_path(self.tmp_path)),
            registration["workspace_root"],
        )
        self.assertEqual(2, len(registration["repositories"]))
        repairs = payload["pause_details"]["auto_repairs"]
        self.assertEqual(1, len(repairs))
        self.assertEqual("repository-registry", repairs[0]["action"])
        self.assertEqual("repository-registry-required", repairs[0]["category"])
        self.assertEqual(
            str(plans.canonicalize_path(self.plan_dir)), repairs[0]["path"]
        )
        self.assertEqual(len(registration["files"]), repairs[0]["entries_written"])
        # The files are migrated, so only the rebind and the re-run are left.
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("## Repositories", manifest)
        session = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("**Writes repositories**:", session)
        self.assertEqual(2, len(registration["next_commands"]))
        for command in registration["next_commands"]:
            self.assertIn(command, payload["pause_message"])

    def test_hard_gate_refuses_to_migrate_a_completed_legacy_handoff(
        self,
    ) -> None:
        # A registry makes a completed handoff's unqualified `Files modified`
        # entries illegal, so the plan would stop parsing entirely. The gate
        # delegates that judgement to the migration itself, which refuses.
        self._add_origin(self.repo_root, "git@github.com:tundra/repo.git")
        other_repo = self._write_cross_repo_plan(("complete", "pending"))
        self._add_origin(other_repo, "git@github.com:tundra/other.git")
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "- **Files modified**: (none)",
                "- **Files modified**: src/new.py",
            ),
            encoding="utf-8",
        )
        before = self._plan_dir_bytes()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        registration = payload["pause_details"]["repository_registration"]
        self.assertEqual("refused", registration["status"])
        self.assertEqual("completed-handoff-paths", registration["missing_fact"])
        self.assertIn("session-1.md", registration["detail"])
        self.assertNotIn("auto_repairs", payload["pause_details"])
        self.assertIn("completed-handoff-paths", payload["pause_message"])
        self.assertEqual(before, self._plan_dir_bytes())

    def test_hard_gate_refuses_to_migrate_a_repository_without_an_origin(
        self,
    ) -> None:
        self._write_cross_repo_plan()
        before = self._plan_dir_bytes()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        registration = payload["pause_details"]["repository_registration"]
        self.assertEqual("refused", registration["status"])
        self.assertEqual("repository-origin", registration["missing_fact"])
        self.assertNotIn("auto_repairs", payload["pause_details"])
        self.assertIn("repository-origin", payload["pause_message"])
        self.assertEqual(before, self._plan_dir_bytes())

    def test_hard_gate_refuses_a_loop_ready_plan_that_skips_preflight(
        self,
    ) -> None:
        # A loop-ready plan short-circuits core preflight, so the readiness
        # gate never re-derives the verdict. The run-level gate must.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self._write_plan(("pending",))
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "Instructions.",
                f"Instructions. Write the helper into {other_repo}/src/new.py.",
            ),
            encoding="utf-8",
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        self.assertEqual(
            2, payload["pause_details"]["repository_intent"]["git_root_count"]
        )

    def test_hard_gate_exempts_the_runner_own_plan_and_state_trees(self) -> None:
        # An operator may keep the plan and the state root inside a checkout
        # unrelated to the work. That is ASL's bookkeeping, not a second
        # repository the run intends to reach.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self.plan_dir = other_repo / "plans" / "current"
        self.state_dir = other_repo / "state"
        self._write_plan(("pending",))

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_task_packet_recheck_catches_a_redirect_only_expansion_notes_carry(
        self,
    ) -> None:
        # Expansion notes are outside the plan-level scan, so the packet
        # recheck is the only surface that sees this redirect.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self._write_plan(("pending",))
        (self.plan_dir / "session-1.expand.md").write_text(
            f"# Session 1 Expansion\n\n- Corrected location: {other_repo}/src/new.py\n",
            encoding="utf-8",
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])
        self.assertEqual(1, payload["pause_details"]["session"])

    def test_required_plan_store_state_root_pauses_before_spending_a_turn(self) -> None:
        store = self.tmp_path / "external-store"
        contract = _contract_block(
            '{"type":"file_exists","path":"README.md","name":"readme"}'
        ).replace("recipe runs", f"All plan-store writes used --state-dir {store}")
        self._write_plan(contracts_by_id={"1": contract})

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        self.assertEqual(
            [str(plans.canonicalize_path(store))],
            payload["pause_details"]["plan_store_state_root"]["required_state_roots"],
        )
        self.assertEqual(
            "pending", plans.inspect_plan(self.plan_dir).sessions[0].status
        )
        self.assertFalse(store.exists())

    def test_required_plan_store_state_root_launches_when_configured(self) -> None:
        contract = _contract_block(
            '{"type":"file_exists","path":"README.md","name":"readme"}'
        ).replace(
            "recipe runs", f"All plan-store writes used --state-dir {self.state_dir}"
        )
        self._write_plan(contracts_by_id={"1": contract})

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(1, len(payload["turns"]))

    def test_hard_gate_exempts_the_document_the_plan_was_split_from(self) -> None:
        # The reported bug end to end: a plan citing where it came from, in
        # prose an `asl expand-ingest` regenerates, is not a plan that reaches
        # two repositories.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        source = other_repo / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(("pending",))
        self._declare_source(source, expansion_cites=f"{source}:209")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_hard_gate_still_pauses_on_a_redirect_beside_the_source_document(
        self,
    ) -> None:
        # The sharp pin: the exemption covers the source document and the
        # directory holding it, never the rest of that repository.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        source = other_repo / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(("pending",))
        self._declare_source(source, expansion_cites=f"{other_repo}/src/new.py")

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])


if __name__ == "__main__":
    unittest.main()
