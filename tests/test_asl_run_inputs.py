"""The run-admission boundary: what a run is allowed to start on, and reach.

The behavioural coverage of these refusals lives where it always has —
``tests/test_asl_run_gates.py`` drives the flag cross-checks through both the
runner and the supervisor helper, ``tests/test_asl_workspaces.py`` resolves
real registries, and ``tests/test_asl_review.py`` routes whole runs through
the cycle caps. What is asserted here is the boundary those modules cannot
state, because they exercise the loop rather than the seam:

* the module does not import the runner, and needs nothing from it;
* it opens exactly one path of its own — the task packet's artifacts, read to
  judge them — and its single durable write is delegated to the migration that
  owns it; and
* the sequence and the refusals that make admission separable at all.

That last group is the reason this file exists. An admission bug does not fail
a gate: it starts a run whose destination nothing has bound, or refuses a run
for the wrong reason and in the wrong order, and the operator sees only the
message that came out last.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    plans,
    repository_registration,
    run_inputs,
    run_selection,
    state,
    verification,
)
from agent_session_loop.run_errors import RunError
from tests.support import _contract_block, assert_no_runner_import, write_plan


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"cwd": ".", "commit_mode": None, "spark": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class _RecordingResolver:
    """Stand-in for the review guard's allowlist parser, remembering calls."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[argparse.Namespace] = []
        self._error = error

    def __call__(self, args: argparse.Namespace) -> tuple[str, ...]:
        self.calls.append(args)
        if self._error is not None:
            raise self._error
        return ()


class RunInputsBoundaryTests(unittest.TestCase):
    """``run_inputs`` is runner-free, and owns no write of its own."""

    def test_run_inputs_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_inputs)

    def test_the_module_creates_no_path_of_its_own(self) -> None:
        source = Path(run_inputs.__file__ or "").read_text(encoding="utf-8")

        # The one durable write this gate causes is the registry, and it is
        # `repository_registration`'s write, guarded by that migration's own
        # refusal. A path created here would be a surface with no owner.
        for writer in ("write_text(", "open(", "mkdir(", "unlink("):
            self.assertNotIn(writer, source, msg=writer)
        self.assertEqual(1, source.count("register_repositories("))
        self.assertEqual(1, source.count("save_plan_metadata("))

    def test_the_only_read_is_the_task_packet_artifact_scan(self) -> None:
        source = Path(run_inputs.__file__ or "").read_text(encoding="utf-8")

        # Every other read goes through `plans`, `preflight`, or `state`. This
        # one is direct because the point of the check is to see what the
        # worker will see, not what a parser reports about it.
        self.assertEqual(1, source.count("read_text("))

    def test_the_allowlist_parser_arrives_as_a_callback(self) -> None:
        source = Path(run_inputs.__file__ or "").read_text(encoding="utf-8")

        # The allowlist parser now lives in `review_guard`, but it still
        # arrives as a callable rather than as an import: the seam is what
        # fixes *when* the refusal runs, keeping it at its position in the
        # sequence instead of hoisting it to this module's own imports.
        self.assertIn(
            "ReviewGuardExcludeResolver = Callable"
            "[[argparse.Namespace], tuple[str, ...]]",
            source,
        )
        self.assertIn(
            "resolve_review_guard_excludes: ReviewGuardExcludeResolver", source
        )
        self.assertEqual(1, source.count("resolve_review_guard_excludes(args)"))


class RunAdmissionTests(unittest.TestCase):
    """``_validate_run_inputs`` refuses in a fixed order, for stated reasons."""

    def test_external_roots_are_explicit_canonical_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "repo"
            repo.mkdir()
            data = root / "trial"
            data.mkdir()
            link = root / "trial-link"
            link.symlink_to(data, target_is_directory=True)
            args = _args(cwd=str(repo), external_write_root=[str(link), str(data)])
            run_inputs._validate_run_inputs(
                args, resolve_review_guard_excludes=_RecordingResolver()
            )
            self.assertEqual([str(data)], args.external_write_root)
            with self.assertRaisesRegex(RunError, "changed its target"):
                run_inputs._validate_run_inputs(
                    _args(
                        cwd=str(repo),
                        external_write_root=[str(link)],
                        _restored_external_write_roots=[str(link)],
                    ),
                    resolve_review_guard_excludes=_RecordingResolver(),
                )
            for invalid in (
                "relative",
                str(root),
                str(repo),
                str(root / "missing"),
                str(root / "*"),
                str(Path.home()),
            ):
                with self.subTest(invalid=invalid), self.assertRaises(RunError):
                    run_inputs._validate_run_inputs(
                        _args(cwd=str(repo), external_write_root=[invalid]),
                        resolve_review_guard_excludes=_RecordingResolver(),
                    )

    def test_a_valid_namespace_resolves_the_canonical_cwd_and_flags(self) -> None:
        resolver = _RecordingResolver()
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(
                cwd=tmp, commit_mode="per-session", timeout=900, max_sessions=3
            )
            cwd, timeout, max_sessions, commit_mode = run_inputs._validate_run_inputs(
                args, resolve_review_guard_excludes=resolver
            )

            self.assertEqual(plans.canonicalize_path(tmp), cwd)
        self.assertEqual(900, timeout)
        self.assertEqual(3, max_sessions)
        self.assertEqual("per-session", commit_mode)
        self.assertEqual([args], resolver.calls)

    def test_a_missing_cwd_is_refused_before_anything_else_is_parsed(self) -> None:
        resolver = _RecordingResolver()
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(cwd=str(Path(tmp) / "absent"))

            with self.assertRaises(plans.PlanError) as caught:
                run_inputs._validate_run_inputs(
                    args, resolve_review_guard_excludes=resolver
                )

        self.assertIn("cwd does not exist", str(caught.exception))
        self.assertEqual([], resolver.calls)

    def test_the_allowlist_is_parsed_only_after_the_cross_field_checks(self) -> None:
        # The seam is a callback rather than a pre-resolved tuple precisely so
        # this stays true: a run refused for contradictory flags must not be
        # refused for its allowlist instead, which is what resolving the
        # allowlist before validation would do.
        resolver = _RecordingResolver()
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(
                cwd=tmp, max_review_fix_cycles=9, max_review_fix_cycles_ceiling=8
            )

            with self.assertRaises(RunError) as caught:
                run_inputs._validate_run_inputs(
                    args, resolve_review_guard_excludes=resolver
                )

        self.assertIn("exceeds --max-review-fix-cycles-ceiling", str(caught.exception))
        self.assertEqual([], resolver.calls)

    def test_an_allowlist_refusal_reaches_the_caller_unchanged(self) -> None:
        resolver = _RecordingResolver(RunError("--review-guard-exclude escapes"))
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(RunError) as caught,
        ):
            run_inputs._validate_run_inputs(
                _args(cwd=tmp), resolve_review_guard_excludes=resolver
            )

        self.assertEqual("--review-guard-exclude escapes", str(caught.exception))

    def test_a_spark_baseline_equal_to_the_spark_model_is_refused(self) -> None:
        resolver = _RecordingResolver()
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(cwd=tmp, spark=True, harness_model=run_selection.SPARK_MODEL)

            with self.assertRaises(RunError) as caught:
                run_inputs._validate_run_inputs(
                    args, resolve_review_guard_excludes=resolver
                )

        self.assertIn("must differ from the Spark model", str(caught.exception))
        self.assertEqual([], resolver.calls)


class PlanStatusProjectionTests(unittest.TestCase):
    """The two projections that call a plan finished, or malformed."""

    def test_a_plan_is_complete_only_when_every_session_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = Path(tmp) / "done"
            write_plan(done, ("complete", "complete"))
            partial = Path(tmp) / "partial"
            write_plan(partial, ("complete", "pending"))

            self.assertTrue(run_inputs._all_sessions_complete(plans.inspect_plan(done)))
            self.assertFalse(
                run_inputs._all_sessions_complete(plans.inspect_plan(partial))
            )

    def test_a_well_formed_plan_names_no_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending"))

            self.assertIsNone(
                run_inputs._invalid_status_pause(plans.inspect_plan(plan_dir))
            )

    def test_an_unrecognized_status_pauses_with_the_offending_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "in-progress"))

            pause = run_inputs._invalid_status_pause(plans.inspect_plan(plan_dir))

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

            pause = run_inputs._invalid_status_pause(plans.inspect_plan(plan_dir))

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
                (plan_dir, plans.canonicalize_path(state_dir)),
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
        self.assertIn(str(plans.canonicalize_path(other)), report["git_roots"])

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
            tuple(s.status for s in plans.inspect_plan(self.plan).sessions),
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


class LegacyBindingTests(unittest.TestCase):
    """Binding is checked, never rewritten — except a first bind."""

    def test_an_unbound_legacy_plan_resolves_to_the_run_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            cwd = plans.canonicalize_path(tmp)

            context = run_inputs._resolve_run_verification_context(plan_dir, cwd)

        self.assertEqual(cwd, context.root)
        self.assertIsNone(context.workspace)
        self.assertEqual(plans.canonicalize_path(plan_dir), context.plan_dir)

    def test_a_legacy_plan_bound_elsewhere_refuses_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            plans.save_plan_metadata(plan_dir, elsewhere)

            with self.assertRaises(plans.PlanError) as caught:
                run_inputs._resolve_run_verification_context(
                    plan_dir, plans.canonicalize_path(tmp)
                )

        self.assertIn("does not match stored plan metadata", str(caught.exception))

    def test_a_legacy_plan_is_first_bound_once_and_never_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            first = Path(tmp) / "first"
            first.mkdir()

            run_inputs._ensure_legacy_run_metadata(
                plan_dir, verification.VerificationContext.legacy(first)
            )
            bound = plans.load_plan_metadata(plan_dir)

            # A second call against another root must not move the binding;
            # the mismatch is `_resolve_run_verification_context`'s to refuse.
            second = Path(tmp) / "second"
            second.mkdir()
            run_inputs._ensure_legacy_run_metadata(
                plan_dir, verification.VerificationContext.legacy(second)
            )

            self.assertEqual(bound, plans.load_plan_metadata(plan_dir))

        assert isinstance(bound, plans.PlanMetadata)
        self.assertEqual(str(plans.canonicalize_path(first)), bound.cwd)

    def test_a_resume_without_a_persisted_binding_is_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = verification.VerificationContext.legacy(tmp)

            run_inputs._validate_resume_workspace_binding(_args(), context)
            run_inputs._validate_resume_workspace_binding(
                _args(_resume_workspace_config={"workspace_root": None}), context
            )

    def test_a_resume_that_changed_workspace_mode_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = verification.VerificationContext.legacy(tmp)
            args = _args(_resume_workspace_config={"workspace_root": tmp})

            with self.assertRaises(plans.PlanError) as caught:
                run_inputs._validate_resume_workspace_binding(args, context)

        self.assertIn("workspace mode does not match", str(caught.exception))


class CycleCapTests(unittest.TestCase):
    """The caps the loop reads off the same namespace afterwards."""

    def test_the_explicit_cycle_cap_wins_over_the_deprecated_alias(self) -> None:
        args = _args(max_review_fix_cycles=1, max_review_fix_plans=7)

        self.assertEqual(1, run_inputs._max_review_fix_cycles(args))

    def test_the_deprecated_alias_is_honored_when_it_is_the_only_flag(self) -> None:
        args = _args(max_review_fix_cycles=None, max_review_fix_plans=7)

        self.assertEqual(7, run_inputs._max_review_fix_cycles(args))

    def test_the_defaults_apply_when_nothing_is_supplied(self) -> None:
        args = _args()

        self.assertEqual(
            run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            run_inputs._max_review_fix_cycles(args),
        )
        self.assertEqual(
            run_inputs.DEFAULT_MAX_REPAIR_GROUP_ATTEMPTS,
            run_inputs._max_repair_group_attempts(args),
        )
        self.assertEqual(
            run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES_CEILING,
            run_inputs._max_review_fix_cycles_ceiling(args),
        )

    def test_an_explicit_ceiling_and_group_cap_are_read_back(self) -> None:
        args = _args(max_review_fix_cycles_ceiling=12, max_repair_group_attempts=5)

        self.assertEqual(12, run_inputs._max_review_fix_cycles_ceiling(args))
        self.assertEqual(5, run_inputs._max_repair_group_attempts(args))


class RepositoryPathLabelTests(unittest.TestCase):
    """Aliases become filesystem-safe labels, and never become nothing."""

    def test_unsafe_characters_collapse_into_single_dashes(self) -> None:
        self.assertEqual("api-web", run_inputs._repository_path_label("api / web"))
        self.assertEqual(
            "main_repo-1", run_inputs._repository_path_label("main_repo.1")
        )

    def test_an_alias_with_nothing_usable_falls_back(self) -> None:
        self.assertEqual("repository", run_inputs._repository_path_label("///"))


if __name__ == "__main__":
    unittest.main()
