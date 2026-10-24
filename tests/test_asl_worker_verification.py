"""Worker verification scenarios and policy/fixture digest coverage."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.support import REPO_ROOT

from .support_asl_worker_verification import FIXTURE_DIR, evaluate

ASL_TEST_LANE = "integration"

# --------------------------------------------------------------------------
# The fixture repository and its scenarios
# --------------------------------------------------------------------------


class ScenarioFixtureTests(unittest.TestCase):
    """The seeded defects are real, and the focused checks are what catch them."""

    def _seed(self, scenario: Any, destination: Path) -> Path:
        repo = destination / "repo"
        log = destination / "invocations.jsonl"
        evaluate.seed_repository(repo, scenario, log_path=log)
        return repo

    def _exit(self, repo: Path, *argv: str) -> int:
        return subprocess.run(
            [sys.executable, *argv],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        ).returncode

    def test_each_scenario_seeds_a_defect_its_own_checks_catch(self) -> None:
        for scenario in evaluate.SCENARIOS:
            with (
                self.subTest(scenario=scenario.key),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = self._seed(scenario, Path(tmp))
                self.assertNotEqual(
                    0,
                    self._exit(repo, "checks/focused.py", scenario.case),
                    "the focused check must fail on the seeded defect",
                )
                self.assertNotEqual(
                    0,
                    self._exit(repo, "checks/contract.py"),
                    "the broad gate must fail on the seeded defect",
                )
                shutil.copyfile(
                    evaluate.REPO_TEMPLATE / "toolkit.py", repo / "toolkit.py"
                )
                self.assertEqual(
                    0, self._exit(repo, "checks/focused.py", scenario.case)
                )
                self.assertEqual(0, self._exit(repo, "checks/contract.py"))

    def test_only_the_scenarios_own_case_is_broken(self) -> None:
        """Every other case passes, so the broad gate is satisfiable at once."""
        for scenario in evaluate.SCENARIOS:
            with (
                self.subTest(scenario=scenario.key),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = self._seed(scenario, Path(tmp))
                for other in evaluate.SCENARIOS:
                    if other.case == scenario.case:
                        continue
                    self.assertEqual(
                        0,
                        self._exit(repo, "checks/focused.py", other.case),
                        f"{other.case} must not be collateral damage",
                    )

    def test_the_unrelated_group_check_fails_and_is_outside_the_contract(self) -> None:
        scenario = evaluate.SCENARIOS_BY_KEY["scope-preserved"]
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._seed(scenario, Path(tmp))
            shutil.copyfile(evaluate.REPO_TEMPLATE / "toolkit.py", repo / "toolkit.py")
            self.assertNotEqual(0, self._exit(repo, "checks/legacy_group.py"))
            self.assertEqual(
                0,
                self._exit(repo, "checks/contract.py"),
                "the documented failure must not be inside the session's gate",
            )

    def test_the_invocation_log_records_what_ran(self) -> None:
        scenario = evaluate.SCENARIOS_BY_KEY["focused-final-gate"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._seed(scenario, root)
            self._exit(repo, "checks/focused.py", scenario.case)
            self._exit(repo, "checks/contract.py")
            records = evaluate.read_jsonl(root / "invocations.jsonl")
            self.assertEqual(
                ["focused:temperature", "contract"],
                [row["check"] for row in records],
            )
            self.assertTrue(all(row["exit_code"] != 0 for row in records))
            self.assertTrue(all(row["cwd"].endswith("repo") for row in records))

    def test_the_log_lives_outside_the_repository_under_evaluation(self) -> None:
        scenario = evaluate.SCENARIOS_BY_KEY["focused-final-gate"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._seed(scenario, root)
            self._exit(repo, "checks/contract.py")
            self.assertTrue((root / "invocations.jsonl").is_file())
            porcelain = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(
                "",
                porcelain.strip(),
                "instrumentation must not show up as a change in the tree it measures",
            )


class SessionTextTests(unittest.TestCase):
    """A trial's session file supplies the task, never the policy."""

    #: Phrases that would tell a worker who owns the final gate. If a fixture
    #: said any of them, a trial would be measuring the fixture.
    FORBIDDEN = (
        *evaluate.POLICY_ANCHORS,
        "the runner owns",
        "runner-owned",
        "do not rerun",
        "focused validation",
        "leave the",
    )

    def test_no_scenario_states_verification_policy(self) -> None:
        for scenario in evaluate.SCENARIOS:
            text = evaluate.session_text(scenario).lower()
            for phrase in self.FORBIDDEN:
                with self.subTest(scenario=scenario.key, phrase=phrase):
                    self.assertNotIn(phrase.lower(), text)

    def test_every_scenario_names_its_focused_check_and_the_broad_gate(self) -> None:
        for scenario in evaluate.SCENARIOS:
            text = evaluate.session_text(scenario)
            with self.subTest(scenario=scenario.key):
                self.assertIn(f"checks/focused.py {scenario.case}", text)
                self.assertIn('"checks/contract.py"', text)
                self.assertIn("- **Status**: pending", text)

    def test_only_the_scope_scenario_documents_the_other_groups_failure(self) -> None:
        for scenario in evaluate.SCENARIOS:
            text = evaluate.session_text(scenario)
            with self.subTest(scenario=scenario.key):
                if scenario.key == "scope-preserved":
                    self.assertIn("checks/legacy_group.py", text)
                    self.assertIn("legacy-report", text)
                else:
                    self.assertNotIn("legacy", text)


class DigestTests(unittest.TestCase):
    def test_every_worker_facing_surface_states_the_policy(self) -> None:
        surfaces = evaluate.policy_surfaces(REPO_ROOT)
        self.assertEqual(
            {"packet:implementation", "packet:review", "skill:session-run"},
            set(surfaces),
        )
        for name, lines in surfaces.items():
            with self.subTest(surface=name):
                self.assertTrue(
                    any(evaluate.POLICY_ANCHOR in line for line in lines),
                    f"{name} states no justified-repeat policy: {lines}",
                )

    def test_the_policy_digest_follows_the_policy_text(self) -> None:
        surfaces = evaluate.policy_surfaces(REPO_ROOT)
        baseline = evaluate.policy_digest(surfaces)
        self.assertEqual(baseline, evaluate.policy_digest(dict(surfaces)))
        drifted = dict(surfaces)
        drifted["skill:session-run"] = ["- something else entirely"]
        self.assertNotEqual(baseline, evaluate.policy_digest(drifted))

    def test_the_fixture_digest_is_stable(self) -> None:
        self.assertEqual(evaluate.fixture_digest(), evaluate.fixture_digest())
        self.assertRegex(evaluate.fixture_digest(), r"^[0-9a-f]{64}$")

    def test_the_fixture_digest_follows_the_seeded_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "repo"
            shutil.copytree(evaluate.REPO_TEMPLATE, template)
            self.assertEqual(
                evaluate.fixture_digest(), evaluate.fixture_digest(template)
            )
            target = template / "checks" / "cases.py"
            target.write_text(
                target.read_text(encoding="utf-8") + "\n# weakened\n", encoding="utf-8"
            )
            self.assertNotEqual(
                evaluate.fixture_digest(), evaluate.fixture_digest(template)
            )

    def test_the_policy_digest_covers_who_owns_the_final_gate(self) -> None:
        """Every surface's ownership statement is inside the hashed text.

        The digest once covered only the justified-repeat bullets, so a
        checkout could reverse "Do not rerun the full Completion Contract
        solely as a final gate" and every cached receipt still looked current.
        """
        surfaces = evaluate.policy_surfaces(REPO_ROOT)
        for name, lines in surfaces.items():
            with self.subTest(surface=name):
                self.assertTrue(
                    any(evaluate.POLICY_FINAL_GATE_ANCHOR in line for line in lines),
                    f"{name} states no final-gate ownership: {lines}",
                )
        self.assertEqual({}, evaluate.missing_policy_anchors(surfaces))

    def test_both_packet_roles_state_the_ownership_split(self) -> None:
        surfaces = evaluate.policy_surfaces(REPO_ROOT)
        for name in ("packet:implementation", "packet:review"):
            with self.subTest(surface=name):
                self.assertTrue(
                    any(
                        evaluate.POLICY_OWNERSHIP_ANCHOR in line
                        for line in surfaces[name]
                    ),
                    f"{name} states no verification-ownership split",
                )
        self.assertTrue(
            any(
                evaluate.POLICY_SKILL_OWNERSHIP_ANCHOR in line
                for line in surfaces["skill:session-run"]
            )
        )

    def test_the_fixture_digest_ignores_this_validator(self) -> None:
        """Editing the reader must not age out evidence about a run.

        ``fixture_digest`` covers the seeded repository and the scenarios. If
        it covered ``evaluate.py`` too, the digest here would move every time
        the validator gained a criterion, and the copy below — which differs
        from the shipped fixture only in the validator beside it — would not
        agree with it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "fixture"
            shutil.copytree(FIXTURE_DIR, copy, ignore=shutil.ignore_patterns("cache"))
            (copy / "evaluate.py").write_text(
                "# a different validator entirely\n", encoding="utf-8"
            )
            self.assertEqual(
                evaluate.fixture_digest(), evaluate.fixture_digest(copy / "repo")
            )


if __name__ == "__main__":
    unittest.main()
