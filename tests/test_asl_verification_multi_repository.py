"""Verification across registered repositories."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import contracts, plans_errors, plans_session_ids, verification
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)


class MultiRepositoryVerificationTests(unittest.TestCase):
    def test_dispatches_checks_and_reports_qualified_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = root / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            (repositories["SVC"] / "target.txt").write_text("svc\n", encoding="utf-8")
            nested = repositories["OAK"] / "nested"
            nested.mkdir()
            contract = contracts.CompletionContract(
                session_id=plans_session_ids.parse_session_id("1"),
                done_when=("done",),
                verify=contracts.VerifyRecipe(
                    checks=(
                        contracts.FileExistsCheck(
                            name="svc-file", path="target.txt", repo="SVC"
                        ),
                        contracts.CommandCheck(
                            name="oak-command",
                            argv=(
                                sys.executable,
                                "-c",
                                "import pathlib; assert pathlib.Path.cwd().name == "
                                "'nested'",
                            ),
                            cwd="nested",
                            timeout_seconds=10.0,
                            repo="OAK",
                        ),
                    )
                ),
            )
            context = verification.verification_context_from_plan(
                plan_dir, workspace_root
            )
            started: list[verification.CheckStartedEvent] = []

            result = verification.run_verification(
                contract,
                context=context,
                observer=verification.VerificationObserver(
                    on_check_started=started.append
                ),
            )

            self.assertTrue(result.passed)
            self.assertEqual(
                ["svc-file", "oak-command"], [r.name for r in result.checks]
            )
            self.assertEqual("SVC:target.txt", result.checks[0].qualified_path)
            self.assertEqual(
                str(repositories["SVC"].resolve()), result.checks[0].resolved_root
            )
            self.assertEqual("OAK:nested", result.checks[1].qualified_cwd)
            self.assertEqual(str(nested.resolve()), result.checks[1].cwd)
            self.assertEqual("OAK:nested", started[1].qualified_cwd)

    def test_registered_context_rejects_programmatic_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:1]
            init_multi_repo_workspace(workspace_root, specs)
            plan_dir = root / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            context = verification.verification_context_from_plan(
                plan_dir, workspace_root
            )
            check = contracts.FileExistsCheck(
                name="escape", path="../outside", repo="SVC"
            )

            with self.assertRaisesRegex(plans_errors.PlanError, "stay beneath"):
                verification.run_check(check, context=context)

    def test_legacy_command_cwd_outside_the_bound_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bound = root / "repo"
            bound.mkdir()
            (root / "elsewhere").mkdir()
            context = verification.VerificationContext.legacy(bound)
            check = contracts.CommandCheck(
                name="escape",
                argv=(sys.executable, "-c", "pass"),
                timeout_seconds=10.0,
                cwd="../elsewhere",
            )

            with self.assertRaisesRegex(
                plans_errors.PlanError, "escapes the bound root"
            ):
                verification.run_check(check, context=context)

    def test_parallel_repository_results_stay_in_declared_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = root / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            checks = (
                contracts.CommandCheck(
                    name="slow-svc",
                    argv=(sys.executable, "-c", "import time; time.sleep(0.05)"),
                    timeout_seconds=10.0,
                    repo="SVC",
                ),
                contracts.CommandCheck(
                    name="fast-oak",
                    argv=(sys.executable, "-c", "pass"),
                    timeout_seconds=10.0,
                    repo="OAK",
                ),
            )
            contract = contracts.CompletionContract(
                session_id=plans_session_ids.parse_session_id("1"),
                done_when=("done",),
                verify=contracts.VerifyRecipe(checks=checks, parallel=True),
            )
            context = verification.verification_context_from_plan(
                plan_dir, workspace_root
            )

            result = verification.run_verification(contract, context=context)

            self.assertEqual(
                ["slow-svc", "fast-oak"], [check.name for check in result.checks]
            )
            self.assertEqual(
                [
                    str(repositories["SVC"].resolve()),
                    str(repositories["OAK"].resolve()),
                ],
                [check.resolved_root for check in result.checks],
            )
