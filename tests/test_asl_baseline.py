"""Completion-contract baseline schema, cache, and CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    baseline,
    baseline_cache,
    contracts,
    plans_content_hash,
    plans_errors,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _baseline_contract_text,
    _command_contract,
    _contract_block,
    git_stdout,
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)


class BaselineContractTests(unittest.TestCase):
    def test_baseline_expectation_defaults_to_skip_for_all_check_types(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {"type": "command", "argv": ["python3", "-V"]},
                        {"type": "file_exists", "path": "a.py"},
                        {
                            "type": "regex_in_file",
                            "path": "a.py",
                            "pattern": "^def",
                        },
                    ]
                }
            )
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        self.assertIsNotNone(contract)
        for check in contract.verify.checks:
            self.assertEqual("skip", check.baseline_expectation)

    def test_baseline_expectation_parsed_when_present(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "type": "command",
                            "argv": ["python3", "-V"],
                            "baseline_expectation": "pass",
                        },
                        {
                            "type": "file_exists",
                            "path": "a.py",
                            "baseline_expectation": "fail",
                        },
                        {
                            "type": "regex_in_file",
                            "path": "a.py",
                            "pattern": "^def",
                            "baseline_expectation": "skip",
                        },
                    ]
                }
            )
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        command_check, file_check, regex_check = contract.verify.checks
        self.assertEqual("pass", command_check.baseline_expectation)
        self.assertEqual("fail", file_check.baseline_expectation)
        self.assertEqual("skip", regex_check.baseline_expectation)

    def test_invalid_baseline_expectation_raises(self) -> None:
        for bad in ("maybe", "", True, 1):
            with self.subTest(value=bad):
                text = _baseline_contract_text(
                    json.dumps(
                        {
                            "checks": [
                                {
                                    "type": "command",
                                    "argv": ["python3", "-V"],
                                    "baseline_expectation": bad,
                                }
                            ]
                        }
                    )
                )
                with self.assertRaisesRegex(
                    plans_errors.PlanError, "baseline_expectation"
                ):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_legacy_contract_without_field_parses_unchanged(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "name": "unit",
                            "type": "command",
                            "argv": ["python3", "-m", "unittest"],
                            "timeout_seconds": 30,
                        }
                    ]
                }
            )
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        check = contract.verify.checks[0]
        self.assertEqual("unit", check.name)
        self.assertEqual(("python3", "-m", "unittest"), check.argv)
        self.assertEqual(30.0, check.timeout_seconds)
        self.assertEqual("skip", check.baseline_expectation)

    def test_legacy_contract_rejects_repository_alias(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": "README.md",
                            "repo": "SVC",
                        }
                    ]
                }
            )
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "repository registry"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_baseline_budget_constants(self) -> None:
        self.assertEqual(120.0, contracts.DEFAULT_BASELINE_CHECK_BUDGET_SECONDS)
        self.assertEqual(600.0, contracts.DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS)

    def test_baseline_budget_clamps_per_check_and_total(self) -> None:
        long_check = contracts.CommandCheck(
            name="slow",
            argv=("sleep", "1000"),
            timeout_seconds=900.0,
        )
        self.assertEqual(120.0, contracts.baseline_check_budget_seconds(long_check))

        short_check = contracts.CommandCheck(
            name="fast",
            argv=("true",),
            timeout_seconds=10.0,
        )
        self.assertEqual(10.0, contracts.baseline_check_budget_seconds(short_check))

        many = contracts.VerifyRecipe(checks=tuple(long_check for _ in range(10)))
        # 10 checks * 120s clamped-per-check = 1200s, clamped to the 600s total.
        self.assertEqual(600.0, contracts.recipe_total_budget_seconds(many))

        few = contracts.VerifyRecipe(checks=(short_check, short_check))
        self.assertEqual(20.0, contracts.recipe_total_budget_seconds(few))


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

    def test_repository_fingerprint_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = baseline_cache.repository_fingerprint(tmp)
            self.assertTrue(fingerprint.startswith("non-git:"))
            # Stable across calls for the same directory.
            self.assertEqual(fingerprint, baseline_cache.repository_fingerprint(tmp))

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
        return verification.CheckResult(
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

            with mock.patch.object(baseline, "run_check", fake_run_check):
                result = baseline.run_baseline(plan_dir, repo_root, total_timeout=10.0)

            # First check gets the full remaining budget (10s), the second
            # only what the first left over (10 - 4 = 6s), not its own 120s
            # per-check budget.
            self.assertEqual([10.0, 6.0], seen_timeouts)
            self.assertFalse(result.total_exceeded)
            self.assertTrue(result.all_satisfied)

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

            with mock.patch.object(baseline, "run_check", fake_run_check):
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

            with mock.patch.object(baseline, "run_check", fake_run_check):
                result = baseline.run_baseline(plan_dir, repo_root, total_timeout=5.0)

            skipped = result.outcomes[1]
            self.assertEqual("skip", skipped.status)
            self.assertFalse(skipped.satisfied)
            self.assertTrue(skipped.total_budget_exceeded)


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
            context = verification.verification_context_from_plan(
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
            context = verification.verification_context_from_plan(
                plan_dir, workspace_root
            )

            with mock.patch.object(
                baseline,
                "_registered_repository_fingerprint",
                return_value=("same-digest", True),
            ):
                self.assertEqual(
                    "same-digest",
                    baseline.aggregate_repository_fingerprint(context, plan_dir),
                )
            with (
                mock.patch.object(
                    baseline,
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
            context = verification.verification_context_from_plan(
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
