"""Preflight duplicate-command and command-runnability tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    contracts,
    plans_session_ids,
    preflight,
    preflight_hint_resolution,
    preflight_models,
    preflight_verification_checks,
)
from tests.support import (
    _contract_block,
    _write_loop_ready_plan,
)


class PreflightCommandRunnabilityTests(unittest.TestCase):
    def _contract(
        self,
        session_id: str,
        *checks: contracts.VerificationCheck,
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=plans_session_ids.parse_session_id(session_id),
            done_when=("recipe runs",),
            verify=contracts.VerifyRecipe(checks=tuple(checks)),
        )

    def _command(
        self,
        name: str,
        argv: tuple[str, ...],
    ) -> contracts.CommandCheck:
        return contracts.CommandCheck(
            name=name,
            argv=argv,
            timeout_seconds=300,
        )

    def test_duplicate_command_and_cwd_emits_yellow(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                self._command("first", ("python3", "-m", "unittest")),
                self._command("second", ("python3", "-m", "unittest")),
            )
        }

        findings = preflight_verification_checks._check_duplicate_verification_commands(
            contracts_by_session
        )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("yellow", finding.severity)
        self.assertEqual("verification-duplicate", finding.category)
        self.assertIn("'first' and 'second'", finding.message)

    def test_same_argv_in_different_cwds_is_not_duplicate(self) -> None:
        command = ("python3", "-m", "unittest")
        first = contracts.CommandCheck(
            name="first", argv=command, timeout_seconds=300, cwd="one"
        )
        second = contracts.CommandCheck(
            name="second", argv=command, timeout_seconds=300, cwd="two"
        )
        contracts_by_session = {"1": self._contract("1", first, second)}

        self.assertEqual(
            (),
            preflight_verification_checks._check_duplicate_verification_commands(
                contracts_by_session
            ),
        )

    def test_same_argv_in_different_repositories_is_not_duplicate(self) -> None:
        command = ("python3", "-m", "unittest")
        first = contracts.CommandCheck(
            name="first", argv=command, timeout_seconds=300, repo="SVC"
        )
        second = contracts.CommandCheck(
            name="second", argv=command, timeout_seconds=300, repo="OAK"
        )

        self.assertEqual(
            (),
            preflight_verification_checks._check_duplicate_verification_commands(
                {"1": self._contract("1", first, second)}
            ),
        )

    def test_default_and_dot_cwd_are_duplicate(self) -> None:
        command = ("python3", "-m", "unittest")
        first = contracts.CommandCheck(name="first", argv=command, timeout_seconds=300)
        second = contracts.CommandCheck(
            name="second", argv=command, timeout_seconds=300, cwd="."
        )
        contracts_by_session = {"1": self._contract("1", first, second)}

        findings = preflight_verification_checks._check_duplicate_verification_commands(
            contracts_by_session
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("verification-duplicate", findings[0].category)

    def test_missing_bare_executable_emits_single_red(self) -> None:
        contracts_by_session = {
            "1": self._contract("1", self._command("run-1", ("pytest", "-q")))
        }
        findings = preflight_verification_checks._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: None,
        )
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("red", finding.severity)
        self.assertEqual("command-not-runnable", finding.category)
        self.assertEqual("1", finding.session_id)
        self.assertIn("executable is not on PATH", finding.message)
        self.assertIn("pytest", finding.detail)

    def test_missing_command_cwd_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = contracts.CommandCheck(
                name="run-1",
                argv=("python3", "-m", "unittest"),
                timeout_seconds=300,
                cwd="no-such-dir",
            )
            findings = preflight_verification_checks._check_command_runnability(
                {"1": self._contract("1", check)},
                tmp,
                which=lambda name: f"/usr/bin/{name}",
                module_probe=lambda interpreter, module, *, cwd: True,
            )
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("red", finding.severity)
        self.assertEqual("command-not-runnable", finding.category)
        self.assertIn("cwd does not exist", finding.message)
        self.assertIn("no-such-dir", finding.detail)

    def test_legacy_escaping_cwd_is_a_malformed_contract_on_its_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = contracts.CommandCheck(
                name="esc", argv=("true",), timeout_seconds=300, cwd=".."
            )
            contracts_by_session = {"1": self._contract("1", check)}
            session_paths = {"1": f"{tmp}/session-1.md"}

            findings = preflight_verification_checks._check_command_runnability(
                contracts_by_session,
                tmp,
                which=lambda name: f"/usr/bin/{name}",
                session_paths=session_paths,
            )
            # The footprint already reports this very target as escaping.
            deduplicated = preflight_verification_checks._check_command_runnability(
                contracts_by_session,
                tmp,
                which=lambda name: f"/usr/bin/{name}",
                session_paths=session_paths,
                footprint_escapes=frozenset({".."}),
            )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("malformed-contract", finding.category)
        self.assertEqual(f"{tmp}/session-1.md", finding.path)
        self.assertIn("escapes the bound root", finding.detail)
        self.assertEqual((), deduplicated)

    def test_missing_module_emits_single_yellow(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                self._command("run-1", ("python3", "-m", "pytest", "tests")),
            )
        }
        findings = preflight_verification_checks._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: f"/usr/bin/{name}",
            module_probe=lambda interpreter, module, *, cwd: False,
        )
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("yellow", finding.severity)
        self.assertEqual("command-not-runnable", finding.category)
        self.assertIn("module is not importable", finding.message)
        self.assertIn("pytest", finding.detail)

    def test_runnable_command_emits_no_finding(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                self._command("run-1", ("python3", "-m", "unittest", "tests")),
            )
        }
        findings = preflight_verification_checks._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: f"/usr/bin/{name}",
            module_probe=lambda interpreter, module, *, cwd: True,
        )
        self.assertEqual((), findings)

    def test_cwd_none_skips_check(self) -> None:
        contracts_by_session = {
            "1": self._contract("1", self._command("run-1", ("pytest", "-q")))
        }
        findings = preflight_verification_checks._check_command_runnability(
            contracts_by_session,
            None,
            which=lambda name: None,
        )
        self.assertEqual((), findings)

    def test_non_command_checks_are_ignored(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                contracts.FileExistsCheck(name="f-1", path="README.md"),
                contracts.RegexInFileCheck(name="r-1", path="README.md", pattern="x"),
            )
        }
        findings = preflight_verification_checks._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: None,
        )
        self.assertEqual((), findings)

    def test_run_preflight_folds_command_finding_with_real_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _contract_block(
                        '{"type": "command", "argv": '
                        '["definitely-not-a-real-binary-xyz", "--version"], '
                        '"timeout_seconds": 30, "name": "run-1"}',
                        session="1",
                    ),
                },
            )
            report = preflight.run_preflight(plan_dir, cwd=repo_root)
            command_findings = [
                finding
                for finding in report.findings
                if finding.category == "command-not-runnable"
            ]
            self.assertEqual(1, len(command_findings))
            self.assertEqual("red", command_findings[0].severity)
            self.assertFalse(report.ok)

    def test_run_preflight_cwd_none_emits_no_command_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _contract_block(
                        '{"type": "command", "argv": '
                        '["definitely-not-a-real-binary-xyz", "--version"], '
                        '"timeout_seconds": 30, "name": "run-1"}',
                        session="1",
                    ),
                },
            )
            report = preflight.run_preflight(plan_dir, cwd=None)
            command_findings = [
                finding
                for finding in report.findings
                if finding.category == "command-not-runnable"
            ]
            self.assertEqual([], command_findings)

    def test_executable_finding_resolves_to_override_hint(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="red",
            category="command-not-runnable",
            message="session 1 check 'run-1' executable is not on PATH",
            session_id="1",
            detail="executable not found on PATH: pytest",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("command-not-runnable.executable", hint.check_id)
        self.assertEqual("red", hint.severity_hint)

    def test_module_finding_resolves_to_override_hint(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="yellow",
            category="command-not-runnable",
            message="session 1 check 'run-1' module is not importable",
            session_id="1",
            detail="module not importable by python3: pytest",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("command-not-runnable.module", hint.check_id)
        self.assertEqual("yellow", hint.severity_hint)
