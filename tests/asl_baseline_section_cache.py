# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import test_asl_baseline_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_baseline_shared import *
from .support_base import load_section_tests
from .asl_baseline_section_contract import *


class BaselineCacheTests(unittest.TestCase):
    def _command_recipe(
        self,
        name: str,
        argv: tuple[str, ...] = ("python3", "-m", "unittest", "tests.t"),
    ) -> contracts.VerifyRecipe:
        return contracts.VerifyRecipe(
            checks=(contracts.CommandCheck(name=name, argv=argv, timeout_seconds=30.0),)
        )

    def test_equivalent_recipes_normalize_and_key_identically(self) -> None:
        recipe_a = self._command_recipe("session-6-verify-1")
        recipe_b = self._command_recipe("session-7-verify-1")

        # Check names differ but the recipe semantics are identical.
        self.assertEqual(
            baseline_cache.normalize_recipe(recipe_a),
            baseline_cache.normalize_recipe(recipe_b),
        )
        key_a = baseline_cache.cache_key(
            recipe=recipe_a,
            cwd=".",
            repo_fingerprint="fp",
            executable="exe",
            environment="env",
        )
        key_b = baseline_cache.cache_key(
            recipe=recipe_b,
            cwd=".",
            repo_fingerprint="fp",
            executable="exe",
            environment="env",
        )
        self.assertEqual(key_a, key_b)

    def test_legacy_cache_keys_match_pre_repository_projection(self) -> None:
        checks_and_keys = (
            (
                contracts.CommandCheck(
                    name="unit",
                    argv=("python3", "-m", "pytest"),
                    cwd=None,
                    timeout_seconds=60,
                ),
                "4c8fc237db277db393f707fef15286459435024190c3bf798f3bd170b5d4d136",
            ),
            (
                contracts.FileExistsCheck(name="file", path="README.md"),
                "71c4ca53d6089aa7d63ca523f5955234ea39b3feb8dfe20384b3bc69149d8cd7",
            ),
            (
                contracts.RegexInFileCheck(
                    name="regex", path="README.md", pattern="^#"
                ),
                "f8c0078387ecae59c89f456c0a04d42d9ed121d775c6a372bd840f4ad6aec8b5",
            ),
        )

        for check, expected in checks_and_keys:
            with self.subTest(kind=check.kind):
                recipe = contracts.VerifyRecipe(checks=(check,))
                normalized = json.loads(baseline_cache.normalize_recipe(recipe))
                self.assertNotIn("repo", normalized["checks"][0])
                self.assertEqual(
                    expected,
                    baseline_cache.cache_key(
                        recipe=recipe,
                        cwd="/tmp/asl-legacy-cache-key",
                        repo_fingerprint="fp",
                        executable="exe",
                        environment="env",
                    ),
                )

    def test_cache_deduplicates_equivalent_recipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_git_repo(Path(tmp))
            cache = baseline_cache.BaselineCache(Path(tmp) / "baseline-results.json")
            key_a = cache.record(
                self._command_recipe("session-6-verify-1"), tmp, {"passed": True}
            )
            key_b = cache.record(
                self._command_recipe("session-7-verify-1"), tmp, {"passed": True}
            )
            self.assertEqual(key_a, key_b)
            self.assertEqual(1, len(cache))

    def test_baseline_expectation_participates_in_key(self) -> None:
        recipe_skip = contracts.VerifyRecipe(
            checks=(
                contracts.CommandCheck(
                    name="c",
                    argv=("true",),
                    timeout_seconds=5.0,
                    baseline_expectation="skip",
                ),
            )
        )
        recipe_pass = contracts.VerifyRecipe(
            checks=(
                contracts.CommandCheck(
                    name="c",
                    argv=("true",),
                    timeout_seconds=5.0,
                    baseline_expectation="pass",
                ),
            )
        )
        self.assertNotEqual(
            baseline_cache.normalize_recipe(recipe_skip),
            baseline_cache.normalize_recipe(recipe_pass),
        )

    def test_repository_alias_participates_in_normalized_recipe(self) -> None:
        def recipe(repo: str) -> contracts.VerifyRecipe:
            return contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c",
                        argv=("true",),
                        timeout_seconds=5.0,
                        repo=repo,
                    ),
                )
            )

        normalized = baseline_cache.normalize_recipe(recipe("SVC"))
        self.assertIn('"repo":"SVC"', normalized)
        self.assertNotEqual(normalized, baseline_cache.normalize_recipe(recipe("OAK")))

    def test_cwd_executable_and_environment_change_the_key(self) -> None:
        recipe = self._command_recipe("c")
        base = baseline_cache.cache_key(
            recipe=recipe,
            cwd="/tmp/a",
            repo_fingerprint="fp",
            executable="exe",
            environment="env",
        )
        self.assertNotEqual(
            base,
            baseline_cache.cache_key(
                recipe=recipe,
                cwd="/tmp/b",
                repo_fingerprint="fp",
                executable="exe",
                environment="env",
            ),
        )
        self.assertNotEqual(
            base,
            baseline_cache.cache_key(
                recipe=recipe,
                cwd="/tmp/a",
                repo_fingerprint="fp",
                executable="other-exe",
                environment="env",
            ),
        )
        self.assertNotEqual(
            base,
            baseline_cache.cache_key(
                recipe=recipe,
                cwd="/tmp/a",
                repo_fingerprint="fp",
                executable="exe",
                environment="other-env",
            ),
        )

    def test_executable_identity_resolves_program(self) -> None:
        resolved = baseline_cache.executable_identity(["python3", "-V"])
        self.assertTrue(resolved.endswith("python3") or resolved == "python3")
        self.assertEqual(
            "/no/such/interpreter",
            baseline_cache.executable_identity(["/no/such/interpreter"]),
        )
        self.assertEqual("", baseline_cache.executable_identity([]))

    def test_environment_signature_depends_on_selected_keys(self) -> None:
        sig_a = baseline_cache.environment_signature({"PATH": "/a"})
        sig_b = baseline_cache.environment_signature({"PATH": "/b"})
        self.assertNotEqual(sig_a, sig_b)
        # A key outside the selected set does not change the signature.
        sig_c = baseline_cache.environment_signature({"PATH": "/a", "UNSELECTED": "x"})
        self.assertEqual(sig_a, sig_c)

    def test_repository_fingerprint_invalidated_by_dirty_staged_untracked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            clean = baseline_cache.repository_fingerprint(repo)

            # Untracked file changes the fingerprint.
            (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
            untracked = baseline_cache.repository_fingerprint(repo)
            self.assertNotEqual(clean, untracked)

            # Modifying a tracked file (dirty) changes it further.
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")
            dirty = baseline_cache.repository_fingerprint(repo)
            self.assertNotEqual(untracked, dirty)

            # Staging the change changes it again.
            git_stdout(repo, "add", "README.md")
            staged = baseline_cache.repository_fingerprint(repo)
            self.assertNotEqual(dirty, staged)

            # Committing everything returns to a distinct clean fingerprint.
            git_stdout(repo, "add", "-A")
            git_stdout(repo, "commit", "-m", "second")
            committed = baseline_cache.repository_fingerprint(repo)
            self.assertNotEqual(staged, committed)
            self.assertNotEqual(clean, committed)

    def test_repository_fingerprint_ignores_a_plan_dir_inside_the_repo(
        self,
    ) -> None:
        # Writing baseline evidence into a plan kept in the repository must
        # not make that evidence stale the moment it lands.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            plan_dir = repo / "plans" / "p"
            plan_dir.mkdir(parents=True)
            before = baseline_cache.repository_fingerprint(repo, plan_dir)
            self.assertEqual(before, baseline_cache.repository_fingerprint(repo))

            (plan_dir / "baseline-results.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                before, baseline_cache.repository_fingerprint(repo, plan_dir)
            )
            self.assertNotEqual(before, baseline_cache.repository_fingerprint(repo))

            (repo / "elsewhere.txt").write_text("x\n", encoding="utf-8")
            self.assertNotEqual(
                before, baseline_cache.repository_fingerprint(repo, plan_dir)
            )

    def test_repository_fingerprint_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = baseline_cache.repository_fingerprint(tmp)
            self.assertTrue(fingerprint.startswith("non-git:"))
            # Stable across calls for the same directory.
            self.assertEqual(fingerprint, baseline_cache.repository_fingerprint(tmp))

    def test_repository_fingerprint_rejects_failed_git_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            real_git = baseline_cache._git

            def failed_status(root: Path, *args: str) -> str | None:
                if args[0] == "status":
                    return None
                return real_git(root, *args)

            with (
                mock.patch.object(baseline_cache, "_git", side_effect=failed_status),
                self.assertRaisesRegex(plans_errors.PlanError, "git status failed"),
            ):
                baseline_cache.repository_fingerprint(repo)

    def test_repository_fingerprint_rejects_failed_status_on_unborn_head(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git_stdout(repo, "init")
            real_git = baseline_cache._git

            def failed_status(root: Path, *args: str) -> str | None:
                if args[0] == "status":
                    return None
                return real_git(root, *args)

            with (
                mock.patch.object(baseline_cache, "_git", side_effect=failed_status),
                self.assertRaisesRegex(plans_errors.PlanError, "git status failed"),
            ):
                baseline_cache.repository_fingerprint(repo)

    def test_repository_fingerprint_rejects_unavailable_git_in_a_checkout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            nested = repo / "nested"
            nested.mkdir()

            with (
                mock.patch.object(baseline_cache, "_git", return_value=None),
                self.assertRaisesRegex(plans_errors.PlanError, "git status failed"),
            ):
                baseline_cache.repository_fingerprint(nested)

    def test_cache_roundtrip_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            path = repo / baseline_cache.BASELINE_RESULTS_FILENAME
            cache = baseline_cache.BaselineCache(path)
            key = cache.record(
                self._command_recipe("c"), repo, {"passed": True, "checks": []}
            )
            cache.save()

            reloaded = baseline_cache.BaselineCache.load(path)
            record = reloaded.get(key)
            self.assertIsNotNone(record)
            self.assertEqual({"passed": True, "checks": []}, record.result)
            self.assertEqual(
                baseline_cache.normalize_recipe(self._command_recipe("c")),
                record.recipe,
            )

    def test_cache_load_failcloses_on_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / baseline_cache.BASELINE_RESULTS_FILENAME
            path.write_text(
                json.dumps(
                    {
                        "version": baseline_cache.BASELINE_CACHE_VERSION + 1,
                        "entries": {"k": {"key": "k", "recipe": "r", "result": {}}},
                    }
                ),
                encoding="utf-8",
            )
            cache = baseline_cache.BaselineCache.load(path)
            self.assertEqual(0, len(cache))

    def test_cache_load_skips_untrustworthy_files_and_entries(self) -> None:
        version = baseline_cache.BASELINE_CACHE_VERSION
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / baseline_cache.BASELINE_RESULTS_FILENAME
            path.write_text(
                json.dumps(
                    {
                        "version": version,
                        "entries": {
                            "null": {"key": "null", "result": None},
                            "text": {"key": "text", "result": "oops"},
                            "ok": {"key": "ok", "result": {"passed": True}},
                        },
                    }
                ),
                encoding="utf-8",
            )
            cache = baseline_cache.BaselineCache.load(path)
            self.assertEqual(1, len(cache))
            self.assertIsNotNone(cache.get("ok"))

            for raw in (
                b'{"version": %d, "entries": {}}\xff' % version,
                b"[" * 100000,
            ):
                path.write_bytes(raw)
                self.assertEqual(0, len(baseline_cache.BaselineCache.load(path)))

    def test_cache_load_rejects_non_integer_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / baseline_cache.BASELINE_RESULTS_FILENAME
            for version in (True, 1.0):
                with self.subTest(version=version):
                    path.write_text(
                        json.dumps(
                            {
                                "version": version,
                                "entries": {
                                    "k": {"key": "k", "recipe": "r", "result": {}}
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    self.assertEqual(0, len(baseline_cache.BaselineCache.load(path)))

    def test_baseline_results_excluded_from_content_and_readiness_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (plan_dir / "session-1.md").write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "Do work.",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            content_before = plans_content_hash.plan_content_sha256(plan_dir)
            readiness_before = plans_content_hash.stable_readiness_sha256(plan_dir)

            cache = baseline_cache.BaselineCache(
                plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
            )
            cache.record(self._command_recipe("c"), plan_dir, {"passed": True})
            cache.save()
            self.assertTrue(
                (plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME).exists()
            )

            self.assertEqual(
                content_before, plans_content_hash.plan_content_sha256(plan_dir)
            )
            self.assertEqual(
                readiness_before, plans_content_hash.stable_readiness_sha256(plan_dir)
            )


class BaselineTotalBudgetTests(unittest.TestCase):
    """The total baseline budget is a fail-closed gate, not advisory (M2)."""

    def _pass_result(self, check: contracts.VerificationCheck, duration: float):
        return verification_models.CheckResult(
            name=check.name,
            kind="command",
            status="pass",
            detail="command exited 0",
            returncode=0,
            duration_seconds=duration,
        )

    def test_command_timeout_clamped_to_remaining_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending", "pending"),
                contracts_by_id={
                    "1": _command_contract('["true"]', session="1"),
                    "2": _command_contract('["echo", "x"]', session="2", name="cmd2"),
                },
            )
            seen_timeouts: list[float] = []

            def fake_run_check(check, *, repo_root):
                seen_timeouts.append(check.timeout_seconds)
                return self._pass_result(check, 4.0)

            with mock.patch.object(baseline_run, "run_check", fake_run_check):
                result = baseline.run_baseline(plan_dir, repo_root, total_timeout=10.0)

            # First check gets the full remaining budget (10s), the second
            # only what the first left over (10 - 4 = 6s), not its own 120s
            # per-check budget.
            self.assertEqual([10.0, 6.0], seen_timeouts)
            self.assertFalse(result.total_exceeded)
            self.assertTrue(result.all_satisfied)

    def test_a_check_two_sessions_share_is_budgeted_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending", "pending"),
                contracts_by_id={
                    "1": _command_contract('["true"]', session="1"),
                    "2": _command_contract('["true"]', session="2"),
                },
            )
            seen_timeouts: list[float] = []

            def fake_run_check(check, *, repo_root):
                seen_timeouts.append(check.timeout_seconds)
                return self._pass_result(check, 1.0)

            with mock.patch.object(baseline_run, "run_check", fake_run_check):
                result = baseline.run_baseline(plan_dir, repo_root)

            # One execution, so the total is that one check's budget, not two.
            self.assertEqual(1, len(seen_timeouts))
            self.assertEqual(seen_timeouts[0], result.total_budget_seconds)

    def test_success_after_total_budget_overrun_is_unsatisfied(self) -> None:
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

            def fake_run_check(check, *, repo_root):
                return self._pass_result(check, 6.0)

            with mock.patch.object(baseline_run, "run_check", fake_run_check):
                result = baseline.run_baseline(plan_dir, repo_root, total_timeout=5.0)

            outcome = result.outcomes[0]
            self.assertEqual("pass", outcome.status)
            self.assertFalse(outcome.satisfied)
            self.assertTrue(outcome.total_budget_exceeded)
            self.assertIn("total baseline budget", outcome.detail)
            self.assertTrue(result.total_exceeded)
            self.assertFalse(result.all_satisfied)
            cache = baseline_cache.BaselineCache.load(result.results_path)
            record = cache.get(outcome.key)
            assert record is not None
            self.assertFalse(record.result["satisfied"])
            self.assertTrue(record.result["total_budget_exceeded"])

    def test_skipped_check_after_budget_exhaustion_marks_overrun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("pending", "pending"),
                contracts_by_id={
                    "1": _command_contract('["true"]', session="1"),
                    "2": _command_contract('["echo", "x"]', session="2", name="cmd2"),
                },
            )

            def fake_run_check(check, *, repo_root):
                return self._pass_result(check, 5.0)

            with mock.patch.object(baseline_run, "run_check", fake_run_check):
                result = baseline.run_baseline(plan_dir, repo_root, total_timeout=5.0)

            skipped = result.outcomes[1]
            self.assertEqual("skip", skipped.status)
            self.assertFalse(skipped.satisfied)
            self.assertTrue(skipped.total_budget_exceeded)

    def test_session_ids_restrict_the_run_to_those_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(
                plan_dir,
                ("complete", "pending"),
                contracts_by_id={
                    "1": _command_contract('["true"]', session="1"),
                    "2": _command_contract('["echo", "x"]', session="2", name="cmd2"),
                },
            )
            seen: list[str] = []

            def fake_run_check(check, *, repo_root):
                seen.append(check.name)
                return self._pass_result(check, 1.0)

            with mock.patch.object(baseline_run, "run_check", fake_run_check):
                result = baseline.run_baseline(
                    plan_dir, repo_root, session_ids=frozenset({"2"})
                )

            # ASI-6: the completed session's check is never re-judged.
            self.assertEqual(["cmd2"], seen)
            self.assertEqual(["2"], [o.session_id for o in result.outcomes])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
