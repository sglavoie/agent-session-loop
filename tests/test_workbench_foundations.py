"""Foundations for the unified Workbench: inventory, ownership, boundaries.

Session 1 of the consolidation produces three things this module is the gate
for.

**A complete inventory with an owner per row.** The matrix in
:mod:`asl_workbench.capabilities` is only worth having if it cannot drift, so
the Workbench's HTTP routes and CLI entry points are read out of the live code
and compared against it in both directions -- a new route without a row fails,
and a row for a route that no longer exists fails too. The Companion's rows are
compared the same way against its real checkout when one is reachable.

**Identified regression coverage.** Every row names the tests holding it, as
``alias:path`` references that are resolved against the filesystem, or names an
explicit gap. The Workbench front end has no automated browser test in this
repository, and the tests here require that to be *stated* rather than papered
over: a row claiming coverage it does not have is the failure this session
exists to prevent.

**A portable package that loads without ASL.** Checked statically over the
package source and then again in a fresh interpreter with
``agent_session_loop`` blocked at ``sys.meta_path``. The subprocess is the part
that is actually load-bearing: this interpreter imported ``agent_session_loop``
before the first test ran, so an in-process check would pass no matter what the
package did.

The characterization tests at the end capture today's local API behavior over
isolated fixtures, including the two behaviors the program intends to change.
Those two are pinned *and* registered in :mod:`asl_workbench.changes`, so a
later session that breaks them can tell an expected failure from a regression.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from agent_session_loop import bridge, plans_files, workbench, workbench_archive
from asl_workbench import capabilities, changes
from asl_workbench.compatibility import (
    KIND_FORBIDDEN,
    KIND_THIRD_PARTY,
    MINIMUM_PYTHON_VERSION,
    REASON_BELOW_MINIMUM,
    REASON_SUPPORTED,
    REASON_UNSUPPORTED_SERIES,
    blocking_meta_path_source,
    check_boundary,
    check_runtime,
    external_imports,
    unsupported_syntax,
)
from tests.support import _http_request, _http_request_raw, write_plan
from tests.workbench_support import (
    STATIC_ROUTE_KEYS,
    WorkbenchAPIFixture,
    recording_server_factory,
    workbench_api_routes,
    workbench_cli_entry_points,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PORTABLE_PACKAGE_NAME = "asl_workbench"
FORBIDDEN_PACKAGE_NAME = "agent_session_loop"

#: The Ruff table that holds the portable package to the Pi's interpreter while
#: the rest of the repository stays on the newer one.
RUFF_PER_FILE_TARGET_TABLE = "[tool.ruff.per-file-target-version]"
RUFF_PORTABLE_TARGET = '"asl_workbench/**/*.py" = "py313"'


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


class PortableBoundaryTests(unittest.TestCase):
    """The portable package stays loadable on the Pi and free of ASL."""

    def test_the_package_passes_every_boundary_check(self) -> None:
        report = check_boundary()
        self.assertTrue(report.ok, "\n".join(report.problems()))

    def test_no_module_imports_the_worker_runtime_or_a_third_party_package(
        self,
    ) -> None:
        findings = external_imports()
        forbidden = [f.describe() for f in findings if f.kind == KIND_FORBIDDEN]
        third_party = [f.describe() for f in findings if f.kind == KIND_THIRD_PARTY]
        self.assertEqual(
            [],
            forbidden,
            "the portable package imported ASL's worker runtime; pass what it "
            "needs through a parameter or a seam instead",
        )
        self.assertEqual(
            [],
            third_party,
            "the Pi installs no wheels, so every import must be stdlib",
        )

    def test_the_import_scan_sees_imports_inside_function_bodies(self) -> None:
        # A deferred import is the way this boundary would realistically be
        # broken -- it survives a namespace check after a successful import --
        # so the scan has to be static and has to walk into bodies.
        with tempfile.TemporaryDirectory() as raw_tmp:
            fake = Path(raw_tmp) / PORTABLE_PACKAGE_NAME
            fake.mkdir()
            (fake / "__init__.py").write_text("", encoding="utf-8")
            (fake / "sneaky.py").write_text(
                "def go():\n    from agent_session_loop import workbench\n"
                "    return workbench\n",
                encoding="utf-8",
            )
            findings = external_imports(fake)
        self.assertEqual(1, len(findings))
        self.assertEqual(KIND_FORBIDDEN, findings[0].kind)
        self.assertEqual("asl_workbench.sneaky", findings[0].module)
        self.assertEqual(2, findings[0].line)

    def test_relative_imports_are_not_reported_as_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            fake = Path(raw_tmp) / PORTABLE_PACKAGE_NAME
            fake.mkdir()
            (fake / "__init__.py").write_text(
                "from . import other\nfrom .other import thing\n", encoding="utf-8"
            )
            (fake / "other.py").write_text("thing = 1\n", encoding="utf-8")
            self.assertEqual((), external_imports(fake))

    def test_the_package_imports_in_a_fresh_interpreter_with_asl_blocked(
        self,
    ) -> None:
        # The load proof. This interpreter already has agent_session_loop in
        # sys.modules, so the check runs in a subprocess where importing it
        # raises outright.
        program = blocking_meta_path_source() + (
            "import json\n"
            f"import {PORTABLE_PACKAGE_NAME} as package\n"
            f"from {PORTABLE_PACKAGE_NAME} import capabilities, changes\n"
            "print(json.dumps({\n"
            "    'rows': len(capabilities.CAPABILITIES),\n"
            "    'changes': len(changes.INTENTIONAL_CHANGES),\n"
            f"    'asl_loaded': any(\n"
            f"        name == {FORBIDDEN_PACKAGE_NAME!r}\n"
            f"        or name.startswith({FORBIDDEN_PACKAGE_NAME + '.'!r})\n"
            "        for name in sys.modules\n"
            "    ),\n"
            "    'exports': sorted(package.__all__),\n"
            "}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"the portable package did not load without ASL:\n{completed.stderr}",
        )
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["asl_loaded"])
        self.assertEqual(len(capabilities.CAPABILITIES), payload["rows"])
        self.assertEqual(len(changes.INTENTIONAL_CHANGES), payload["changes"])
        self.assertIn("check_boundary", payload["exports"])

    def test_the_blocking_finder_actually_blocks(self) -> None:
        # Otherwise the load proof above would pass by accident.
        program = blocking_meta_path_source() + (f"import {FORBIDDEN_PACKAGE_NAME}\n")
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("blocked by the portable boundary", completed.stderr)


class PythonVersionBoundaryTests(unittest.TestCase):
    """The portable package holds to the Pi's interpreter floor."""

    def test_the_floor_matches_the_companion_installers_own_check(self) -> None:
        # The installer on the Pi refuses anything below 3.13. A higher floor
        # here would produce modules the installer accepts and cannot import.
        self.assertEqual((3, 13), MINIMUM_PYTHON_VERSION)

    def test_versions_are_judged_against_the_floor_and_the_series(self) -> None:
        cases = (
            ((3, 12, 9), REASON_BELOW_MINIMUM),
            ((3, 13, 0), REASON_SUPPORTED),
            ((3, 14, 1), REASON_SUPPORTED),
            ((4, 0, 0), REASON_UNSUPPORTED_SERIES),
        )
        for version, reason in cases:
            with self.subTest(version=version):
                support = check_runtime(version)
                self.assertEqual(reason, support.reason)
                self.assertEqual(reason == REASON_SUPPORTED, support.supported)

    def test_a_refusal_says_which_version_and_which_boundary(self) -> None:
        described = check_runtime((3, 9)).describe()
        self.assertIn("3.9", described)
        self.assertIn("3.13", described)

    def test_the_running_interpreter_is_supported(self) -> None:
        self.assertTrue(check_runtime().supported)

    def test_no_module_needs_newer_syntax_than_the_floor(self) -> None:
        findings = unsupported_syntax()
        self.assertEqual([], [finding.describe() for finding in findings])

    def test_syntax_newer_than_the_floor_is_reported(self) -> None:
        # Guards the check itself: if feature_version stopped being honoured,
        # the test above would pass on an empty promise.
        with tempfile.TemporaryDirectory() as raw_tmp:
            fake = Path(raw_tmp) / PORTABLE_PACKAGE_NAME
            fake.mkdir()
            # PEP 695 type parameter syntax; 3.12 and later parse it, so a
            # source-level floor below that is what makes this fail.
            (fake / "__init__.py").write_text("", encoding="utf-8")
            (fake / "newer.py").write_text("type Alias = int\n", encoding="utf-8")
            findings = unsupported_syntax(fake)
            self.assertEqual((), findings)
            # And a construct no released Python parses is caught.
            (fake / "newer.py").write_text("def f(:\n    pass\n", encoding="utf-8")
            findings = unsupported_syntax(fake)
        self.assertEqual(1, len(findings))
        self.assertEqual("asl_workbench.newer", findings[0].module)

    def test_ruff_pins_the_portable_package_to_the_floor(self) -> None:
        # The linter is the gate a developer hits first, so the floor is
        # configured there as well as checked here.
        config = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(RUFF_PER_FILE_TARGET_TABLE, config)
        self.assertIn(RUFF_PORTABLE_TARGET, config)


class LocalAPICharacterizationTests(WorkbenchAPIFixture, unittest.TestCase):
    """Today's local API behavior, captured before anything is extracted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))
        self.plan_dir = self.plans_root / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)

    def test_health_answers_without_a_token(self) -> None:
        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/health")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual("workbench", body["command"])

    def test_the_queue_answers_with_the_triage_fields_the_front_end_reads(
        self,
    ) -> None:
        registry = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="Alpha plan",
        )
        workbench.save_registry(registry, self.registry_path)
        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/queue")
        self.assertEqual(200, status)
        payload = body["plans"][0]
        for field in ("urgency_rank", "urgency_group", "activity_label", "plan_dir"):
            with self.subTest(field=field):
                self.assertIn(field, payload)

    def test_a_mutation_without_the_bearer_token_is_refused(self) -> None:
        server = self.start_workbench_server(token="fixed-token")
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["reason"])
        # And the refusal is a refusal: nothing was written.
        self.assertEqual((), workbench.load_registry(self.registry_path).pins)

    def test_the_same_mutation_with_the_token_is_accepted(self) -> None:
        server = self.start_workbench_server(token="fixed-token")
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self.auth_headers(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(1, len(workbench.load_registry(self.registry_path).pins))

    def test_an_unknown_path_is_404_and_a_wrong_method_is_405(self) -> None:
        # The vocabulary matters to the front end: 404 means "no such route",
        # 405 carries the methods that route does answer.
        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/nonexistent")
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/queue",
            method="POST",
            body={},
            headers=self.auth_headers(server),
        )
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", body["reason"])
        self.assertIn("GET", body["allow"])

    def test_the_shell_is_served_as_html_without_a_token(self) -> None:
        # Raw rather than JSON: the shell is the one 200 that is not a payload.
        server = self.start_workbench_server(token="fixed-token")
        status, body, headers = _http_request_raw(f"{self.base_url(server)}/")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"<html", body.lower())

    def test_reading_the_archive_reports_an_expired_entry_and_keeps_it(
        self,
    ) -> None:
        # This test used to assert the opposite, as characterization of the
        # behavior the program intended to change: the read was what deleted.
        # Session 8 landed the change, so the scenario is unchanged and the
        # outcome is inverted -- which is exactly what the register said would
        # happen, and why a session watching it flip could tell it from a
        # regression.
        record = changes.intentional_change(changes.KEY_OBSERVATIONAL_READS)
        self.assertIn("cleanup_state", record.current_behavior)
        self.assertIn("no longer exists", record.current_behavior)
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        meta_path = (
            workbench_archive.archive_entry_dir(result.archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["archived_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")

        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/archive")
        self.assertEqual(200, status)
        self.assertEqual([result.archive_id], body["archive_cleanup"]["candidate_ids"])
        self.assertEqual(
            [result.archive_id], [entry["archive_id"] for entry in body["entries"]]
        )
        self.assertEqual("eligible", body["entries"][0]["cleanup_state"])
        self.assertTrue(
            workbench_archive.archive_entry_dir(
                result.archive_id, self.state_dir
            ).exists()
        )

    def test_permanently_emptying_the_archive_requires_a_matching_count(
        self,
    ) -> None:
        # The confirmation contract the other intentional change keeps.
        record = changes.intentional_change(changes.KEY_EXPLICIT_ARCHIVE_CLEANUP)
        self.assertIn("confirm_count", record.current_behavior)
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self.start_workbench_server()
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/archive/delete",
            method="POST",
            body={"archive_ids": [result.archive_id], "confirm_count": 2},
            headers=self.auth_headers(server),
        )
        self.assertEqual(400, status)
        self.assertEqual(
            (result.archive_id,),
            tuple(
                entry.archive_id
                for entry in workbench_archive.list_archive_entries(
                    self.state_dir
                ).entries
            ),
            "a mismatched confirmation must leave the archive untouched",
        )
        self.assertTrue(body["error"])

    def test_archiving_one_plan_requires_the_plan_dir_be_confirmed(self) -> None:
        server = self.start_workbench_server()
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir / "wrong"),
            },
            headers=self.auth_headers(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_the_historic_delete_route_archives_instead_of_deleting(self) -> None:
        # The compatibility adapter the matrix records: same route, same
        # confirmation, and the plan is recoverable afterwards.
        server = self.start_workbench_server()
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self.auth_headers(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertFalse(self.canonical_plan_dir.exists())
        entries = workbench_archive.list_archive_entries(self.state_dir).entries
        self.assertEqual(1, len(entries))


class BridgeDeepLinkTests(unittest.TestCase):
    """The tokenized deep link the bridge hands out, pinned as a contract."""

    def test_the_token_rides_in_the_query_and_the_plan_in_the_fragment(
        self,
    ) -> None:
        # Split deliberately: the fragment never reaches the server, so the
        # token cannot live there; the front end routes on the fragment, so the
        # plan selection cannot live in the query.
        factory, built = recording_server_factory()
        lifecycle = bridge.WorkbenchLifecycle(server_factory=factory)
        self.addCleanup(lifecycle.stop)
        url = lifecycle.url("/plans/alpha", bridge.WORKBENCH_FOCUS_REPLY)

        parsed = urllib.parse.urlparse(url)
        self.assertEqual("http", parsed.scheme)
        self.assertEqual("127.0.0.1", parsed.hostname)
        self.assertEqual("/", parsed.path)
        self.assertEqual(
            {"token": [built[0].token]}, urllib.parse.parse_qs(parsed.query)
        )
        route, _, fragment_query = parsed.fragment.partition("?")
        self.assertEqual("plan", route)
        self.assertEqual(
            {"dir": ["/plans/alpha"], "focus": [bridge.WORKBENCH_FOCUS_REPLY]},
            urllib.parse.parse_qs(fragment_query),
        )

    def test_exactly_one_server_is_started_however_many_links_are_asked_for(
        self,
    ) -> None:
        factory, built = recording_server_factory()
        lifecycle = bridge.WorkbenchLifecycle(server_factory=factory)
        self.addCleanup(lifecycle.stop)
        self.assertEqual([], built, "the server must start lazily")
        lifecycle.url("/plans/alpha", bridge.WORKBENCH_FOCUS_ACTIVE)
        lifecycle.url("/plans/beta", bridge.WORKBENCH_FOCUS_EVENTS)
        self.assertEqual(1, len(built))
        self.assertEqual(0, built[0].kwargs["port"], "must bind ephemerally")
        self.assertEqual("127.0.0.1", built[0].kwargs["host"])

    def test_the_bridge_owned_server_stops_with_the_bridge(self) -> None:
        factory, built = recording_server_factory()
        lifecycle = bridge.WorkbenchLifecycle(server_factory=factory)
        lifecycle.url("/plans/alpha", bridge.WORKBENCH_FOCUS_ACTIVE)
        lifecycle.stop()
        self.assertEqual(1, built[0].shutdown_calls)

    def test_the_focus_values_the_matrix_relies_on_exist(self) -> None:
        for focus in (
            bridge.WORKBENCH_FOCUS_REPLY,
            bridge.WORKBENCH_FOCUS_VERIFICATION,
            bridge.WORKBENCH_FOCUS_ACTIVE,
            bridge.WORKBENCH_FOCUS_EVENTS,
        ):
            with self.subTest(focus=focus):
                self.assertTrue(focus)


if __name__ == "__main__":
    unittest.main()
