"""Capability matrix and intentional-change register for the Workbench foundations.

**A complete inventory with an owner per row.** The matrix in
:mod:`asl_workbench.capabilities` is only worth having if it cannot drift, so
the Workbench's HTTP routes and CLI entry points are read out of the live code
and compared against it in both directions -- a new route without a row fails,
and a row for a route that no longer exists fails too.

**Identified regression coverage.** Every row names the tests holding it, as
``alias:path`` references that are resolved against the filesystem, or names an
explicit gap: a row claiming coverage it does not have is the failure this
program exists to prevent.

The two deliberate behavior changes are registered in
:mod:`asl_workbench.changes`; the register is checked against the matrix here.
The portable-package boundary lives in
``tests.test_workbench_foundations_boundary`` and the local API
characterization in ``tests.test_workbench_foundations_api``.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import unittest

from asl_workbench import capabilities, changes
from tests.workbench_foundations_support import REPO_ROOT
from tests.workbench_support import (
    STATIC_ROUTE_KEYS,
    workbench_api_routes,
    workbench_cli_entry_points,
)

# Integration rather than portable: it binds a loopback server and spawns
# a fresh interpreter to prove the portable package loads without ASL.
ASL_TEST_LANE = "integration"


class CapabilityMatrixTests(unittest.TestCase):
    """The matrix is complete, well-formed, and honest about coverage."""

    def test_the_matrix_covers_every_live_local_api_route(self) -> None:
        live = set(workbench_api_routes())
        recorded = set(capabilities.surface_keys(capabilities.SURFACE_WORKBENCH_HTTP))
        self.assertEqual(
            set(),
            live - recorded,
            "local API routes exist with no owner in the capability matrix; "
            "add a row naming the owner, authority, and regression coverage",
        )
        self.assertEqual(
            set(),
            recorded - live,
            "the matrix claims local API routes the server no longer serves",
        )

    def test_the_matrix_covers_the_static_asset_routes(self) -> None:
        # Served before the route table is consulted, so a reader of the table
        # cannot see them; they are enumerated instead and checked here.
        recorded = capabilities.surface_keys(capabilities.SURFACE_WORKBENCH_STATIC)
        self.assertEqual(set(STATIC_ROUTE_KEYS), set(recorded))

    def test_the_matrix_covers_every_workbench_cli_entry_point(self) -> None:
        live = set(workbench_cli_entry_points())
        recorded = set(capabilities.surface_keys(capabilities.SURFACE_WORKBENCH_CLI))
        self.assertEqual(
            set(),
            live - recorded,
            "CLI entry points exist with no owner in the capability matrix",
        )
        self.assertEqual(
            set(),
            recorded - live,
            "the matrix claims CLI entry points the parser does not expose",
        )

    def test_every_row_is_structurally_valid(self) -> None:
        problems = capabilities.validate_matrix(
            repo_root=REPO_ROOT,
            known_changes=changes.change_keys(),
        )
        self.assertEqual((), problems, "\n".join(problems))

    def test_every_row_names_exactly_one_owner_from_the_closed_vocabulary(
        self,
    ) -> None:
        # Ownership is the whole point of the matrix: the consolidation fails if
        # two implementations both survive, and it survives by every behavior
        # having one place it lives.
        for row in capabilities.CAPABILITIES:
            with self.subTest(capability=row.key):
                self.assertIn(row.owner, capabilities.OWNERS)
                self.assertIn(row.authority, capabilities.AUTHORITIES)

    def test_local_coverage_references_resolve_on_disk(self) -> None:
        for path in capabilities.local_coverage_paths():
            with self.subTest(coverage=path):
                self.assertTrue(
                    (REPO_ROOT / path).exists(),
                    f"matrix names coverage that does not exist: scripts:{path}",
                )

    def test_coverage_is_either_named_or_declared_missing(self) -> None:
        for row in capabilities.CAPABILITIES:
            with self.subTest(capability=row.key):
                self.assertTrue(
                    row.regression_coverage,
                    "a row with no coverage and no declared gap claims an "
                    "assurance nothing provides",
                )

    def test_workbench_browser_rows_say_which_kind_of_coverage_they_have(
        self,
    ) -> None:
        # There is a browser runner here now, but it drives one shell rather
        # than every surface, so this row-by-row claim is no longer uniform: a
        # row either names the spec that exercises it or declares that it is
        # still held by the documented smoke checklist. What must not happen is
        # a row doing both, which reads as covered while excusing itself, or
        # neither, which claims an assurance nothing provides.
        browser_rows = capabilities.by_surface(capabilities.SURFACE_WORKBENCH_BROWSER)
        self.assertTrue(browser_rows)
        driven_by_a_browser = False
        for row in browser_rows:
            with self.subTest(capability=row.key):
                specs = [
                    reference
                    for reference in row.regression_coverage
                    if reference.endswith(".spec.js")
                ]
                if specs:
                    driven_by_a_browser = True
                    self.assertIsNone(
                        row.coverage_gap,
                        "a row naming a spec cannot also claim the browser gap",
                    )
                else:
                    self.assertEqual(
                        capabilities.GAP_WORKBENCH_BROWSER, row.coverage_gap
                    )
        self.assertTrue(
            driven_by_a_browser,
            "the shell suite exists, so at least one row must name it",
        )

    def test_compatibility_adapters_are_recorded_where_they_exist(self) -> None:
        # The historic delete route is the clearest case: it must keep working
        # and it must not be a second archive implementation.
        alias = capabilities.capability("workbench-http:POST /api/plan/delete")
        self.assertTrue(alias.compatibility_adapters)
        self.assertEqual(capabilities.OWNER_PORTABLE, alias.owner)
        self.assertEqual(capabilities.AUTHORITY_ASL, alias.authority)

    def test_the_matrix_renders(self) -> None:
        rendered = capabilities.render_matrix()
        self.assertIn("# Capability and ownership matrix", rendered)
        for surface in capabilities.SURFACES:
            with self.subTest(surface=surface):
                self.assertIn(f"## {surface}", rendered)

    def test_the_entry_point_renders_markdown_or_json_and_refuses_anything_else(
        self,
    ) -> None:
        # The module is the inventory's only reader-facing surface, so an
        # unrecognized flag has to fail rather than silently print the default
        # view: a caller asking for a format it did not get would otherwise
        # parse Markdown as JSON and conclude the matrix was empty.
        for argv, expected in ((["--json"], "json"), ([], "markdown")):
            with self.subTest(argv=argv):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(0, capabilities.main(argv))
                written = stream.getvalue()
                if expected == "json":
                    decoded = json.loads(written)
                    self.assertEqual(len(capabilities.CAPABILITIES), decoded["total"])
                    self.assertEqual(
                        capabilities.summary_counts(), decoded["by_surface"]
                    )
                else:
                    self.assertIn("# Capability and ownership matrix", written)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as refusal,
        ):
            capabilities.main(["--not-a-flag"])
        self.assertEqual(2, refusal.exception.code)

    def test_unknown_lookups_refuse_rather_than_return_empty(self) -> None:
        with self.assertRaises(capabilities.MatrixError):
            capabilities.capability("workbench-http:GET /api/nope")
        with self.assertRaises(capabilities.MatrixError):
            capabilities.by_surface("not-a-surface")
        with self.assertRaises(capabilities.MatrixError):
            capabilities.coverage_paths("not-an-alias")


class IntentionalChangeRegisterTests(unittest.TestCase):
    """The two deliberate behavior changes are recorded before the work starts."""

    def test_exactly_the_two_planned_changes_are_registered(self) -> None:
        self.assertEqual(
            (
                changes.KEY_OBSERVATIONAL_READS,
                changes.KEY_EXPLICIT_ARCHIVE_CLEANUP,
            ),
            changes.change_keys(),
        )

    def test_every_record_is_structurally_valid(self) -> None:
        problems = changes.validate_changes(
            [row.key for row in capabilities.CAPABILITIES]
        )
        self.assertEqual((), problems, "\n".join(problems))

    def test_each_record_names_the_tests_that_will_have_to_be_rewritten(
        self,
    ) -> None:
        # A pinned test that fails is either a regression or one of these. The
        # reference has to name a real test method for that distinction to be
        # usable, so the method is looked up in the module's source.
        for record in changes.INTENTIONAL_CHANGES:
            for reference in record.pinned_coverage:
                with self.subTest(change=record.key, pinned=reference):
                    location, _, test_name = reference.partition("::")
                    alias, _, path = location.partition(":")
                    self.assertEqual(capabilities.LOCAL_COVERAGE_ALIAS, alias)
                    module_path = REPO_ROOT / path
                    self.assertTrue(module_path.exists(), f"missing {reference}")
                    self.assertTrue(test_name, "pin must name the test method")
                    tree = ast.parse(module_path.read_text(encoding="utf-8"))
                    defined = {
                        node.name
                        for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef)
                    }
                    self.assertIn(test_name, defined)

    def test_each_record_states_both_the_current_and_target_behavior(self) -> None:
        for record in changes.INTENTIONAL_CHANGES:
            with self.subTest(change=record.key):
                self.assertNotEqual(
                    record.current_behavior.strip(),
                    record.target_behavior.strip(),
                    "a change that does not change anything is not a change",
                )

    def test_capabilities_and_changes_agree_in_both_directions(self) -> None:
        claimed_by_rows = {
            change_key
            for row in capabilities.CAPABILITIES
            for change_key in row.intentional_changes
        }
        self.assertEqual(set(changes.change_keys()), claimed_by_rows)
        for record in changes.INTENTIONAL_CHANGES:
            for key in record.capability_keys:
                with self.subTest(change=record.key, capability=key):
                    row = capabilities.capability(key)
                    self.assertIn(
                        record.key,
                        row.intentional_changes,
                        f"{key} is listed under {record.key} but does not "
                        "point back at it",
                    )
        # And the reverse: a row claiming a change the record does not list
        # would make the register's capability_keys an incomplete index.
        for row in capabilities.CAPABILITIES:
            for change_key in row.intentional_changes:
                with self.subTest(capability=row.key, change=change_key):
                    self.assertIn(
                        row.key,
                        changes.intentional_change(change_key).capability_keys,
                    )

    def test_unknown_change_lookups_refuse(self) -> None:
        with self.assertRaises(changes.ChangeError):
            changes.intentional_change("no-such-change")


if __name__ == "__main__":
    unittest.main()
