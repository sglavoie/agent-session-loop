# mypy: ignore-errors
from .asl_write_guards_support import *


class WriteGuardsBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes in one place."""

    def setUp(self) -> None:
        self.trees = {
            module: ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
            for module in GUARD_MODULES
        }

    def test_the_modules_do_not_import_the_runner(self) -> None:
        for module in GUARD_MODULES:
            with self.subTest(module=module.__name__):
                assert_no_runner_import(self, module)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_FUNCTIONS + MOVED_CONSTANTS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(OWNERS[name], name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        defined = {
            node.name
            for tree in self.trees.values()
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.assertEqual(set(MOVED_FUNCTIONS), defined)

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for tree in self.trees.values():
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                writes = called_names(node) & DURABLE_WRITE_NAMES
                if writes:
                    observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)


class RefusalShapeTests(unittest.TestCase):
    """A refusal is five steps, and every enforcement path performs all five."""

    def _source(self, name: str) -> str:
        return inspect.getsource(getattr(OWNERS[name], name))

    def test_every_refusal_reverts_rereads_writes_records_then_pauses(self) -> None:
        for name in ENFORCEMENT_FUNCTIONS:
            with self.subTest(name=name):
                source = self._source(name)
                reverted = source.index(
                    "handoff_guards._revert_session_status_to_pending("
                )
                reinspected = source.index("inspected = reinspect(")
                # The pass lane writes the result too, so the refusal's own
                # write is the last one rather than the first.
                written = source.rindex("durability.write_json_file(")
                recorded = source.rindex("turn_execution_commit.record_turn_rejected(")
                paused = source.index("run_termination._pause_run(")
                self.assertLess(reverted, reinspected)
                self.assertLess(reinspected, written)
                self.assertLess(written, recorded)
                self.assertLess(recorded, paused)

    def test_every_refusal_republishes_the_snapshot_it_just_reread(self) -> None:
        # The revert moved a status, so the snapshot the run publishes must be
        # the post-revert one; publishing the pre-revert snapshot would report
        # a session as complete that the guard had just reopened.
        for name in ENFORCEMENT_FUNCTIONS:
            with self.subTest(name=name):
                self.assertIn(
                    'payload["final_snapshot"] = '
                    "plans_payloads.snapshot_payload(inspected)",
                    self._source(name),
                )


class SeamTests(unittest.TestCase):
    """The two runner-owned collaborators enter as required keyword-only seams."""

    def test_both_seams_are_required_keyword_only_everywhere(self) -> None:
        for name in ENFORCEMENT_FUNCTIONS:
            signature = inspect.signature(getattr(OWNERS[name], name))
            for seam in SEAM_PARAMETERS:
                with self.subTest(name=name, seam=seam):
                    parameter = signature.parameters[seam]
                    self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_the_seam_protocols_are_satisfied_by_the_symbols_the_loop_passes(
        self,
    ) -> None:
        # Session 23 moved the re-read into `session_verification` and Session 24
        # moved the appender into `handoff_correction`; both stay seams because
        # `handoff_correction` imports both of those modules, so this is what
        # checks the two declared shapes against the symbols actually passed,
        # rather than only the loop's call sites doing so at runtime.
        appender: handoff_guards.HandoffNoteAppender = (
            handoff_correction._append_handoff_note
        )
        reinspect: run_termination.SnapshotReinspector = (
            session_verification._reinspect_after_revert
        )
        self.assertEqual(
            ["- **Notes**:", "  - guard refused"],
            appender(
                ["## Handoff", "", "- **Notes**:"],
                handoff_index=0,
                section_end=3,
                note="guard refused",
            )[2:],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            plan_dir = Path(tmp_dir).resolve() / "plan"
            write_plan(plan_dir, ("pending",))
            fallback = plans_snapshot.inspect_plan(plan_dir)
            # Nothing was reverted, so the fallback snapshot is returned as it
            # stands rather than re-read.
            self.assertIs(
                fallback,
                reinspect(plan_dir, fallback, None, plans_snapshot.PlanSnapshotCache()),
            )

    def test_the_loop_passes_both_seams_at_every_enforcement_call(self) -> None:
        # The two paths live in two modules since Session 34: the finalization
        # sequence moved into `turn_postflight`, while the crash path stayed
        # with the loop's own `except BaseException` handler -- which Session
        # 37 carried out of the runner into `run_loop`. Both are scanned, and
        # each is counted on its own, so a call that merely moved between them
        # cannot pass by keeping the total.
        calls: dict[str, int] = {}
        guard_names = {module.__name__.rpartition(".")[2] for module in GUARD_MODULES}
        for module in (run_loop_finalization, turn_postflight):
            tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if (
                    not isinstance(func.value, ast.Name)
                    or func.value.id not in guard_names
                ):
                    continue
                if func.attr not in ENFORCEMENT_FUNCTIONS:
                    continue
                calls[module.__name__] = calls.get(module.__name__, 0) + 1
                passed = {keyword.arg for keyword in node.keywords}
                with self.subTest(module=module.__name__, line=node.lineno):
                    self.assertLessEqual(set(SEAM_PARAMETERS), passed)
        # One per guard on each path: the finalization sequence and the crash
        # path each reach all three.
        self.assertEqual(
            {
                "agent_session_loop.run_loop_finalization": 3,
                "agent_session_loop.turn_postflight": 3,
            },
            calls,
        )
