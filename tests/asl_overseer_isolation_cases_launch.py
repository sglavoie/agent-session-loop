from .asl_overseer_isolation_support import *


class LaunchPropagationTests(unittest.TestCase):
    """The observation-only switch reaches the launch, and defaults off there."""

    def test_the_entry_point_still_reclaims_by_default(self) -> None:
        parameter = inspect.signature(run_entry.run_from_args).parameters[
            "reclaim_stale_locks"
        ]

        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        self.assertIs(True, parameter.default)

    def test_the_entry_point_forwards_the_switch_to_the_contention_check(
        self,
    ) -> None:
        """Read off the call site: the gate must not hardcode the hygiene."""
        source = inspect.getsource(run_entry.run_from_args)
        call = source[source.index("run_contention.detect_conflicts(") :]
        call = call[: call.index(")")]

        self.assertIn("reclaim=reclaim_stale_locks", call)

    def test_an_overseer_launch_turns_the_corpus_wide_hygiene_off(self) -> None:
        seen: list[dict[str, Any]] = []

        def _entry(args: argparse.Namespace, **kwargs: Any) -> int:
            seen.append({"args": args, **kwargs})
            return 0

        namespace = argparse.Namespace(plan_dir="/plans/mine")
        sentinel = object()

        code = overseer_isolation.launch(
            args=namespace,
            run_locked=sentinel,
            run_entry=_entry,
            run_argv=["run", "/plans/mine"],
        )

        self.assertEqual(0, code)
        self.assertEqual(1, len(seen))
        self.assertIs(False, seen[0]["reclaim_stale_locks"])
        self.assertIs(sentinel, seen[0]["run_locked"])
        self.assertIs(namespace, seen[0]["args"])


class ExtractionContractTests(unittest.TestCase):
    """The boundary stays importable without the runner and writes one file."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_isolation)

    def test_recovery_is_the_only_symbol_that_can_remove_a_lock(self) -> None:
        source = Path(overseer_isolation.__file__).read_text(encoding="utf-8")

        self.assertEqual(1, source.count("_reclaim_dead_local_lock"))
        self.assertNotIn(".unlink(", source)
        self.assertNotIn("unlock_plan", source)

    def test_the_observation_reads_locks_without_reclaiming(self) -> None:
        source = inspect.getsource(overseer_isolation.observe_contention)

        self.assertIn("reclaim=False", source)
        self.assertNotIn("reclaim=True", source)
