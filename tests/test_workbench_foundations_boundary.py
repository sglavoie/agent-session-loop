"""Portable-package boundary checks for the unified Workbench foundations.

The portable package is checked statically over its source and then again in a
fresh interpreter with ``agent_session_loop`` blocked at ``sys.meta_path``. The
subprocess is the load-bearing part: this interpreter imported
``agent_session_loop`` before the first test ran, so an in-process check would
pass no matter what the package did. The Python floor the Pi's interpreter
imposes is held here as well.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
from tests.workbench_foundations_support import (
    FORBIDDEN_PACKAGE_NAME,
    PORTABLE_PACKAGE_NAME,
    REPO_ROOT,
    RUFF_PER_FILE_TARGET_TABLE,
    RUFF_PORTABLE_TARGET,
)

ASL_TEST_LANE = "integration"


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


if __name__ == "__main__":
    unittest.main()
