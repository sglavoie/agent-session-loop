"""The typing ratchet: repository-wide suppressions may only shrink.

The repository still hides nine mypy error codes globally, because clearing
them needs container and generic annotations across a 125-file untyped tree
rather than defect fixes. That baseline is a debt, and debt without a ratchet
grows: a new module lands untyped, a tenth code joins the list to keep the
gate green, and the extraction program's typed boundaries stop being a
boundary at all.

These tests are that ratchet. They assert three things a future session
cannot quietly undo:

* the repository-wide ``disable_error_code`` list may only *shrink*, and no
  per-module override may add a suppression of its own;
* the ``enable_error_code`` override list of extracted durable boundaries may
  only *grow*, and every module on it re-enables all three codes;
* every module on that list keeps its durable-boundary payloads typed --
  annotated public signatures, parameterized containers, no ``type: ignore``,
  and no import of the runner it was extracted from.

The third check is a linter, not a mypy run: it fails on the *shape* mypy
would have to trust, so a public entry point that takes a bare ``dict`` is
caught even though ``dict`` is a perfectly valid annotation.
"""

from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The repository-wide suppressions as of the session that added this ratchet.
#: This set is a ceiling: a code may leave it once the tree is clean enough to
#: drop the suppression, but nothing may join it. A new module that would need
#: a tenth code is a module that needs annotations instead.
BASELINE_DISABLED_CODES = frozenset(
    {
        "arg-type",
        "union-attr",
        "index",
        "assignment",
        "var-annotated",
        "misc",
        "call-overload",
        "operator",
        "override",
        "annotation-unchecked",
    }
)

#: The codes an extracted durable boundary opts back in to. All three are ways
#: a wrong value reaches a durable record instead of raising at the call site.
RATCHETED_CODES = frozenset({"arg-type", "union-attr", "index"})

#: Modules extracted out of the runner in sessions 5 and 9-11. This set is a
#: floor: every future extraction adds to it, and nothing may leave it. A
#: module removed from ``pyproject.toml`` fails here rather than silently
#: rejoining the untyped baseline.
REQUIRED_RATCHETED_MODULES = frozenset(
    {
        "agent_session_loop.agent_mux_delivery",
        "agent_session_loop.agent_mux_projection",
        "agent_session_loop.clock",
        "agent_session_loop.durability",
        "agent_session_loop.harness_compatibility",
        "agent_session_loop.maintenance",
        "agent_session_loop.review_transitions",
        "agent_session_loop.run_coordination",
        "agent_session_loop.transitions",
        "agent_session_loop.turn_execution",
        "agent_session_loop.verification_transitions",
    }
)

#: Container annotations that say nothing about what they hold. A durable
#: payload crossing a boundary as one of these is exactly the case the
#: repository-wide ``arg-type`` suppression was hiding. ``Any`` is excluded
#: deliberately: it is an explicit statement that the value is unconstrained,
#: which a JSON payload slot legitimately is.
UNPARAMETERIZED_CONTAINERS = frozenset(
    {
        "dict",
        "list",
        "tuple",
        "set",
        "frozenset",
        "Mapping",
        "MutableMapping",
        "Sequence",
        "Iterable",
        "Iterator",
        "Callable",
    }
)


def load_mypy_config() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["mypy"]


def ratcheted_override() -> dict[str, Any]:
    """The single override table that re-enables codes for typed boundaries."""
    overrides = load_mypy_config()["overrides"]
    enabling = [override for override in overrides if "enable_error_code" in override]
    if len(enabling) != 1:
        raise AssertionError(
            "expected exactly one enable_error_code override table, "
            f"found {len(enabling)}"
        )
    return enabling[0]


def module_path(dotted: str) -> Path:
    return REPO_ROOT / Path(*dotted.split(".")).with_suffix(".py")


def unparameterized_container(annotation: ast.expr) -> str | None:
    """Name the bare container this annotation is, or ``None`` when it is typed."""
    if isinstance(annotation, ast.Name) and annotation.id in UNPARAMETERIZED_CONTAINERS:
        return annotation.id
    if (
        isinstance(annotation, ast.Attribute)
        and annotation.attr in UNPARAMETERIZED_CONTAINERS
    ):
        return annotation.attr
    return None


def public_definitions(
    tree: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every module-level and class-level definition that is part of the API.

    Nested helpers are excluded: a closure inside a private function is not a
    boundary anything durable crosses, and typing it is a style question rather
    than a correctness one.
    """
    definitions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def collect(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                collect(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("_") and node.name != "__init__":
                    continue
                definitions.append((f"{prefix}{node.name}", node))

    collect(tree.body, "")
    return definitions


def annotated_arguments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = node.args
    every = [
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
        args.vararg,
        args.kwarg,
    ]
    return [
        argument
        for argument in every
        if argument is not None and argument.arg not in ("self", "cls")
    ]


class GlobalSuppressionRatchetTests(unittest.TestCase):
    """The repository-wide baseline may only shrink."""

    def test_no_code_joins_the_repository_wide_baseline(self) -> None:
        disabled = set(load_mypy_config()["disable_error_code"])

        added = disabled - BASELINE_DISABLED_CODES
        self.assertEqual(
            added,
            set(),
            "new repository-wide mypy suppressions are not allowed; annotate the "
            f"code instead of disabling {sorted(added)}",
        )

    def test_the_baseline_pin_is_kept_honest(self) -> None:
        """A code dropped from ``pyproject.toml`` must be dropped here too.

        Without this the ceiling would drift upward for free: a session could
        clear a code, leave the pin behind, and a later session could re-add it
        without tripping the ratchet above.
        """
        disabled = set(load_mypy_config()["disable_error_code"])

        self.assertEqual(disabled, set(BASELINE_DISABLED_CODES))

    def test_no_override_table_adds_a_suppression(self) -> None:
        """Per-module ``disable_error_code`` is unioned with the global list.

        That makes it a repository-wide suppression wearing a narrower name, so
        the ratchet refuses it outright.
        """
        for override in load_mypy_config()["overrides"]:
            with self.subTest(module=override.get("module")):
                self.assertNotIn("disable_error_code", override)

    def test_the_typed_boundaries_are_still_checked(self) -> None:
        config = load_mypy_config()

        self.assertEqual(
            config["files"],
            [
                "agent_session_loop",
                "tests",
                "skills/asl/scripts",
                "skills/session-resplit/scripts",
            ],
        )


class RatchetedModuleListTests(unittest.TestCase):
    """The list of typed durable boundaries may only grow."""

    def test_every_extracted_boundary_is_still_on_the_list(self) -> None:
        listed = set(ratcheted_override()["module"])

        missing = REQUIRED_RATCHETED_MODULES - listed
        self.assertEqual(
            missing,
            set(),
            "an extracted durable boundary left the enable_error_code list; "
            f"restore {sorted(missing)}",
        )

    def test_every_listed_module_re_enables_all_three_codes(self) -> None:
        override = ratcheted_override()

        self.assertEqual(set(override["enable_error_code"]), set(RATCHETED_CODES))

    def test_every_listed_module_exists(self) -> None:
        for dotted in ratcheted_override()["module"]:
            with self.subTest(module=dotted):
                self.assertTrue(module_path(dotted).is_file())

    def test_the_list_stays_sorted_so_additions_do_not_collide(self) -> None:
        listed = list(ratcheted_override()["module"])

        self.assertEqual(listed, sorted(listed))

    def test_the_required_set_names_only_real_modules(self) -> None:
        for dotted in sorted(REQUIRED_RATCHETED_MODULES):
            with self.subTest(module=dotted):
                self.assertTrue(module_path(dotted).is_file())


class DurableBoundaryTypingTests(unittest.TestCase):
    """Ratcheted modules keep their durable-boundary payloads typed."""

    def modules(self) -> list[tuple[str, ast.Module]]:
        parsed: list[tuple[str, ast.Module]] = []
        for dotted in ratcheted_override()["module"]:
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
        for dotted in ratcheted_override()["module"]:
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
