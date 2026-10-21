from .asl_run_inputs_support import *
from .asl_run_inputs_support import _contract_block


class RepositoryIntentMessageTests(unittest.TestCase):
    """What the operator is told, and what they are told to do next."""

    report: dict[str, Any] = {
        "git_root_count": 2,
        "git_roots": ["/repo/one", "/repo/two"],
    }

    def test_without_a_migration_the_whole_repair_is_asked_for_by_hand(self) -> None:
        message = run_inputs._repository_intent_message(self.report)

        self.assertIn("run reaches 2 git repositories", message)
        self.assertIn("/repo/one, /repo/two", message)
        self.assertIn("declare the registry", message)
        self.assertIn("prepare the workspace", message)

    def test_an_applied_migration_leaves_only_the_commands_to_run(self) -> None:
        result = repository_registration.RegistrationResult(
            status="applied",
            files=(Path("/repo/one/manifest.md"),),
            next_commands=("asl prepare", "asl run"),
        )

        message = run_inputs._repository_intent_message(
            self.report, subject="task packet", registration=result
        )

        self.assertIn("task packet reaches 2 git repositories", message)
        self.assertIn("1 file(s) rewritten", message)
        self.assertIn("nothing is left to hand-edit", message)
        self.assertIn("asl prepare; then: asl run", message)

    def test_a_refused_migration_names_the_fact_it_could_not_derive(self) -> None:
        result = repository_registration.RegistrationResult(
            status="refused",
            missing_fact="workspace-root",
            detail="two checkouts share no common parent",
        )

        message = run_inputs._repository_intent_message(
            self.report, registration=result
        )

        self.assertIn("could not be derived automatically", message)
        self.assertIn("workspace-root: two checkouts share no common parent", message)
        self.assertIn("supply that fact", message)


class TaskPacketIntentTests(unittest.TestCase):
    """The packet is judged on what it actually hands the worker."""

    def test_every_artifact_the_packet_points_at_is_listed(self) -> None:
        plan_dir = Path("/plans/demo")

        paths = run_inputs._task_packet_artifact_paths(plan_dir, "3", "2")

        self.assertEqual(
            (
                plan_dir / "session-3.md",
                plan_dir / "session-3.expand.md",
                plan_dir / "context.md",
                plan_dir / "progress.md",
                plan_dir / "session-2.md",
            ),
            paths,
        )

    def test_a_first_session_lists_no_predecessor_handoff(self) -> None:
        paths = run_inputs._task_packet_artifact_paths(Path("/plans/demo"), "1", None)

        self.assertEqual(4, len(paths))

    def test_a_packet_that_stays_inside_the_root_needs_no_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / ".git").mkdir(parents=True)
            plan_dir = root / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")

            report = run_inputs._task_packet_repository_intent_report(
                f"Work inside `{root / 'src'}`.\n",
                root=root,
                artifact_paths=(plan_dir / "context.md",),
                exempt_roots=(plan_dir,),
            )

        self.assertIsNone(report)

    def test_a_second_checkout_named_only_in_an_artifact_is_still_caught(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / ".git").mkdir(parents=True)
            other = Path(tmp) / "other"
            (other / ".git").mkdir(parents=True)
            plan_dir = root / "plan"
            plan_dir.mkdir()
            # The run-level scan never read this file's body; the packet does.
            expansion = plan_dir / "session-1.expand.md"
            expansion.write_text(
                f"Also patch `{other / 'lib.py'}`.\n", encoding="utf-8"
            )

            report = run_inputs._task_packet_repository_intent_report(
                "Implement session 1.\n",
                root=root,
                artifact_paths=(expansion,),
                exempt_roots=(plan_dir,),
            )

        assert report is not None
        self.assertTrue(report["registry_required"])
        self.assertEqual(2, report["git_root_count"])
        self.assertIn(str(plans_files.canonicalize_path(other)), report["git_roots"])

    def test_provenance_roots_are_exempt_from_the_packet_scan_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / ".git").mkdir(parents=True)
            source = Path(tmp) / "source"
            (source / ".git").mkdir(parents=True)
            plan_dir = root / "plan"
            plan_dir.mkdir()

            report = run_inputs._task_packet_repository_intent_report(
                f"Split from `{source / 'plan.md'}`.\n",
                root=root,
                artifact_paths=(),
                exempt_roots=(plan_dir,),
                provenance_roots=(source,),
            )

        self.assertIsNone(report)


class PlanStoreStateRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.plan = self.root / "plan"
        self.run = self.root / "run"
        self.store = self.root / "external store"
        self.effective = self.root / "state"

    def report(
        self, requirement: str, *, override: str | None = None
    ) -> dict[str, Any] | None:
        block = _contract_block(
            '{"type":"file_exists","path":"README.md","name":"readme"}'
        )
        write_plan(
            self.plan,
            ("pending",),
            contracts_by_id={"1": block.replace("recipe runs", requirement)},
        )
        return run_inputs._session_plan_store_state_root_report(
            self.plan, "1", state_dir_arg=override, run_dir=self.run
        )

    def test_required_store_outside_effective_roots_is_reported(self) -> None:
        report = self.report(
            f'All plan-store writes used --state-dir "{self.store}"',
            override=str(self.effective),
        )
        assert report is not None
        self.assertEqual([str(self.store)], report["required_state_roots"])
        self.assertEqual(str(self.effective), report["effective_state_root"])
        self.assertEqual("session-1.md:Completion Contract/Done when", report["source"])
        self.assertEqual(
            ("pending",),
            tuple(s.status for s in plans_snapshot.inspect_plan(self.plan).sessions),
        )

    def test_matching_or_covering_state_root_needs_no_pause(self) -> None:
        for override in (self.store, self.root):
            with self.subTest(override=override):
                self.assertIsNone(
                    self.report(
                        f'All cross-plan-store writes used --state-dir "{self.store}"',
                        override=str(override),
                    )
                )

    def test_same_plan_store_writes_are_already_control_plane(self) -> None:
        self.assertIsNone(
            self.report(
                f"All plan-store writes used --state-dir `{self.plan}`",
                override=str(self.effective),
            )
        )

    def test_home_relative_default_root_is_resolved_without_an_override(self) -> None:
        with mock.patch.dict("os.environ", {"HOME": str(self.root)}):
            report = self.report(
                "All plan-store writes used --state-dir ~/.local/share/agent-plans"
            )
            assert report is not None
            self.assertEqual(
                [str(self.root / ".local/share/agent-plans")],
                report["required_state_roots"],
            )
            self.assertEqual(
                str(state.state_root(None)), report["effective_state_root"]
            )

    def test_equal_flag_and_duplicate_roots_are_supported(self) -> None:
        report = self.report(
            f"Plan store writes used --state-dir='{self.store}' "
            f'and --state-dir="{self.store}"',
            override=str(self.effective),
        )
        assert report is not None
        self.assertEqual([str(self.store)], report["required_state_roots"])

    def test_flags_without_plan_store_write_requirement_are_not_demands(self) -> None:
        for requirement in (
            f'CLI supports --state-dir "{self.store}"',
            f'Read-only verification uses --state-dir "{self.store}"',
            "Plan-store writes tested with --state-dir $STORE",
            "Plan-store writes tested with --state-dir relative/example",
        ):
            with self.subTest(requirement=requirement):
                self.assertIsNone(
                    self.report(requirement, override=str(self.effective))
                )

    def test_historical_handoff_and_examples_are_not_requirements(self) -> None:
        self.report("recipe runs", override=str(self.effective))
        path = self.plan / "session-1.md"
        path.write_text(
            path.read_text()
            + f'\n- Plan-store writes used --state-dir "{self.store}"\n'
        )
        self.assertIsNone(
            run_inputs._session_plan_store_state_root_report(
                self.plan, "1", state_dir_arg=str(self.effective), run_dir=self.run
            )
        )

    def test_no_contract_keeps_existing_behavior(self) -> None:
        write_plan(self.plan, ("pending",))
        self.assertIsNone(
            run_inputs._session_plan_store_state_root_report(
                self.plan, "1", state_dir_arg=None, run_dir=self.run
            )
        )
