"""Final acceptance: the criteria matrix and the architectural invariants.

The matrix names, for every acceptance criterion of the assurance work, the
tests that prove it. Resolving those names is itself a test, so renaming or
deleting a regression fails here instead of silently uncovering a criterion.

The remaining classes assert the four whole-system properties no single
session owns: no adapter drives implementation tools under the audit profile,
an audit never changes what the run it graded recorded, the operator read path
cannot launch an auditor, and no canonical ledger operation deletes history."""

from __future__ import annotations

import ast
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_summary,
    assurance_models_vocab,
    assurance_report,
    harness_claude,
    harness_copilot,
    harnesses,
)
from agent_session_loop import assurance_projection as projection
from tests import run_suite
from tests.support import REPO_ROOT

from .support_asl_acceptance import ACCEPTANCE_MATRIX, _split_reference


class AcceptanceMatrixTests(unittest.TestCase):
    """Every acceptance criterion names regressions that actually exist."""

    def test_every_acceptance_criterion_resolves_to_an_executable_regression(
        self,
    ) -> None:
        for criterion, references in ACCEPTANCE_MATRIX:
            with self.subTest(criterion=criterion):
                self.assertTrue(references, "criterion names no regression")
            for reference in references:
                with self.subTest(reference=reference):
                    location, class_name, method_name = _split_reference(reference)
                    self.assertTrue(
                        method_name.startswith("test"),
                        f"{reference} does not name a test method",
                    )
                    if location.endswith(".py"):
                        self._assert_script_suite_defines(
                            location, class_name, method_name
                        )
                    else:
                        self._assert_module_defines(location, class_name, method_name)

    def _assert_module_defines(
        self, module_name: str, class_name: str, method_name: str
    ) -> None:
        module = importlib.import_module(module_name)
        case = getattr(module, class_name, None)
        self.assertIsNotNone(case, f"{module_name} defines no {class_name}")
        self.assertTrue(
            issubclass(case, unittest.TestCase),
            f"{module_name}:{class_name} is not a TestCase",
        )
        self.assertTrue(
            callable(getattr(case, method_name, None)),
            f"{module_name}:{class_name} defines no {method_name}",
        )

    def _assert_script_suite_defines(
        self, relative_path: str, class_name: str, method_name: str
    ) -> None:
        """Standalone suites are read rather than imported.

        They live outside the ``tests`` package and expect to run as scripts,
        so parsing proves the regression exists without adopting their import
        assumptions.
        """
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                names = {
                    child.name
                    for child in node.body
                    if isinstance(child, ast.FunctionDef)
                }
                self.assertIn(method_name, names, f"{relative_path}:{class_name}")
                return
        self.fail(f"{relative_path} defines no {class_name}")

    def test_every_named_module_runs_in_a_suite_shard(self) -> None:
        """A regression the suite driver never launches proves nothing."""
        shard_modules = set(run_suite.FAST_MODULES) | set(run_suite.INTEGRATION_MODULES)
        script_suites = set(run_suite.SCRIPT_SUITES)
        for _, references in ACCEPTANCE_MATRIX:
            for reference in references:
                location = _split_reference(reference)[0]
                with self.subTest(location=location):
                    if location.endswith(".py"):
                        self.assertIn(location, script_suites)
                    else:
                        self.assertIn(location, shard_modules)


class AuditAccessProfileTests(unittest.TestCase):
    """No adapter drives implementation tools while auditing."""

    #: Argv fragments that grant a worker's write access. None may appear in an
    #: audit invocation for any harness, whatever that harness calls them.
    WORKER_GRANTS = (
        "--dangerously-bypass-approvals-and-sandbox",
        "workspace-write",
        "bypassPermissions",
        "dontAsk",
        "--allow-all-tools",
        "--allow-all-paths",
    )

    def _audit_argv(self, name: str, read_root: Path, cwd: Path) -> list[str]:
        adapter = harnesses.get_adapter(name)
        # Codex states its audit profile over the app-server protocol, so its
        # *argv* boundary is the fallback transport's argv; the protocol side
        # is asserted separately against the turn plan.
        builder = getattr(adapter, "build_exec_argv", adapter.build_argv)
        return builder(
            cwd=cwd,
            plan_dir=None,
            prompt="audit this run",
            last_message_path=cwd / "last-message.txt",
            access=harnesses.audit_read_only_access((read_root,)),
        )

    def _audit_access_text(self, name: str, read_root: Path, cwd: Path) -> str:
        """Everything describing one audit turn's access, on either transport.

        Argv alone would pass vacuously for a harness driven over a protocol,
        so this renders argv *and* the turn plan, which is where the sandbox,
        the approval policy, and the writable roots now live.
        """
        adapter = harnesses.get_adapter(name)
        invocation = adapter.build_invocation(
            cwd=cwd,
            plan_dir=None,
            prompt="audit this run",
            last_message_path=cwd / "last-message.txt",
            access=harnesses.audit_read_only_access((read_root,)),
        )
        rendered = "\n".join(invocation.argv)
        if invocation.turn_plan is not None:
            rendered += "\n" + json.dumps(
                invocation.turn_plan.to_payload(), sort_keys=True
            )
        return rendered

    def test_no_audit_argv_carries_a_worker_write_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in harnesses.HARNESS_NAMES:
                argv = self._audit_argv(name, root / "evidence", root / "workdir")
                described = self._audit_access_text(
                    name, root / "evidence", root / "workdir"
                )
                for grant in self.WORKER_GRANTS:
                    with self.subTest(harness=name, grant=grant):
                        self.assertNotIn(grant, argv)
                        self.assertNotIn(grant, described)

    def test_no_audit_argv_enables_a_mutating_tool(self) -> None:
        """Each adapter denies mutation in the vocabulary its own CLI enforces."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = self._audit_argv("codex", root / "evidence", root / "workdir")
            self.assertNotIn("--add-dir", codex)
            self.assertEqual(["read-only"], codex[codex.index("-s") + 1 :][:1])
            codex_plan = harnesses.get_adapter("codex").build_turn_plan(  # type: ignore[attr-defined]
                cwd=root / "workdir",
                plan_dir=None,
                access=harnesses.audit_read_only_access((root / "evidence",)),
            )
            self.assertEqual("read-only", codex_plan.thread_params.get("sandbox"))
            self.assertNotIn("config", codex_plan.thread_params)

            claude = self._audit_argv("claude", root / "evidence", root / "workdir")
            self.assertEqual(
                ["plan"], claude[claude.index("--permission-mode") + 1 :][:1]
            )
            self.assertIn(harness_claude.CLAUDE_AUDIT_DENIED_TOOLS, claude)
            for tool in harness_claude.CLAUDE_AUDIT_TOOLS.split(","):
                self.assertNotIn(tool, ("Bash", "Edit", "Write"))

            copilot = self._audit_argv("copilot", root / "evidence", root / "workdir")
            for denied in harness_copilot.COPILOT_AUDIT_DENIED_TOOLS:
                self.assertIn(f"--deny-tool={denied}", copilot)

    def test_no_adapter_accepts_a_writable_plan_dir_for_an_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            for name in harnesses.HARNESS_NAMES:
                with (
                    self.subTest(harness=name),
                    self.assertRaises(harnesses.HarnessError),
                ):
                    harnesses.get_adapter(name).check_access(
                        harnesses.audit_read_only_access((Path(tmp),)),
                        plan_dir=plan_dir,
                        unsafe_bypass=False,
                    )

    def test_no_adapter_lets_an_audit_inherit_unsafe_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in harnesses.HARNESS_NAMES:
                with (
                    self.subTest(harness=name),
                    self.assertRaises(harnesses.HarnessError),
                ):
                    harnesses.get_adapter(name).check_access(
                        harnesses.audit_read_only_access((Path(tmp),)),
                        plan_dir=None,
                        unsafe_bypass=True,
                    )


class ObservationalStatusTests(unittest.TestCase):
    """Nothing an operator reads assurance through can start a harness."""

    #: Every module `asl status` reaches to render assurance.
    READ_PATH = (
        assurance_report,
        projection,
        ledger,
        assurance_models_findings,
        assurance_models_inbox,
        assurance_models_summary,
        assurance_models_vocab,
    )

    #: Modules that start an agent turn. The read path may borrow a name from
    #: ``harnesses`` — the harness-name tuple is a vocabulary, not a launcher —
    #: so importing it is allowed and calling into it is not.
    FORBIDDEN_IMPORTS = (
        "subprocess",
        "supervisor_audit",
        "assurance_catchup",
        "runner",
    )

    #: The call names that actually start something. Generic names such as
    #: ``run`` are left out on purpose: the read path calls ``inbox.run(...)``
    #: to look a run up, and banning the import of ``subprocess`` already
    #: settles the only ambiguous case.
    FORBIDDEN_CALLS = (
        "run_harness",
        "build_invocation",
        "build_argv",
        "audit_terminal_run",
        "Popen",
    )

    def test_the_operator_read_path_cannot_reach_a_harness(self) -> None:
        for module in self.READ_PATH:
            source = Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported.add(node.module.split(".")[0])
                    if node.level:
                        imported.update(alias.name for alias in node.names)
            for forbidden in self.FORBIDDEN_IMPORTS:
                with self.subTest(module=module.__name__, imports=forbidden):
                    self.assertNotIn(forbidden, imported)

            called = {
                node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Attribute, ast.Name))
            }
            for forbidden in self.FORBIDDEN_CALLS:
                with self.subTest(module=module.__name__, calls=forbidden):
                    self.assertNotIn(forbidden, called)


if __name__ == "__main__":
    unittest.main()
