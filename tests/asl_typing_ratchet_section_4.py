# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import test_asl_typing_ratchet_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_typing_ratchet_shared import *
from .asl_typing_ratchet_section_1 import *
from .asl_typing_ratchet_section_2 import *
from .asl_typing_ratchet_section_3 import *


class DurableBoundaryTypingTests(unittest.TestCase):
    """Boundary modules keep their durable-boundary payloads typed.

    Scoped to the Boundary table. These four rules are extraction discipline,
    which the Checked table's members are on that table precisely because they
    cannot meet.
    """

    def boundary_modules(self) -> list[str]:
        return list(ratcheted_override(BOUNDARY_TABLE)["module"])

    def modules(self) -> list[tuple[str, ast.Module]]:
        parsed: list[tuple[str, ast.Module]] = []
        for dotted in self.boundary_modules():
            path = module_path(dotted)
            parsed.append((dotted, ast.parse(path.read_text(encoding="utf-8"))))
        return parsed

    def test_public_signatures_are_fully_annotated(self) -> None:
        for dotted, tree in self.modules():
            for name, node in public_definitions(tree):
                with self.subTest(module=dotted, definition=name):
                    if node.name != "__init__":
                        self.assertIsNotNone(
                            node.returns,
                            f"{dotted}.{name} has no return annotation",
                        )
                    for argument in annotated_arguments(node):
                        self.assertIsNotNone(
                            argument.annotation,
                            f"{dotted}.{name} takes an unannotated {argument.arg}",
                        )

    def test_public_signatures_carry_no_bare_containers(self) -> None:
        for dotted, tree in self.modules():
            for name, node in public_definitions(tree):
                with self.subTest(module=dotted, definition=name):
                    annotations = [
                        argument.annotation
                        for argument in annotated_arguments(node)
                        if argument.annotation is not None
                    ]
                    if node.returns is not None:
                        annotations.append(node.returns)
                    for annotation in annotations:
                        bare = unparameterized_container(annotation)
                        self.assertIsNone(
                            bare,
                            f"{dotted}.{name} crosses the boundary with a bare "
                            f"{bare}; name what it holds",
                        )

    def test_declared_attributes_carry_no_bare_containers(self) -> None:
        """Dataclass fields and module constants are durable payload slots too."""
        for dotted, tree in self.modules():
            for node in ast.walk(tree):
                if not isinstance(node, ast.AnnAssign):
                    continue
                bare = unparameterized_container(node.annotation)
                with self.subTest(module=dotted, line=node.lineno):
                    self.assertIsNone(
                        bare,
                        f"{dotted}:{node.lineno} declares a bare {bare}",
                    )

    def test_no_ratcheted_module_suppresses_an_error_inline(self) -> None:
        """``# type: ignore`` would reopen locally what the module opted in to."""
        for dotted in self.boundary_modules():
            source = module_path(dotted).read_text(encoding="utf-8")
            with self.subTest(module=dotted):
                self.assertNotIn("type: ignore", source)
                self.assertNotIn("mypy: disable-error-code", source)

    def test_no_ratcheted_module_imports_the_runner(self) -> None:
        """The extraction only holds while the dependency runs one way."""
        for dotted, tree in self.modules():
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [alias.name for alias in node.names]
                    if node.module is not None:
                        imported.append(node.module)
                with self.subTest(module=dotted, line=getattr(node, "lineno", 0)):
                    self.assertNotIn(
                        "runner", [name.split(".")[-1] for name in imported]
                    )


if __name__ == "__main__":
    unittest.main()


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    suite = unittest.TestSuite()
    cases = sorted(
        (
            value
            for value in globals().values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ),
        key=lambda case: case.__name__,
    )
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite
