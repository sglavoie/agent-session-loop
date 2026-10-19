# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import test_asl_typing_ratchet_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_typing_ratchet_shared import *
from .asl_typing_ratchet_section_1 import *
from .asl_typing_ratchet_section_2 import *

REQUIRED_CHECKED_MODULES = frozenset(
    {
        "agent_session_loop.compact_state",
        "agent_session_loop.workbench_cli",
        "agent_session_loop.workbench_discovery",
        "agent_session_loop.workbench_launch",
        "agent_session_loop.workbench_recovery_actions",
        "agent_session_loop.workbench_recovery_build",
        "agent_session_loop.workbench_recovery_builders_run",
        "agent_session_loop.workbench_recovery_builders_state",
        "agent_session_loop.workbench_recovery_conversation",
        "agent_session_loop.workbench_recovery_evidence_checks",
        "agent_session_loop.workbench_recovery_evidence_commit",
        "agent_session_loop.workbench_recovery_models",
        "agent_session_loop.workbench_recovery_payload",
        "agent_session_loop.workbench_recovery_review",
        "agent_session_loop.workbench_recovery_state",
        "agent_session_loop.workbench_registry",
        "agent_session_loop.workbench_registry_coerce",
        "agent_session_loop.workbench_registry_models",
        "agent_session_loop.workbench_summary",
        "agent_session_loop.workbench_summary_helpers",
        "agent_session_loop.workbench_summary_model",
    }
)

#: Each table's floor, keyed by table name.
REQUIRED_MODULES = {
    BOUNDARY_TABLE: REQUIRED_RATCHETED_MODULES,
    CHECKED_TABLE: REQUIRED_CHECKED_MODULES,
}

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


def enabling_overrides() -> list[dict[str, Any]]:
    """Every override table that re-enables codes, in file order."""
    overrides = load_mypy_config()["overrides"]
    return [override for override in overrides if "enable_error_code" in override]


def ratcheted_override(table: str) -> dict[str, Any]:
    """The named override table that re-enables codes under ``table``'s contract.

    The tables are unnamed in TOML, so each is resolved by the anchor module
    only it may list. Resolving by name rather than by position keeps the
    guarantee the old "exactly one table" assertion gave: a floor set and the
    linter each address one unambiguous table.
    """
    anchor = TABLE_ANCHORS[table]
    matching = [
        override
        for override in enabling_overrides()
        if anchor in override.get("module", [])
    ]
    if len(matching) != 1:
        raise AssertionError(
            f"expected exactly one enable_error_code override table listing "
            f"{anchor} (the {table} table), found {len(matching)}"
        )
    return matching[0]


def module_path(dotted: str) -> Path:
    """Resolve a dotted name to its file.

    A package name such as ``agent_session_loop`` is the name mypy matches for
    that package's ``__init__.py``, so it resolves to the ``__init__`` rather
    than to a sibling module file that never existed.
    """
    base = REPO_ROOT / Path(*dotted.split("."))
    if base.is_dir():
        return base / "__init__.py"
    return base.with_suffix(".py")


def dotted_name(path: Path) -> str:
    """The name mypy matches for a module file, relative to the repository root.

    The inverse of :func:`module_path`. An ``__init__.py`` is named by its
    package -- mypy has no ``agent_session_loop.__init__`` to match -- so the
    file name is dropped rather than turned into a component.
    """
    relative = path.relative_to(REPO_ROOT)
    if path.name == "__init__.py":
        return ".".join(relative.parent.parts)
    return ".".join((*relative.parent.parts, relative.stem))


def package_modules() -> frozenset[str]:
    """Every module in the maintained packages, by the name mypy would match.

    Walked recursively so a subpackage added later is discovered rather than
    silently exempted, and filtered on ``__pycache__`` because a stale ``.pyc``
    directory holds no source.
    """
    return frozenset(
        dotted_name(path)
        for root in PACKAGE_ROOTS
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def subscript_base(node: ast.Subscript) -> str | None:
    """Name the generic being parameterized, or ``None`` when it is an expression."""
    if isinstance(node.value, ast.Name):
        return node.value.id
    if isinstance(node.value, ast.Attribute):
        return node.value.attr
    return None


def unparameterized_container(annotation: ast.expr) -> str | None:
    """Name the first bare container in this annotation, or ``None`` when typed.

    The search recurses through unions and generic arguments, because
    ``dict | None`` and ``list[dict]`` restore the ``Any``-valued payload the
    root-only check existed to reject just as surely as a bare ``dict`` does. A
    fully parameterized nesting such as ``list[dict[str, object]]`` is accepted:
    every container in it names what it holds.

    ``Literal[...]`` arguments are skipped and string annotations are never
    parsed -- both hold values rather than types, so ``Literal["dict"]`` is not
    a bare container.
    """
    if isinstance(annotation, ast.Name) and annotation.id in UNPARAMETERIZED_CONTAINERS:
        return annotation.id
    if (
        isinstance(annotation, ast.Attribute)
        and annotation.attr in UNPARAMETERIZED_CONTAINERS
    ):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        if subscript_base(annotation) == "Literal":
            return None
        return unparameterized_container(annotation.slice)
    if isinstance(annotation, ast.Tuple | ast.List):
        for element in annotation.elts:
            bare = unparameterized_container(element)
            if bare is not None:
                return bare
        return None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return unparameterized_container(annotation.left) or unparameterized_container(
            annotation.right
        )
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
                "asl_workbench",
                "tests",
                "skills/asl/scripts",
                "skills/session-resplit/scripts",
            ],
        )


class RatchetedModuleListTests(unittest.TestCase):
    """Both ratcheted module lists may only grow."""

    def test_both_named_tables_resolve_and_nothing_else_enables_codes(self) -> None:
        """Named lookup only stays unambiguous while there are no other tables.

        A third ``enable_error_code`` table would be a contract nobody wrote
        down: the linter would skip it and no floor set would guard it.
        """
        resolved = [ratcheted_override(table) for table in REQUIRED_MODULES]

        self.assertEqual(len(enabling_overrides()), len(resolved))

    def test_the_two_tables_are_disjoint(self) -> None:
        """A module on both would be linted and not linted at once."""
        boundary = set(ratcheted_override(BOUNDARY_TABLE)["module"])
        checked = set(ratcheted_override(CHECKED_TABLE)["module"])

        self.assertEqual(boundary & checked, set())

    def test_every_ratcheted_module_is_still_on_its_list(self) -> None:
        for table, required in REQUIRED_MODULES.items():
            listed = set(ratcheted_override(table)["module"])
            missing = required - listed
            with self.subTest(table=table):
                self.assertEqual(
                    missing,
                    set(),
                    f"a module left the {table} enable_error_code list; "
                    f"restore {sorted(missing)}",
                )

    def test_every_package_module_is_ratcheted(self) -> None:
        """A new package module must join a table rather than inherit the debt.

        The floor sets above only stop a *listed* module from leaving. Nothing
        made a module that never appeared on either table an error, so a module
        added tomorrow would stay under the globally disabled ``arg-type``,
        ``union-attr`` and ``index`` codes for as long as nobody noticed. This
        derives the tree instead of trusting a list, and requires exactly one
        table per module: neither is a home a module can be missing from, and
        two would mean it is linted and not linted at once.
        """
        tables_by_module: dict[str, list[str]] = {
            dotted: [] for dotted in package_modules()
        }
        for table in REQUIRED_MODULES:
            for dotted in ratcheted_override(table)["module"]:
                if dotted in tables_by_module:
                    tables_by_module[dotted].append(table)

        for dotted, tables in sorted(tables_by_module.items()):
            with self.subTest(module=dotted):
                self.assertEqual(
                    len(tables),
                    1,
                    f"{dotted} is on {len(tables)} enable_error_code tables; add "
                    f"it to {BOUNDARY_TABLE} if it meets the extraction rules, "
                    f"otherwise to {CHECKED_TABLE}",
                )

    def test_every_listed_module_re_enables_all_three_codes(self) -> None:
        for table in REQUIRED_MODULES:
            override = ratcheted_override(table)
            with self.subTest(table=table):
                self.assertEqual(
                    set(override["enable_error_code"]), set(RATCHETED_CODES)
                )

    def test_every_listed_module_exists(self) -> None:
        for table in REQUIRED_MODULES:
            for dotted in ratcheted_override(table)["module"]:
                with self.subTest(table=table, module=dotted):
                    self.assertTrue(module_path(dotted).is_file())

    def test_the_lists_stay_sorted_so_additions_do_not_collide(self) -> None:
        for table in REQUIRED_MODULES:
            listed = list(ratcheted_override(table)["module"])
            with self.subTest(table=table):
                self.assertEqual(listed, sorted(listed))

    def test_the_required_sets_name_only_real_modules(self) -> None:
        for table, required in REQUIRED_MODULES.items():
            for dotted in sorted(required):
                with self.subTest(table=table, module=dotted):
                    self.assertTrue(module_path(dotted).is_file())

    def test_each_anchor_is_on_the_floor_of_the_table_it_names(self) -> None:
        """An anchor that could be moved off its table would rename it."""
        for table, anchor in TABLE_ANCHORS.items():
            with self.subTest(table=table):
                self.assertIn(anchor, REQUIRED_MODULES[table])


class UnparameterizedContainerTests(unittest.TestCase):
    """The bare-container rule reads the whole annotation, not just its root.

    Before this, only the root node was inspected, so ``dict | None`` and
    ``list[dict]`` passed the linter while restoring exactly the ``Any``-valued
    payload it exists to reject.
    """

    def bare(self, source: str) -> str | None:
        return unparameterized_container(ast.parse(source, mode="eval").body)

    def test_a_root_container_is_still_named(self) -> None:
        self.assertEqual(self.bare("dict"), "dict")
        self.assertEqual(self.bare("collections.abc.Mapping"), "Mapping")

    def test_a_union_member_is_reached(self) -> None:
        self.assertEqual(self.bare("dict | None"), "dict")
        self.assertEqual(self.bare("str | list"), "list")

    def test_a_generic_argument_is_reached(self) -> None:
        self.assertEqual(self.bare("list[dict]"), "dict")
        self.assertEqual(self.bare("dict[str, list]"), "list")
        self.assertEqual(self.bare("Callable[[int], dict]"), "dict")
        self.assertEqual(self.bare("Callable[..., dict]"), "dict")

    def test_a_fully_parameterized_nesting_is_accepted(self) -> None:
        self.assertIsNone(self.bare("list[dict[str, object]]"))
        self.assertIsNone(self.bare("dict[str, list[str]] | None"))
        self.assertIsNone(
            self.bare("Callable[[Mapping[str, Any]], dict[str, Any]] | None")
        )

    def test_literal_arguments_are_values_rather_than_types(self) -> None:
        """``Literal["dict"]`` names a string, not a container."""
        self.assertIsNone(self.bare('Literal["dict"]'))
        self.assertIsNone(self.bare('Literal["dict", "list"] | None'))

    def test_a_string_annotation_is_left_alone(self) -> None:
        """Forward references are not parsed, so they cannot be misread."""
        self.assertIsNone(self.bare('"dict"'))


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
