# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import test_asl_baseline_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_baseline_shared import *
from .asl_baseline_section_1 import *
from .asl_baseline_section_2 import *


class MultiRepositoryBaselineTests(unittest.TestCase):
    def test_non_git_registered_repository_is_unavailable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:1]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = root / "plan"
            contract = _contract_block(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": "README.md",
                        "name": "readme",
                        "repo": "SVC",
                        "baseline_expectation": "pass",
                    }
                )
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract},
                specs=specs,
            )
            context = verification_models.verification_context_from_plan(
                plan_dir, workspace_root
            )
            shutil.rmtree(repositories["SVC"] / ".git")

            _digest, available = baseline._registered_repository_fingerprint(
                context, plan_dir
            )
            run = baseline.run_baseline(plan_dir, workspace_root, context=context)

            self.assertFalse(available)
            self.assertFalse(run.all_satisfied)
            self.assertEqual("pass", run.outcomes[0].status)
            self.assertFalse(run.outcomes[0].satisfied)
            self.assertFalse(run.outcomes[0].repository_evidence_available)

    def test_resolves_all_checks_before_executing_baseline_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:1]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            outside = root / "outside"
            outside.mkdir()
            (outside / "file.txt").write_text("outside\n", encoding="utf-8")
            os.symlink(outside, repositories["SVC"] / "link")
            marker = root / "marker.txt"
            plan_dir = root / "plan"
            command = _contract_block(
                json.dumps(
                    {
                        "type": "command",
                        "argv": [
                            sys.executable,
                            "-c",
                            f"open({str(marker)!r}, 'a').write('x')",
                        ],
                        "name": "side-effect",
                        "repo": "SVC",
                        "baseline_expectation": "pass",
                    }
                ),
                session="1",
            )
            escaping = _contract_block(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": "link/file.txt",
                        "name": "escapes",
                        "repo": "SVC",
                        "baseline_expectation": "pass",
                    }
                ),
                session="2",
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending", "pending"),
                contracts_by_id={"1": command, "2": escaping},
                specs=specs,
            )

            for _attempt in range(2):
                with self.assertRaisesRegex(
                    plans_errors.PlanError, "escapes repository"
                ):
                    baseline.run_baseline(plan_dir, workspace_root)

            self.assertFalse(marker.exists())
            results_path = plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
            self.assertTrue(results_path.is_file())
            self.assertEqual(0, len(baseline_cache.BaselineCache.load(results_path)))

    def test_aggregate_refuses_unavailable_repository_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:1]
            init_multi_repo_workspace(workspace_root, specs)
            plan_dir = root / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            context = verification_models.verification_context_from_plan(
                plan_dir, workspace_root
            )

            with mock.patch.object(
                baseline_evidence,
                "_registered_repository_fingerprint",
                return_value=("same-digest", True),
            ):
                self.assertEqual(
                    "same-digest",
                    baseline.aggregate_repository_fingerprint(context, plan_dir),
                )
            with (
                mock.patch.object(
                    baseline_evidence,
                    "_registered_repository_fingerprint",
                    return_value=("same-digest", False),
                ),
                self.assertRaisesRegex(plans_errors.PlanError, "unavailable"),
            ):
                baseline.aggregate_repository_fingerprint(context, plan_dir)

    def test_records_registry_aggregate_and_invalidates_on_other_repo_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            (repositories["SVC"] / "baseline.txt").write_text(
                "present\n", encoding="utf-8"
            )
            plan_dir = root / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "path": "baseline.txt", '
                '"name": "baseline", "repo": "SVC", '
                '"baseline_expectation": "pass"}'
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract},
                specs=specs,
            )

            run = baseline.run_baseline(plan_dir, workspace_root)

            self.assertTrue(run.all_satisfied)
            outcome = run.outcomes[0]
            self.assertEqual("SVC", outcome.repo)
            self.assertEqual("SVC:baseline.txt", outcome.qualified_path)
            self.assertEqual(run.registry_digest, outcome.registry_digest)
            self.assertEqual(
                run.aggregate_repository_fingerprint,
                outcome.aggregate_repository_fingerprint,
            )
            cache = baseline_cache.BaselineCache.load(run.results_path)
            record = cache.get(outcome.key)
            assert record is not None
            context = verification_models.verification_context_from_plan(
                plan_dir, workspace_root
            )
            self.assertTrue(
                baseline.baseline_evidence_is_current(record.result, context, plan_dir)
            )

            (repositories["OAK"] / "untracked.txt").write_text(
                "changed\n", encoding="utf-8"
            )

            self.assertFalse(
                baseline.baseline_evidence_is_current(record.result, context, plan_dir)
            )


class BaselineCliTests(unittest.TestCase):
    def _run_baseline(
        self,
        plan_dir: Path,
        cwd: Path,
        *,
        json_output: bool = True,
        extra: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "baseline",
            str(plan_dir),
            "--cwd",
            str(cwd),
            *extra,
        ]
        if json_output:
            argv.append("--json")
        return subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_baseline_records_pass_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _command_contract('["true"]', expectation="pass")
                },
            )

            completed = self._run_baseline(plan_dir, repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual(1, payload["check_count"])
            self.assertTrue(payload["all_satisfied"])
            self.assertEqual(1, payload["satisfied_count"])
            check = payload["checks"][0]
            self.assertEqual("pass", check["expectation"])
            self.assertEqual("pass", check["status"])
            self.assertTrue(check["satisfied"])
            results_path = plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
            self.assertTrue(results_path.exists())
            cache = baseline_cache.BaselineCache.load(results_path)
            self.assertEqual(1, len(cache))

    def test_baseline_flags_unexpected_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _command_contract('["false"]', expectation="pass")
                },
            )

            completed = self._run_baseline(plan_dir, repo_root)

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("baseline-unsatisfied", payload["reason"])
            self.assertFalse(payload["all_satisfied"])
            check = payload["checks"][0]
            self.assertEqual("fail", check["status"])
            self.assertFalse(check["satisfied"])

    def test_baseline_expected_fail_is_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _command_contract('["false"]', expectation="fail")
                },
            )

            completed = self._run_baseline(plan_dir, repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual("ok", payload["reason"])
            check = payload["checks"][0]
            self.assertEqual("fail", check["expectation"])
            self.assertEqual("fail", check["status"])
            self.assertTrue(check["satisfied"])

    def test_baseline_with_only_skip_checks_reports_no_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("ok\n", encoding="utf-8")
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _contract_block(
                        '{"type": "file_exists", "path": "target.txt", '
                        '"name": "target"}'
                    )
                },
            )

            completed = self._run_baseline(plan_dir, repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("no-baseline-checks", payload["reason"])
            self.assertEqual(0, payload["check_count"])
            # No evidence needed, but the store is still written (empty).
            self.assertTrue(
                (plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME).exists()
            )

    def test_baseline_deduplicates_equivalent_checks_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending", "pending"),
                contracts_by_id={
                    "1": _command_contract(
                        '["true"]', session="1", name="shared", expectation="pass"
                    ),
                    "2": _command_contract(
                        '["true"]', session="2", name="shared", expectation="pass"
                    ),
                },
            )

            completed = self._run_baseline(plan_dir, repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            # Two sessions, one equivalent recipe -> a single deduplicated entry.
            self.assertEqual(1, payload["check_count"])
            cache = baseline_cache.BaselineCache.load(
                plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
            )
            self.assertEqual(1, len(cache))

    def test_baseline_text_output_lists_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _command_contract('["true"]', expectation="pass")
                },
            )

            completed = self._run_baseline(plan_dir, repo_root, json_output=False)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Baseline checks: 1", completed.stdout)
            self.assertIn("[OK]", completed.stdout)

    def test_baseline_unrunnable_command_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _command_contract(
                        '["agent-session-loop-not-a-real-binary-xyz"]',
                        expectation="pass",
                    )
                },
            )

            completed = self._run_baseline(plan_dir, repo_root)

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("baseline-unsatisfied", payload["reason"])
            check = payload["checks"][0]
            self.assertEqual("error", check["status"])
            self.assertFalse(check["satisfied"])

    def test_baseline_total_budget_clamps_real_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _command_contract('["sleep", "5"]', expectation="pass")
                },
            )

            completed = self._run_baseline(
                plan_dir, repo_root, extra=("--total-timeout", "0.5")
            )

            # Without the clamp the sleep would run its full 5 seconds,
            # exit 0, and count as satisfied despite blowing the total budget.
            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("baseline-unsatisfied", payload["reason"])
            check = payload["checks"][0]
            self.assertEqual("timeout", check["status"])
            self.assertFalse(check["satisfied"])
            self.assertEqual(0.5, check["budget_seconds"])


if __name__ == "__main__":
    unittest.main()


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    suite = unittest.TestSuite()
    cases = sorted(
        (
            value
            for value in globals().values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ),
        key=lambda case: case.__name__,
    )
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite
