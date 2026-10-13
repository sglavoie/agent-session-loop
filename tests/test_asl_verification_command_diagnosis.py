"""Command diagnosis, scope diagnosis, and module probe tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from agent_session_loop import (
    verification,
)
from tests.support_asl_verification import VerificationExecutorFixtures


class CommandDiagnosisTests(VerificationExecutorFixtures):
    def test_diagnose_command_flags_missing_bare_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("pytest", "-q"))
            diagnostic = verification.diagnose_command(
                check, repo_root=Path(tmp), which=lambda _name: None
            )

            self.assertIsNotNone(diagnostic)
            self.assertEqual("executable-not-found", diagnostic.reason)
            self.assertIn("pytest", diagnostic.detail)

    def test_diagnose_command_skips_path_bearing_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command((".venv/bin/tool", "run"))
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda _name: None,
            )

            self.assertIsNone(diagnostic)

    def test_diagnose_command_flags_missing_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-m", "pytest", "-q"))
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=lambda *_a, **_k: False,
            )

            self.assertIsNotNone(diagnostic)
            self.assertEqual("module-not-importable", diagnostic.reason)
            self.assertIn("pytest", diagnostic.detail)

    def test_diagnose_command_passes_present_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-m", "unittest"))
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=lambda *_a, **_k: True,
            )

            self.assertIsNone(diagnostic)

    def test_diagnose_command_fails_open_on_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-m", "pytest"))
            # A probe that returns the fail-open sentinel (``None``) yields no
            # finding even when the module might otherwise look absent.
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=lambda *_a, **_k: None,
            )

            self.assertIsNone(diagnostic)

    def test_diagnose_command_ignores_non_module_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-c", "print(1)"))
            probe_calls: list[tuple[object, ...]] = []

            def _record(interpreter: str, module: str, **_k: object) -> bool:
                probe_calls.append((interpreter, module))
                return False

            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=_record,
            )

            self.assertIsNone(diagnostic)
            self.assertEqual([], probe_calls)

    def test_diagnose_command_none_for_runnable_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-c", "import sys; sys.exit(0)"))
            diagnostic = verification.diagnose_command(check, repo_root=Path(tmp))

            self.assertIsNone(diagnostic)

    def test_diagnose_command_skips_when_repo_root_none(self) -> None:
        check = self._command(("pytest",))
        diagnostic = verification.diagnose_command(
            check, repo_root=None, which=lambda _name: None
        )

        self.assertIsNone(diagnostic)

    def test_diagnose_command_scope_flags_check_all(self) -> None:
        check = self._command(("./operator", "health", "check-all"))

        diagnostic = verification.diagnose_command_scope(check)

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual("operator-health-check-all", diagnostic.reason)
        self.assertIn("forbidden", diagnostic.detail)

    def test_diagnose_command_scope_flags_unscoped_health(self) -> None:
        check = self._command(("./operator", "health", "test", "--resume"))

        diagnostic = verification.diagnose_command_scope(check)

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual("operator-health-unscoped", diagnostic.reason)
        self.assertIn("package target flags", diagnostic.detail)

    def test_diagnose_command_scope_allows_scoped_health(self) -> None:
        check = self._command(
            ("./operator", "health", "lint", "--core-oidc", "--resume")
        )

        diagnostic = verification.diagnose_command_scope(check)

        self.assertIsNone(diagnostic)

    def test_default_module_probe_detects_present_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            present = verification._default_module_probe(
                sys.executable, "unittest", cwd=repo_root
            )
            missing = verification._default_module_probe(
                sys.executable,
                "definitely_not_a_real_module_xyz",
                cwd=repo_root,
            )

            self.assertIs(True, present)
            self.assertIs(False, missing)

    def test_default_module_probe_detects_missing_dotted_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = verification._default_module_probe(
                sys.executable,
                "definitely_not_a_real_module_xyz.submodule",
                cwd=Path(tmp),
            )
            self.assertIs(False, outcome)

    def test_default_module_probe_detects_present_dotted_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = verification._default_module_probe(
                sys.executable, "importlib.util", cwd=Path(tmp)
            )
            self.assertIs(True, outcome)
