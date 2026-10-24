from .asl_run_inputs_support import *
from .asl_run_inputs_support import _args, _RecordingResolver
from .support_base import isolated_home


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

        # Every other read goes through the `plans_*` modules, `preflight`, or
        # `state`. This one is direct because the point of the check is to see what the
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

    def setUp(self) -> None:
        # The home directory is one of the refused external roots; refuse one
        # this test owns rather than reading the developer's.
        isolated_home(self)

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

            self.assertEqual(plans_files.canonicalize_path(tmp), cwd)
        self.assertEqual(900, timeout)
        self.assertEqual(3, max_sessions)
        self.assertEqual("per-session", commit_mode)
        self.assertEqual([args], resolver.calls)

    def test_a_missing_cwd_is_refused_before_anything_else_is_parsed(self) -> None:
        resolver = _RecordingResolver()
        with tempfile.TemporaryDirectory() as tmp:
            args = _args(cwd=str(Path(tmp) / "absent"))

            with self.assertRaises(plans_errors.PlanError) as caught:
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
