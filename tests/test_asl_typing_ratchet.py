"""The typing ratchet: repository-wide suppressions may only shrink.

The repository still hides nine mypy error codes globally, because clearing
them needs container and generic annotations across a 125-file untyped tree
rather than defect fixes. That baseline is a debt, and debt without a ratchet
grows: a new module lands untyped, a tenth code joins the list to keep the
gate green, and the extraction program's typed boundaries stop being a
boundary at all.

These tests are that ratchet. ``pyproject.toml`` carries *two*
``enable_error_code`` override tables, under two contracts:

* **Boundary** -- mypy codes *and* the linter below: annotated public
  signatures, parameterized containers, no ``type: ignore``, no import of the
  runner. That is extraction discipline, which is meaningful for a package
  boundary and meaningless for a test module or for a package module whose job
  is to drive the runner.
* **Checked** -- the same three mypy codes and nothing else. Where a module
  lands when it is clean under mypy but cannot meet the extraction rules.

Neither table can name itself: mypy rejects an extra key in an overrides table
as an unrecognized option, so each is looked up by an anchor module only that
table may list (see ``TABLE_ANCHORS``).

Together the tests assert five things a future session cannot quietly undo:

* the repository-wide ``disable_error_code`` list may only *shrink*, and no
  per-module override -- in either table -- may add a suppression of its own;
* both tables re-enable all three codes, list only modules that exist, and stay
  sorted and disjoint;
* each table's module list may only *grow*, measured against its own floor set
  (``REQUIRED_RATCHETED_MODULES``, ``REQUIRED_CHECKED_MODULES``);
* every module in the package appears on exactly one of the two tables, so a
  module added tomorrow cannot sit under the global suppressions unnoticed --
  the floors only stop listed modules from leaving;
* every module on the **Boundary** table keeps its durable-boundary payloads
  typed under the linter rules above.

That last check is a linter, not a mypy run: it fails on the *shape* mypy
would have to trust, so a public entry point that takes a bare ``dict`` is
caught even though ``dict`` is a perfectly valid annotation. The bare-container
rule reads the whole annotation rather than its root, because ``dict | None``
and ``list[dict]`` restore the untyped payload just as surely. It applies to
the Boundary table only.
"""

from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_ROOT = REPO_ROOT / "agent_session_loop"

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

#: The codes a ratcheted module opts back in to. All three are ways a wrong
#: value reaches a durable record instead of raising at the call site.
RATCHETED_CODES = frozenset({"arg-type", "union-attr", "index"})

#: The two override tables. An overrides table has no name key -- mypy rejects
#: one as an unrecognized option -- so each is named by an anchor module that
#: only that table may list. The anchors are load-bearing: swap a table's
#: contract and the lookup below stops finding it.
BOUNDARY_TABLE = "boundary"
CHECKED_TABLE = "checked"
TABLE_ANCHORS = {
    BOUNDARY_TABLE: "agent_session_loop.artifacts",
    CHECKED_TABLE: "agent_session_loop.compact_state",
}

#: The package modules that meet the boundary contract: clean under all three
#: codes and clean under the linter below. This set is a floor: every module
#: that becomes clean adds to it, and nothing may leave it. A module removed
#: from ``pyproject.toml`` fails here rather than silently rejoining the
#: untyped baseline.
REQUIRED_RATCHETED_MODULES = frozenset(
    {
        "agent_session_loop",
        "agent_session_loop.__main__",
        "agent_session_loop.agent_mux_contract",
        "agent_session_loop.agent_mux_delivery",
        "agent_session_loop.agent_mux_pairing",
        "agent_session_loop.agent_mux_projection",
        "agent_session_loop.artifacts",
        "agent_session_loop.assurance_catchup",
        "agent_session_loop.assurance_cli",
        "agent_session_loop.assurance_draft",
        "agent_session_loop.assurance_ledger",
        "agent_session_loop.assurance_models",
        "agent_session_loop.assurance_projection",
        "agent_session_loop.assurance_reconcile",
        "agent_session_loop.assurance_report",
        "agent_session_loop.audit_replay",
        "agent_session_loop.authoring",
        "agent_session_loop.baseline",
        "agent_session_loop.baseline_cache",
        "agent_session_loop.bridge",
        "agent_session_loop.claude_guidance_hook",
        "agent_session_loop.claude_stream",
        "agent_session_loop.cli",
        "agent_session_loop.cli_common",
        "agent_session_loop.cli_trace",
        "agent_session_loop.clock",
        "agent_session_loop.codex_app_server",
        "agent_session_loop.command_guard",
        "agent_session_loop.commit_groups",
        "agent_session_loop.commit_message",
        "agent_session_loop.commit_settlement",
        "agent_session_loop.config",
        "agent_session_loop.contracts",
        "agent_session_loop.control_cli",
        "agent_session_loop.control_queue",
        "agent_session_loop.control_signal",
        "agent_session_loop.diagnostics_export",
        "agent_session_loop.durability",
        "agent_session_loop.events",
        "agent_session_loop.external_write_evidence",
        "agent_session_loop.formatting",
        "agent_session_loop.guard_pretool_hook",
        "agent_session_loop.guards",
        "agent_session_loop.guidance_delivery",
        "agent_session_loop.handoff_correction",
        "agent_session_loop.handoff_guards",
        "agent_session_loop.harness_compatibility",
        "agent_session_loop.harness_failures",
        "agent_session_loop.harnesses",
        "agent_session_loop.install_verify",
        "agent_session_loop.intervention",
        "agent_session_loop.literals",
        "agent_session_loop.live_verification",
        "agent_session_loop.mailbox",
        "agent_session_loop.mailbox_cli",
        "agent_session_loop.mailbox_rendering",
        "agent_session_loop.maintenance",
        "agent_session_loop.metrics",
        "agent_session_loop.model_availability",
        "agent_session_loop.overseer_discovery",
        "agent_session_loop.overseer_isolation",
        "agent_session_loop.overseer_policy_client",
        "agent_session_loop.overseer_store",
        "agent_session_loop.pause_policy",
        "agent_session_loop.plans",
        "agent_session_loop.preflight",
        "agent_session_loop.prepare",
        "agent_session_loop.process_tree",
        "agent_session_loop.progress_cli",
        "agent_session_loop.progress_repair",
        "agent_session_loop.qna",
        "agent_session_loop.readiness_gate",
        "agent_session_loop.repository_evidence",
        "agent_session_loop.repository_intent",
        "agent_session_loop.repository_registration",
        "agent_session_loop.repository_scope",
        "agent_session_loop.responses",
        "agent_session_loop.retention",
        "agent_session_loop.retry_hints",
        "agent_session_loop.review",
        "agent_session_loop.review_continuation",
        "agent_session_loop.review_cycle",
        "agent_session_loop.review_delta",
        "agent_session_loop.review_guard",
        "agent_session_loop.review_repair",
        "agent_session_loop.review_session_body",
        "agent_session_loop.review_session_contract",
        "agent_session_loop.review_transitions",
        "agent_session_loop.run_commit",
        "agent_session_loop.run_completion",
        "agent_session_loop.run_contention",
        "agent_session_loop.run_control",
        "agent_session_loop.run_coordination",
        "agent_session_loop.run_doctor",
        "agent_session_loop.run_entry",
        "agent_session_loop.run_errors",
        "agent_session_loop.run_flags",
        "agent_session_loop.run_inputs",
        "agent_session_loop.run_loop",
        "agent_session_loop.run_outcome",
        "agent_session_loop.run_payloads",
        "agent_session_loop.run_persistence",
        "agent_session_loop.run_prompts",
        "agent_session_loop.run_reporting",
        "agent_session_loop.run_selection",
        "agent_session_loop.run_termination",
        "agent_session_loop.runner",
        "agent_session_loop.session_overview",
        "agent_session_loop.session_verification",
        "agent_session_loop.startup_context",
        "agent_session_loop.state",
        "agent_session_loop.supervision_liveness",
        "agent_session_loop.supervisor_audit",
        "agent_session_loop.supervisor_guide",
        "agent_session_loop.tail",
        "agent_session_loop.task_packet",
        "agent_session_loop.transitions",
        "agent_session_loop.turn_dispositions",
        "agent_session_loop.turn_execution",
        "agent_session_loop.turn_postflight",
        "agent_session_loop.turn_preflight",
        "agent_session_loop.turn_preparation",
        "agent_session_loop.turn_settlement",
        "agent_session_loop.verification",
        "agent_session_loop.verification_transitions",
        "agent_session_loop.workbench_server",
        "agent_session_loop.workspace_evidence",
        "agent_session_loop.workspaces",
        "agent_session_loop.write_guards",
    }
)

#: The floor for the Checked table, same rule as the set above: clean under all
#: three codes, but held off the Boundary table by the linter rather than by
#: mypy. Seeded with the two package modules that import the runner, which is a
#: rule neither of them could ever satisfy.
REQUIRED_CHECKED_MODULES = frozenset(
    {
        "agent_session_loop.compact_state",
        "agent_session_loop.workbench",
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
    """Every module in the package, by the dotted name mypy would match.

    Walked recursively so a subpackage added later is discovered rather than
    silently exempted, and filtered on ``__pycache__`` because a stale ``.pyc``
    directory holds no source.
    """
    return frozenset(
        dotted_name(path)
        for path in PACKAGE_ROOT.rglob("*.py")
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
