from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_workbench_archive_support import *


class WorkbenchDiscoveryTests(unittest.TestCase):
    def test_discovers_plans_from_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            plan_state_dir = state_root / plans_files.path_sha256(plan_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / "state.json").write_text(
                json.dumps({"plan_dir": str(plan_dir)}, sort_keys=True),
                encoding="utf-8",
            )

            with patched_env(AGENT_PLANS_DIR=None):
                result = workbench_discovery.discover_plans(
                    state_root=state_root,
                    include_default_dirs=False,
                )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(plan_dir, discovered.plan_dir)
            self.assertEqual(("state",), discovered.sources)
            self.assertTrue(discovered.exists)
            self.assertIsNone(discovered.label)
            self.assertIsNone(discovered.profile)
            self.assertEqual((), result.warnings)

    def test_discovers_plans_from_registry_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            missing_dir = tmp_path / "plans" / "ghost"
            registry = workbench_registry_models.WorkbenchRegistry(
                pins=(
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(plan_dir),
                        label="Alpha",
                        profile=workbench_registry_models.LaunchProfile(
                            harness="claude"
                        ),
                    ),
                    workbench_registry_models.RegistryPin(plan_dir=str(missing_dir)),
                ),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual(2, len(result.plans))
            by_path = {plan.plan_dir: plan for plan in result.plans}
            alpha = by_path[plan_dir]
            self.assertEqual(("registry",), alpha.sources)
            self.assertEqual("Alpha", alpha.label)
            self.assertEqual("claude", alpha.profile.harness)
            self.assertTrue(alpha.exists)
            ghost = by_path[missing_dir]
            self.assertFalse(ghost.exists)
            self.assertEqual(("registry",), ghost.sources)

    def test_discovers_plans_from_explicit_plan_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "plans"
            plan_root.mkdir()
            real_plan = _write_plan_dir(plan_root / "alpha")
            (plan_root / "bare-file").write_text("ignored", encoding="utf-8")
            (plan_root / "incomplete").mkdir()
            (plan_root / "incomplete" / "manifest.md").write_text(
                "no context", encoding="utf-8"
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                plan_roots=[plan_root],
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            self.assertEqual(real_plan, result.plans[0].plan_dir)
            self.assertEqual(("plan-root",), result.plans[0].sources)

    def test_discovers_hash_only_plan_directory_names(self) -> None:
        # Regression: hash-only split-plan output directories (named
        # <content-hash-prefix>-<source-fingerprint-prefix>) must be discovered
        # exactly like slug-prefixed ones. Discovery keys on manifest.md +
        # context.md, not on the directory basename format.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "plans"
            plan_root.mkdir()
            hash_only_plan = _write_plan_dir(plan_root / "9e3ab88dedcf-7d2caa1117db")
            agent_dir = tmp_path / "agent-plans"
            agent_dir.mkdir()
            default_hash_only = _write_plan_dir(agent_dir / "9e3ab88dedcf-7d2caa1117db")

            with patched_env(
                AGENT_PLANS_DIR=str(agent_dir),
                HOME=str(tmp_path / "home-overridden"),
            ):
                result = workbench_discovery.discover_plans(
                    state_root=tmp_path / "state-empty",
                    plan_roots=[plan_root],
                    include_default_dirs=True,
                )

            paths_to_sources = {plan.plan_dir: plan.sources for plan in result.plans}
            self.assertIn(hash_only_plan, paths_to_sources)
            self.assertEqual(("plan-root",), paths_to_sources[hash_only_plan])
            self.assertIn(default_hash_only, paths_to_sources)
            self.assertEqual(("agent-plans-dir",), paths_to_sources[default_hash_only])

    def test_discovers_plans_from_environment_default_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            agent_dir = tmp_path / "agent-plans"
            agent_dir.mkdir()
            plan_a = _write_plan_dir(agent_dir / "alpha")
            tmp_default = tmp_path / "tmp-root"
            tmp_default.mkdir()
            (tmp_default / "agent-plans").mkdir()
            plan_b = _write_plan_dir(tmp_default / "agent-plans" / "beta")
            share_default = tmp_path / "share-root"
            share_default.mkdir()
            (share_default / "agent-plans").mkdir()
            plan_c = _write_plan_dir(share_default / "agent-plans" / "gamma")

            with patched_env(
                AGENT_PLANS_DIR=str(agent_dir),
                TMPDIR=str(tmp_default),
                XDG_DATA_HOME=str(share_default),
                HOME=str(tmp_path / "home-overridden"),
            ):
                result = workbench_discovery.discover_plans(
                    state_root=tmp_path / "state-empty",
                    include_default_dirs=True,
                )

            paths_to_sources = {plan.plan_dir: plan.sources for plan in result.plans}
            self.assertIn(plan_a, paths_to_sources)
            self.assertIn(plan_b, paths_to_sources)
            self.assertIn(plan_c, paths_to_sources)
            self.assertEqual(("agent-plans-dir",), paths_to_sources[plan_a])
            self.assertEqual(("tmp-default",), paths_to_sources[plan_b])
            self.assertEqual(("share-default",), paths_to_sources[plan_c])

    def test_dedupes_across_sources_and_records_each_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            plan_state_dir = state_root / plans_files.path_sha256(plan_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / "state.json").write_text(
                json.dumps({"plan_dir": str(plan_dir)}, sort_keys=True),
                encoding="utf-8",
            )
            registry = workbench_registry_models.WorkbenchRegistry(
                pins=(
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(plan_dir),
                        label="Alpha",
                        profile=workbench_registry_models.LaunchProfile(
                            harness="codex"
                        ),
                    ),
                ),
            )

            result = workbench_discovery.discover_plans(
                state_root=state_root,
                registry=registry,
                plan_roots=[tmp_path / "plans"],
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(plan_dir, discovered.plan_dir)
            self.assertEqual(("state", "registry", "plan-root"), discovered.sources)
            self.assertEqual("Alpha", discovered.label)
            self.assertEqual("codex", discovered.profile.harness)

    def test_discovers_plans_from_registry_plan_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "registry-roots"
            plan_root.mkdir()
            real_plan = _write_plan_dir(plan_root / "alpha")
            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(plan_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(real_plan, discovered.plan_dir)
            self.assertEqual(("plan-root",), discovered.sources)
            self.assertTrue(discovered.exists)

    def test_dedupes_registry_and_cli_plan_roots_into_single_plan_root_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            registry_root = tmp_path / "registry-roots"
            registry_root.mkdir()
            cli_root = tmp_path / "cli-roots"
            cli_root.mkdir()
            shared_plan = _write_plan_dir(registry_root / "alpha")
            cli_only_plan = _write_plan_dir(cli_root / "beta")
            # Symlink the same plan into both roots so a single plan is
            # surfaced by both registry roots and CLI roots.
            (cli_root / "alpha").symlink_to(shared_plan)

            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(registry_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                plan_roots=[cli_root],
                include_default_dirs=False,
            )

            by_path = {plan.plan_dir: plan for plan in result.plans}
            self.assertIn(shared_plan, by_path)
            self.assertIn(cli_only_plan, by_path)
            self.assertEqual(("plan-root",), by_path[shared_plan].sources)
            self.assertEqual(("plan-root",), by_path[cli_only_plan].sources)
            # Order: registry roots scanned first, so shared_plan precedes
            # the CLI-only plan in the discovery list.
            ordered = [plan.plan_dir for plan in result.plans]
            self.assertEqual([shared_plan, cli_only_plan], ordered)

    def test_registry_pin_and_registry_root_merge_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "registry-roots"
            plan_root.mkdir()
            plan_dir = _write_plan_dir(plan_root / "alpha")
            registry = workbench_registry_models.WorkbenchRegistry(
                pins=(
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(plan_dir),
                        label="Alpha",
                        profile=workbench_registry_models.LaunchProfile(
                            harness="codex"
                        ),
                    ),
                ),
                plan_roots=(str(plan_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(plan_dir, discovered.plan_dir)
            self.assertEqual(("registry", "plan-root"), discovered.sources)
            self.assertEqual("Alpha", discovered.label)
            self.assertEqual("codex", discovered.profile.harness)

    def test_missing_directories_and_invalid_inputs_become_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            not_a_dir = tmp_path / "not-a-dir"
            not_a_dir.write_text("file not directory", encoding="utf-8")

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                plan_roots=[tmp_path / "missing", not_a_dir],
                include_default_dirs=False,
            )

            self.assertEqual((), result.plans)
            joined = "\n".join(result.warnings)
            self.assertIn("is not a directory", joined)

    def test_registry_plan_root_missing_emits_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            missing_root = tmp_path / "missing-registry-root"
            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(missing_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual((), result.plans)
            self.assertEqual((), result.warnings)

    def test_registry_plan_root_warns_when_not_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            not_a_dir = tmp_path / "registry-root-file"
            not_a_dir.write_text("file not directory", encoding="utf-8")
            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(not_a_dir),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual((), result.plans)
            joined = "\n".join(result.warnings)
            self.assertIn("registry_plan_root[0]", joined)
            self.assertIn("is not a directory", joined)
