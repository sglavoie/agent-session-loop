from .asl_run_inputs_support import *
from .asl_run_inputs_support import _args


class PlanStatusProjectionTests(unittest.TestCase):
    """The two projections that call a plan finished, or malformed."""

    def test_a_plan_is_complete_only_when_every_session_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = Path(tmp) / "done"
            write_plan(done, ("complete", "complete"))
            partial = Path(tmp) / "partial"
            write_plan(partial, ("complete", "pending"))

            self.assertTrue(
                run_inputs._all_sessions_complete(plans_snapshot.inspect_plan(done))
            )
            self.assertFalse(
                run_inputs._all_sessions_complete(plans_snapshot.inspect_plan(partial))
            )

    def test_a_well_formed_plan_names_no_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending"))

            self.assertIsNone(
                run_inputs._invalid_status_pause(plans_snapshot.inspect_plan(plan_dir))
            )

    def test_an_unrecognized_status_pauses_with_the_offending_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "in-progress"))

            pause = run_inputs._invalid_status_pause(
                plans_snapshot.inspect_plan(plan_dir)
            )

        assert pause is not None
        reason, detail, payload = pause
        self.assertEqual("unknown-status", reason)
        self.assertIn("not pending or complete", detail)
        self.assertEqual(["2"], [entry["id"] for entry in payload["sessions"]])
        # The pause carries the value it could not read, not only its verdict.
        self.assertEqual("in-progress", payload["sessions"][0]["raw_value"])

    def test_a_session_without_a_populated_status_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending"))
            (plan_dir / "session-2.md").write_text(
                "# Session 2: Test\n\nInstructions.\n", encoding="utf-8"
            )

            pause = run_inputs._invalid_status_pause(
                plans_snapshot.inspect_plan(plan_dir)
            )

        assert pause is not None
        reason, detail, payload = pause
        self.assertEqual("malformed-plan", reason)
        self.assertIn("missing a populated handoff status", detail)
        self.assertEqual(["2"], [entry["id"] for entry in payload["sessions"]])
        self.assertEqual("empty", payload["sessions"][0]["status"])


class RunnerOwnedRootsTests(unittest.TestCase):
    """ASL's own bookkeeping is never a repository the run reaches."""

    def test_the_plan_and_state_roots_are_always_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"

            roots = run_inputs._runner_owned_roots(plan_dir, str(state_dir))

            self.assertEqual(
                (plan_dir, plans_files.canonicalize_path(state_dir)),
                roots,
            )

    def test_a_run_directory_joins_them_when_there_is_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            run_dir = Path(tmp) / "state" / "runs" / "run-1"

            roots = run_inputs._runner_owned_roots(
                plan_dir, str(state_dir), run_dir=run_dir
            )

        self.assertEqual(3, len(roots))
        self.assertEqual(run_dir, roots[-1])


class LegacyBindingTests(unittest.TestCase):
    """Binding is checked, never rewritten — except a first bind."""

    def test_an_unbound_legacy_plan_resolves_to_the_run_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            cwd = plans_files.canonicalize_path(tmp)

            context = run_inputs._resolve_run_verification_context(plan_dir, cwd)

        self.assertEqual(cwd, context.root)
        self.assertIsNone(context.workspace)
        self.assertEqual(plans_files.canonicalize_path(plan_dir), context.plan_dir)

    def test_a_legacy_plan_bound_elsewhere_refuses_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            plans_metadata.save_plan_metadata(plan_dir, elsewhere)

            with self.assertRaises(plans_errors.PlanError) as caught:
                run_inputs._resolve_run_verification_context(
                    plan_dir, plans_files.canonicalize_path(tmp)
                )

        self.assertIn("does not match stored plan metadata", str(caught.exception))

    def test_a_legacy_plan_is_first_bound_once_and_never_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            first = Path(tmp) / "first"
            first.mkdir()

            run_inputs._ensure_legacy_run_metadata(
                plan_dir, verification_models.VerificationContext.legacy(first)
            )
            bound = plans_metadata.load_plan_metadata(plan_dir)

            # A second call against another root must not move the binding;
            # the mismatch is `_resolve_run_verification_context`'s to refuse.
            second = Path(tmp) / "second"
            second.mkdir()
            run_inputs._ensure_legacy_run_metadata(
                plan_dir, verification_models.VerificationContext.legacy(second)
            )

            self.assertEqual(bound, plans_metadata.load_plan_metadata(plan_dir))

        assert isinstance(bound, plans_metadata.PlanMetadata)
        self.assertEqual(str(plans_files.canonicalize_path(first)), bound.cwd)

    def test_a_resume_without_a_persisted_binding_is_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = verification_models.VerificationContext.legacy(tmp)

            run_inputs._validate_resume_workspace_binding(_args(), context)
            run_inputs._validate_resume_workspace_binding(
                _args(_resume_workspace_config={"workspace_root": None}), context
            )

    def test_a_resume_that_changed_workspace_mode_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = verification_models.VerificationContext.legacy(tmp)
            args = _args(_resume_workspace_config={"workspace_root": tmp})

            with self.assertRaises(plans_errors.PlanError) as caught:
                run_inputs._validate_resume_workspace_binding(args, context)

        self.assertIn("workspace mode does not match", str(caught.exception))
