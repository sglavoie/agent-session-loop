from .asl_run_loop_support import *
from .asl_run_loop_support import _loop_tree


class BoundaryTests(unittest.TestCase):
    """The dependency runs one way, and there is nothing to run it back."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, run_loop)

    def test_the_module_reaches_no_name_through_the_runner(self) -> None:
        # The import check above catches the import; this catches the other
        # half, an attribute read on a `runner` the module never imported,
        # which would fail at runtime rather than at import time.
        reached = {
            ast.unparse(node)
            for node in ast.walk(_loop_tree())
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "runner"
        }
        self.assertEqual(set(), reached)

    def test_every_moved_symbol_lives_here_and_nowhere_in_the_runner(self) -> None:
        for name, owner in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(owner, name))
                self.assertFalse(hasattr(runner, name))

    def test_the_names_it_borrows_are_read_at_their_owners(self) -> None:
        source = run_loop_view.assembled_source()
        for name in OWNED_ELSEWHERE:
            with self.subTest(name=name):
                self.assertIn(name, source)


class SingularityTests(unittest.TestCase):
    """One loop, one context, and helpers that serve only this module."""

    def test_the_module_owns_exactly_one_session_loop(self) -> None:
        loops = [node for node in ast.walk(_loop_tree()) if isinstance(node, ast.While)]
        self.assertEqual(
            1,
            len(loops),
            "the session loop is the module's whole shape; a second `while` "
            "here is a second place a run can decide to stop",
        )

    def test_the_loop_is_the_module_s_only_public_function(self) -> None:
        public = [
            node.name
            for node in _loop_tree().body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        ]
        self.assertEqual(["run_locked"], public)

    def test_the_context_is_a_dataclass_the_delegates_can_satisfy(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(run_loop_context.RunContext))
        fields = {
            field.name for field in dataclasses.fields(run_loop_context.RunContext)
        }
        # The three lifetimes the docstring names, one witness each.
        for name in ("run_id", "completed_count", "last_session_details"):
            with self.subTest(field=name):
                self.assertIn(name, fields)

    def test_the_private_helpers_are_reached_from_nowhere_else(self) -> None:
        private = {name for name, _ in MOVED_SYMBOLS if name.startswith("_")}
        package = Path(run_loop.__file__ or "").parent
        for path in sorted(package.glob("*.py")):
            if path.name == "run_loop.py":
                continue
            source = path.read_text(encoding="utf-8")
            for name in sorted(private):
                with self.subTest(module=path.name, helper=name):
                    self.assertNotIn(f"run_loop.{name}", source)


class RunnerRemainderTests(unittest.TestCase):
    """What the runner kept: two delegations and the seam this module fills."""

    def test_the_runner_starts_a_run_through_this_module(self) -> None:
        source = inspect.getsource(runner.run_from_args)
        self.assertIn("run_locked=run_loop.run_locked", "".join(source.split()))

    def test_the_locked_run_still_satisfies_the_entry_point_s_seam(self) -> None:
        declared = inspect.signature(run_entry.LockedRun.__call__).parameters
        actual = inspect.signature(run_loop.run_locked).parameters
        for name in (name for name in declared if name != "self"):
            with self.subTest(parameter=name):
                self.assertIn(name, actual)
