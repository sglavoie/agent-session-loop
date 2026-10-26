"""Test-lane and process-sweep docs checked against authoritative definitions."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    preflight_runtime_hints,
    process_tree,
    task_packet,
    verification_process,
)
from tests import run_suite
from tests.asl_docs_support import ASL_GUIDE, read_asl_reference
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class AuthoritativeLaneDocsTests(unittest.TestCase):
    docs: str

    guide: str

    justfile: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = read_asl_reference()
        cls.guide = (REPO_ROOT / ASL_GUIDE).read_text(encoding="utf-8")
        cls.justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")

    def sweep_section(self) -> str:
        return self.docs.split("### Process sweep", 1)[1].split("\n## ", 1)[0]

    def test_packet_mode_and_rollback_are_documented(self) -> None:
        self.assertEqual("packet", task_packet.WORKER_MODE_PACKET)
        self.assertIn("task_packet.build_task_packet", self.docs)
        self.assertIn("ASL_WORKER_MODE=skill", self.docs)

    def test_docs_guide_and_readme_never_reference_pytest(self) -> None:
        self.assertNotIn("pytest", self.docs)
        self.assertNotIn("pytest", self.guide)
        self.assertNotIn(
            "pytest", (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        )

    def test_test_recipes_exist_and_are_documented(self) -> None:
        self.assertIn("\ntest-fast:", self.justfile)
        self.assertIn("\ntest-portable:", self.justfile)
        self.assertIn("\ntest-all:", self.justfile)
        for text in (self.docs, self.guide):
            self.assertIn("just test-fast", text)
            self.assertIn("just test-portable", text)
            self.assertIn("just test-all", text)
        self.assertIn("tests/run_suite.py", self.docs)

    def test_the_stress_fault_lane_is_real_and_stays_out_of_the_budgets(self) -> None:
        """The fault list must name live classes and never be timed.

        Three things can rot independently here: a renamed test class leaves a
        dotted name nothing resolves, a new fault group lands in the module but
        never in the recipe, and a well-meaning edit folds the fault list into
        the budget phase -- where a deadline pinned against an injected clock
        would be read with a stopwatch and reported as a CLI regression.
        """
        self.assertIn(
            "scripts/reliability-stress.sh",
            self.justfile.split("\nreliability-stress ", 1)[1].split("\n\n", 1)[0],
        )
        recipe = (REPO_ROOT / "scripts" / "reliability-stress.sh").read_text(
            encoding="utf-8"
        )
        names = recipe.split("fault_scenarios=(", 1)[1].split(")", 1)[0].split()
        self.assertIn("tests.test_asl_redirect_adversarial.SlowStorageTests", names)
        defined: dict[str, set[str]] = {}
        for name in names:
            module_name, class_name = name.rsplit(".", 1)
            if module_name not in defined:
                path = REPO_ROOT / Path(*module_name.split(".")).with_suffix(".py")
                tree = ast.parse(path.read_text(encoding="utf-8"))
                defined[module_name] = {
                    node.name for node in tree.body if isinstance(node, ast.ClassDef)
                }
            self.assertIn(class_name, defined[module_name], name)
        adversarial = "tests.test_asl_redirect_adversarial"
        # The three groups held back assert against fixed records and injected
        # state rather than against a race, so repeating them under load would
        # buy nothing; every other group in the module must be in the recipe.
        deterministic = {
            "PartialMultiRepositoryTests",
            "ReviewRepairTests",
            "MissingHistoryTests",
        }
        self.assertEqual(
            {
                name
                for name in defined[adversarial]
                if name.endswith("Tests") and name not in deterministic
            },
            {
                name.rsplit(".", 1)[1]
                for name in names
                if name.startswith(f"{adversarial}.")
            },
            "every racing fault group in the adversarial module belongs in the recipe",
        )
        [budget_line] = [
            line
            for line in recipe.splitlines()
            if "ASL_E2E_TIME_BUDGETS=1" in line and "unittest" in line
        ]
        self.assertNotIn("fault_scenarios", budget_line)
        self.assertIn("just reliability-stress", self.docs)

    def test_the_stress_script_refuses_counts_that_are_not_positive(self) -> None:
        """A count that would loop zero times must fail, not report ok."""
        script = REPO_ROOT / "scripts" / "reliability-stress.sh"
        for args in (["abc"], ["0"], ["3", "-1"], ["3", "x"], ["01"]):
            with self.subTest(args=args):
                completed = subprocess.run(
                    ["bash", os.fspath(script), *args],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(completed.returncode, 2, completed.stdout)
                self.assertEqual(completed.stdout, "")
                self.assertIn("usage:", completed.stderr)
                self.assertIn("positive integers", completed.stderr)

    def test_ci_lints_with_the_dev_group_and_pins_every_action(self) -> None:
        """One list of linter versions, and no action that can move under CI."""
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        dev = pyproject["dependency-groups"]["dev"]
        self.assertEqual(
            sorted(name.split("==", 1)[0] for name in dev), ["mypy", "ruff"]
        )
        self.assertTrue(all("==" in name for name in dev), dev)
        workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertGreaterEqual(len(workflows), 3)
        for path in workflows:
            name = path.name
            workflow = path.read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                for uses in re.findall(r"uses:\s*(\S+)", workflow):
                    self.assertRegex(uses, r"@[0-9a-f]{40}$")
                # No job pushes, so no checkout leaves the token in .git/config.
                self.assertEqual(
                    workflow.count("uses: actions/checkout@"),
                    workflow.count("persist-credentials: false"),
                )
                for runner in re.findall(r"runs-on:\s*(\S+)", workflow):
                    self.assertNotIn("latest", runner)
                self.assertNotRegex(workflow, r"(ruff|mypy)==")
        check = (REPO_ROOT / ".github/workflows/check.yml").read_text(encoding="utf-8")
        self.assertIn("python3 -m pip install --group dev", check)
        self.assertNotIn("cache-dependency-path: .github", check)
        self.assertEqual(2, check.count("path: .cache/run_suite_logs/"))
        self.assertIn('python-version: ["3.14", "3.15"]', check)
        # The 3.13 floor the portable Workbench promises is exercised in CI.
        self.assertIn("python3.13 -m unittest -q tests.test_workbench_portable", check)

    def test_the_pause_no_reply_clears_says_where_to_look_in_both_places(self) -> None:
        """A stop nobody can answer has to name the evidence that ends it."""
        hint = preflight_runtime_hints.RUNTIME_REPAIR_HINTS[
            events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED
        ]
        for phrase in (
            "remaining_pids",
            "unverified_pids",
            "ownership_unknown",
            "predecessor-uncollected",
            process_tree.INCOMPLETE_SWEEP_OPT_OUT,
            process_tree.SWEEP_DISABLED_ENV_VAR,
            "ps -p",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, hint.message)
                self.assertIn(phrase, self.docs)

    def test_the_portable_lane_is_a_subset_of_the_fast_lane(self) -> None:
        """A renamed or deleted module cannot survive as a silent skip.

        Portability is declared by hand -- ``ASL_TEST_PORTABLE = True`` means
        "spawns no external process" -- so nothing recomputes it. Without this,
        a module moved out of ``FAST_MODULES`` would keep the Linux lane green
        by running a name the fast lane no longer vouches for.
        """
        stray = set(run_suite.PORTABLE_MODULES) - set(run_suite.FAST_MODULES)

        self.assertEqual(stray, set())
        self.assertEqual(
            len(set(run_suite.PORTABLE_MODULES)),
            len(run_suite.PORTABLE_MODULES),
        )

    def test_process_sweep_docs_match_the_authoritative_definitions(self) -> None:
        section = self.sweep_section()
        self.assertIn(f"`{events.PROCESS_SWEEP_EVENT}` event", section)
        # The opt-out is documented value by value; each documented value has
        # to actually disable the sweep.
        self.assertIn(f"`{process_tree.SWEEP_DISABLED_ENV_VAR}=1`", section)
        for value in ("true", "yes", "on"):
            self.assertIn(f"`{value}`", section)
        for value in ("1", "true", "yes", "on"):
            self.assertFalse(
                process_tree.sweep_enabled(
                    {process_tree.SWEEP_DISABLED_ENV_VAR: value}
                ),
                f"docs claim {value!r} disables the sweep",
            )
        self.assertIn("The sweep is **on by default**.", section)
        self.assertTrue(process_tree.sweep_enabled({}))
        self.assertIn("including a typo, leaves it\nenabled", section)
        self.assertTrue(
            process_tree.sweep_enabled(
                {process_tree.SWEEP_DISABLED_ENV_VAR: "disbaled"}
            )
        )

    def test_process_sweep_docs_list_every_report_key(self) -> None:
        section = self.sweep_section()
        # No group to enumerate and nothing snapshotted, so this reads the
        # process table and signals nothing.
        report = process_tree.sweep_survivors(root_pid=os.getpid(), expected_pgid=None)
        self.assertEqual([], report["killed"])
        for key in report:
            self.assertIn(f"`{key}`", section, f"docs never mention report key {key!r}")
        for key in report["enumeration"]:
            self.assertIn(
                f"`{key}`",
                section,
                f"docs never mention enumeration key {key!r}",
            )

    def test_process_sweep_docs_state_the_limit_and_drop_the_stale_claims(
        self,
    ) -> None:
        section = self.sweep_section()
        self.assertIn(
            "is identified by none of the three\nroutes and survives the sweep,"
            " as does one forked after that read.",
            section,
        )
        # Superseded by the sweep: the parallel-verification contract used to
        # present an escaped descendant as something the runner never chases.
        self.assertNotIn("so the runner does not try", self.docs)
        self.assertNotIn("is **not** stopped by either", self.docs)

    def test_reader_join_bounds_match_the_documented_numbers(self) -> None:
        self.assertEqual(5.0, verification_process._ABANDONED_READER_JOIN_SECONDS)
        self.assertEqual(30.0, verification_process._EXITED_READER_JOIN_SECONDS)
        self.assertIn(
            "the pipe readers of a killed check are joined under a five-second\n"
            "  limit and those of a check that exited on its own under thirty "
            "seconds",
            self.docs,
        )


if __name__ == "__main__":
    unittest.main()
