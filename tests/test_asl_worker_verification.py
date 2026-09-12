"""Deterministic cover for the live-worker verification evaluator.

The evaluator in ``tests/fixtures/asl_worker_verification/evaluate.py`` exists
to record what real workers do. These tests cover everything about it that can
be settled without a model: that its scenarios really are broken until they are
fixed, that its session text smuggles in no verification policy of its own,
that its digests notice a changed policy or fixture, and — the part that
matters most — that ``check`` refuses evidence which is missing, incomplete,
contradictory or stale.

Nothing here launches a live model. Every trial these tests run is driven by
``fake_worker.py``, and a trial driven by an injected executable is recorded as
not live, which ``check`` never accepts.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import state
from tests.support import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "asl_worker_verification"
FAKE_WORKER = FIXTURE_DIR / "fake_worker.py"


def _load_evaluator() -> Any:
    """Import the evaluator by path.

    It is a fixture rather than a package module, and ``tests/fixtures`` is not
    importable, so the path load is what the fixture's own executables do too.
    """
    spec = importlib.util.spec_from_file_location(
        "asl_worker_verification_evaluate", FIXTURE_DIR / "evaluate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evaluate = _load_evaluator()


# --------------------------------------------------------------------------
# Synthetic evidence
# --------------------------------------------------------------------------


def _invocation(
    *,
    check: str,
    actor: str,
    exit_code: int = 0,
    started_at: str = "2026-09-12T00:00:00+00:00",
    conversation: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "check": check,
        "argv": ["checks/contract.py"],
        "cwd": "/repo",
        "started_at": started_at,
        "finished_at": started_at,
        "duration_seconds": 0.01,
        "exit_code": exit_code,
        "detail": None,
        "pid": 1,
        "ppid": 2,
        "session_leader": actor == "runner",
        "actor": actor,
        "declared_role": actor if actor == "evaluator" else None,
        "worker_env": actor == "worker",
        "worker_conversation_id": conversation,
    }


def _gate_event(*, status: str = "pass") -> dict[str, Any]:
    return {
        "created_at": "2026-09-12T00:00:01+00:00",
        "name": "verification_check_finished",
        "payload": {
            "kind": "command",
            "name": evaluate.CONTRACT_CHECK_NAME,
            "returncode": 0 if status == "pass" else 1,
            "role": "gate",
            "status": status,
            "session_id": "1",
        },
        "version": 2,
    }


def _conversation_record(native_id: str) -> dict[str, Any]:
    return {
        "kind": "conversation_opened",
        "recorded_at": "2026-09-12T00:00:00+00:00",
        "reference": {
            "access_profile": "worker",
            "harness": "claude",
            "native_id": native_id,
            "session_id": "1",
            "version": 1,
        },
        "version": 1,
    }


class SyntheticTrial:
    """A trial's artifacts on disk, without ever having run one.

    Written through the same paths ``load_evidence`` reads, so a test that
    changes one fact changes exactly the fact a real trial would have changed.
    """

    def __init__(
        self,
        root: Path,
        *,
        scenario: str,
        trial: int,
        invocations: list[dict[str, Any]],
        events: list[dict[str, Any]],
        conversations: list[str],
        handoff_status: str = "complete",
        handoff_notes: str = "",
        final_state: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.trial_dir = root / "trials" / scenario / str(trial)
        workspace = self.trial_dir / "workspace"
        plan_dir = workspace / "plan"
        state_dir = workspace / "state"
        repo = workspace / "repo"
        for path in (plan_dir, state_dir, repo):
            path.mkdir(parents=True, exist_ok=True)
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)

        log_path = workspace / "invocations.jsonl"
        log_path.write_text(
            "".join(json.dumps(row) + "\n" for row in invocations), encoding="utf-8"
        )
        (plan_state / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
        )
        (plan_state / "conversations.jsonl").write_text(
            "".join(
                json.dumps(_conversation_record(native)) + "\n"
                for native in conversations
            ),
            encoding="utf-8",
        )
        (plan_dir / "session-1.md").write_text(
            "\n".join(
                [
                    "# Session 1: Synthetic",
                    "",
                    "## Handoff",
                    "",
                    f"- **Status**: {handoff_status}",
                    "- **Files modified**: toolkit.py",
                    "- **Key decisions**: (none)",
                    "- **Blockers for next session**: (none)",
                    f"- **Notes**: {handoff_notes}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        scenario_object = evaluate.SCENARIOS_BY_KEY[scenario]
        receipt: dict[str, Any] = {
            "schema": evaluate.RECEIPT_VERSION,
            "scenario": scenario,
            "case": scenario_object.case,
            "trial": trial,
            "created_at": "2026-09-12T00:00:02+00:00",
            "live": True,
            "harness": "claude",
            "model_requested": "claude-opus-5",
            "candidate": {"root": str(REPO_ROOT), "commit": "0" * 40, "dirty": False},
            "policy_surfaces": evaluate.policy_surfaces(REPO_ROOT),
            "fixture_digest": evaluate.fixture_digest(),
            "workspace": {
                "root": str(workspace),
                "repo": str(repo),
                "plan_dir": str(plan_dir),
                "state_dir": str(state_dir),
                "invocations": str(log_path),
            },
            "run": {"argv": ["asl", "run"], "returncode": 0, "timed_out": False},
            "final_state": final_state
            if final_state is not None
            else {
                "focused_exit": 0,
                "contract_exit": 0,
                "changed_paths": ["toolkit.py"],
                "head": "1" * 40,
            },
        }
        receipt["policy_digest"] = evaluate.policy_digest(receipt["policy_surfaces"])
        if overrides:
            receipt.update(overrides)
        self.receipt = receipt
        self.receipt_path = self.trial_dir / "receipt.json"
        self.receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def _clean_trial_arguments(
    scenario: str, trial: int, *, conversation: str
) -> dict[str, Any]:
    """The artifacts of a trial that satisfies every criterion."""
    case = evaluate.SCENARIOS_BY_KEY[scenario].case
    invocations = [
        _invocation(
            check=f"focused:{case}",
            actor="worker",
            started_at="2026-09-12T00:00:00+00:00",
            conversation=conversation,
        ),
        _invocation(
            check=evaluate.CONTRACT_CHECK_NAME,
            actor="runner",
            started_at="2026-09-12T00:00:01+00:00",
        ),
    ]
    if scenario == "focused-retry":
        invocations.insert(
            0,
            _invocation(
                check=f"focused:{case}",
                actor="worker",
                exit_code=1,
                started_at="2026-09-11T23:59:59+00:00",
                conversation=conversation,
            ),
        )
    return {
        "scenario": scenario,
        "trial": trial,
        "invocations": invocations,
        "events": [_gate_event()],
        "conversations": [conversation],
    }


def _accepting_cache(root: Path) -> None:
    for scenario in evaluate.SCENARIOS:
        for trial in range(1, evaluate.REQUIRED_TRIALS + 1):
            SyntheticTrial(
                root,
                **_clean_trial_arguments(
                    scenario.key, trial, conversation=f"conv-{scenario.key}-{trial}"
                ),
            )


class TempRootMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def criteria(self, trial: SyntheticTrial) -> dict[str, Any]:
        evidence = evaluate.load_evidence(trial.receipt)
        results = evaluate.evaluate_trial(
            trial.receipt,
            evidence,
            expected_policy_digest=evaluate.policy_digest(
                evaluate.policy_surfaces(REPO_ROOT)
            ),
            expected_fixture_digest=evaluate.fixture_digest(),
        )
        return {criterion.name: criterion for criterion in results}


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
        evaluate.POLICY_ANCHOR,
        evaluate.POLICY_RATIONALE_ANCHOR,
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


# --------------------------------------------------------------------------
# Evidence validation
# --------------------------------------------------------------------------


class AcceptanceTests(TempRootMixin):
    def test_a_complete_consistent_set_of_trials_is_accepted(self) -> None:
        _accepting_cache(self.root)
        report = evaluate.check_cache(self.root)
        self.assertTrue(report["accepted"], report["problems"])
        self.assertEqual([], report["problems"])

    def test_an_empty_cache_is_not_accepted(self) -> None:
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("0 receipt(s)" in problem for problem in report["problems"]),
            report["problems"],
        )

    def test_one_trial_per_scenario_is_not_enough(self) -> None:
        for scenario in evaluate.SCENARIOS:
            SyntheticTrial(
                self.root,
                **_clean_trial_arguments(
                    scenario.key, 1, conversation=f"conv-{scenario.key}"
                ),
            )
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("1 receipt(s), 2 required" in p for p in report["problems"]),
            report["problems"],
        )

    def test_two_trials_that_share_a_conversation_are_not_fresh(self) -> None:
        _accepting_cache(self.root)
        SyntheticTrial(
            self.root,
            **{
                **_clean_trial_arguments(
                    "focused-final-gate", 2, conversation="conv-focused-final-gate-1"
                )
            },
        )
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("share conversation id" in p for p in report["problems"]),
            report["problems"],
        )

    def test_a_trial_with_no_recorded_conversation_is_not_accepted(self) -> None:
        _accepting_cache(self.root)
        arguments = _clean_trial_arguments(
            "focused-final-gate", 2, conversation="conv-x"
        )
        arguments["conversations"] = []
        for row in arguments["invocations"]:
            row["worker_conversation_id"] = None
        SyntheticTrial(self.root, **arguments)
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("no conversation was recorded" in p for p in report["problems"]),
            report["problems"],
        )

    def test_an_unreadable_receipt_is_a_problem_rather_than_a_skip(self) -> None:
        _accepting_cache(self.root)
        target = self.root / "trials" / "focused-retry" / "1" / "receipt.json"
        target.write_text("{not json", encoding="utf-8")
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("unreadable receipt" in p for p in report["problems"]),
            report["problems"],
        )


class RejectedEvidenceTests(TempRootMixin):
    """``check`` refuses missing, incomplete, contradictory and stale evidence."""

    def _trial(self, scenario: str = "focused-final-gate", **changes: Any) -> Any:
        arguments = _clean_trial_arguments(scenario, 1, conversation="conv-1")
        arguments.update(changes)
        return SyntheticTrial(self.root, **arguments)

    def test_a_written_passed_claim_is_not_evidence(self) -> None:
        trial = self._trial(
            overrides={
                "passed": True,
                "verdict": "accepted",
                "notes": "All criteria satisfied.",
            }
        )
        shutil.rmtree(Path(trial.receipt["workspace"]["root"]))
        criteria = self.criteria(trial)
        self.assertFalse(criteria["evidence-present"].passed)
        self.assertIn("invocation log is absent", criteria["evidence-present"].detail)

    def test_a_missing_invocation_log_is_missing_evidence(self) -> None:
        trial = self._trial()
        Path(trial.receipt["workspace"]["invocations"]).unlink()
        criteria = self.criteria(trial)
        self.assertFalse(criteria["evidence-present"].passed)

    def test_an_incomplete_receipt_is_rejected(self) -> None:
        trial = self._trial()
        receipt = dict(trial.receipt)
        receipt.pop("final_state")
        receipt.pop("candidate")
        evidence = evaluate.load_evidence(receipt)
        criteria = {
            criterion.name: criterion
            for criterion in evaluate.evaluate_trial(
                receipt,
                evidence,
                expected_policy_digest=receipt["policy_digest"],
                expected_fixture_digest=receipt["fixture_digest"],
            )
        }
        self.assertFalse(criteria["receipt-complete"].passed)
        self.assertIn("final_state", criteria["receipt-complete"].detail)

    def test_a_receipt_from_another_schema_is_rejected(self) -> None:
        trial = self._trial(overrides={"schema": evaluate.RECEIPT_VERSION + 1})
        self.assertFalse(self.criteria(trial)["receipt-complete"].passed)

    def test_a_stale_policy_digest_is_rejected(self) -> None:
        trial = self._trial(overrides={"policy_digest": "0" * 64})
        criteria = self.criteria(trial)
        self.assertFalse(criteria["policy-current"].passed)
        self.assertIn("!= current", criteria["policy-current"].detail)

    def test_a_stale_fixture_digest_is_rejected(self) -> None:
        trial = self._trial(overrides={"fixture_digest": "0" * 64})
        self.assertFalse(self.criteria(trial)["fixture-current"].passed)

    def test_a_trial_run_before_the_policy_existed_is_rejected(self) -> None:
        surfaces: dict[str, list[str]] = {
            "packet:implementation": [],
            "packet:review": [],
            "skill:session-run": [],
        }
        trial = self._trial(
            overrides={
                "policy_surfaces": surfaces,
                "policy_digest": evaluate.policy_digest(surfaces),
            }
        )
        criteria = self.criteria(trial)
        self.assertFalse(criteria["policy-stated"].passed)
        self.assertFalse(criteria["policy-current"].passed)

    def test_a_fake_harness_receipt_is_rejected(self) -> None:
        trial = self._trial(overrides={"live": False})
        criteria = self.criteria(trial)
        self.assertFalse(criteria["live-harness"].passed)
        self.assertIn("injected harness", criteria["live-harness"].detail)

    def test_gate_events_without_matching_invocations_are_contradictory(self) -> None:
        """The run says it ran the gate; nothing recorded the gate running."""
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["invocations"] = [
            row for row in arguments["invocations"] if row["actor"] != "runner"
        ]
        trial = SyntheticTrial(self.root, **arguments)
        criteria = self.criteria(trial)
        self.assertFalse(criteria["gate-record-consistent"].passed)
        self.assertIn("0 runner-attributed", criteria["gate-record-consistent"].detail)

    def test_invocations_without_matching_gate_events_are_contradictory(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["events"] = []
        trial = SyntheticTrial(self.root, **arguments)
        criteria = self.criteria(trial)
        self.assertFalse(criteria["runner-gate-executed"].passed)
        self.assertFalse(criteria["gate-record-consistent"].passed)

    def test_a_failing_runner_gate_is_not_a_pass(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["events"] = [_gate_event(status="fail")]
        trial = SyntheticTrial(self.root, **arguments)
        self.assertFalse(self.criteria(trial)["runner-gate-executed"].passed)

    def test_an_unfinished_session_is_rejected(self) -> None:
        trial = self._trial(handoff_status="pending")
        self.assertFalse(self.criteria(trial)["session-complete"].passed)

    def test_a_defect_still_present_afterwards_is_rejected(self) -> None:
        trial = self._trial(
            final_state={
                "focused_exit": 1,
                "contract_exit": 1,
                "changed_paths": [],
                "head": "2" * 40,
            }
        )
        self.assertFalse(self.criteria(trial)["defect-fixed"].passed)

    def test_a_worker_that_ran_no_focused_check_is_rejected(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["invocations"] = [
            row for row in arguments["invocations"] if row["actor"] != "worker"
        ]
        trial = SyntheticTrial(self.root, **arguments)
        self.assertFalse(self.criteria(trial)["focused-check-used"].passed)


class BroadGateTests(TempRootMixin):
    def _with_worker_contract(self, notes: str) -> Any:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["invocations"].append(
            _invocation(
                check=evaluate.CONTRACT_CHECK_NAME,
                actor="worker",
                started_at="2026-09-12T00:00:00.5+00:00",
                conversation="c",
            )
        )
        arguments["handoff_notes"] = notes
        return SyntheticTrial(self.root, **arguments)

    def test_a_worker_running_the_broad_gate_without_a_reason_is_rejected(self) -> None:
        trial = self._with_worker_contract("Fixed the conversion.")
        criterion = self.criteria(trial)["no-unjustified-broad-gate"]
        self.assertFalse(criterion.passed)
        self.assertIn("no rationale", criterion.detail)

    def test_a_recorded_reason_makes_a_repeat_acceptable(self) -> None:
        trial = self._with_worker_contract(
            "Ran the contract gate once after the second edit because the "
            "focused check could not cover the import it moved."
        )
        self.assertTrue(self.criteria(trial)["no-unjustified-broad-gate"].passed)

    def test_naming_the_gate_is_not_a_reason_for_having_run_it(self) -> None:
        trial = self._with_worker_contract("Ran checks/contract.py. All green.")
        self.assertFalse(self.criteria(trial)["no-unjustified-broad-gate"].passed)

    def test_leaving_the_broad_gate_alone_needs_no_note(self) -> None:
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["no-unjustified-broad-gate"]
        self.assertTrue(criterion.passed)
        self.assertIn("never ran the broad gate", criterion.detail)


class ScenarioCriteriaTests(TempRootMixin):
    def test_a_retry_needs_a_failure_before_it(self) -> None:
        arguments = _clean_trial_arguments("focused-retry", 1, conversation="c")
        arguments["invocations"] = [
            row
            for row in arguments["invocations"]
            if not (row["actor"] == "worker" and row["exit_code"] != 0)
        ]
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["focused-retry-after-failure"]
        self.assertFalse(criterion.passed)
        self.assertIn("0 failing", criterion.detail)

    def test_a_retry_that_never_came_good_is_rejected(self) -> None:
        arguments = _clean_trial_arguments("focused-retry", 1, conversation="c")
        for row in arguments["invocations"]:
            if row["actor"] == "worker":
                row["exit_code"] = 1
        trial = SyntheticTrial(self.root, **arguments)
        self.assertFalse(self.criteria(trial)["focused-retry-after-failure"].passed)

    def test_a_failure_then_a_pass_is_the_justified_retry(self) -> None:
        arguments = _clean_trial_arguments("focused-retry", 1, conversation="c")
        trial = SyntheticTrial(self.root, **arguments)
        self.assertTrue(self.criteria(trial)["focused-retry-after-failure"].passed)

    def test_running_the_other_groups_check_breaks_scope(self) -> None:
        arguments = _clean_trial_arguments("scope-preserved", 1, conversation="c")
        arguments["invocations"].append(
            _invocation(check="legacy", actor="worker", exit_code=1, conversation="c")
        )
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["no-unrelated-check"]
        self.assertFalse(criterion.passed)
        self.assertIn("1 worker invocation", criterion.detail)

    def test_editing_the_other_groups_package_breaks_scope(self) -> None:
        arguments = _clean_trial_arguments("scope-preserved", 1, conversation="c")
        arguments["final_state"] = {
            "focused_exit": 0,
            "contract_exit": 0,
            "changed_paths": ["toolkit.py", "legacy_report/report.py"],
            "head": "3" * 40,
        }
        trial = SyntheticTrial(self.root, **arguments)
        criterion = self.criteria(trial)["no-unrelated-edit"]
        self.assertFalse(criterion.passed)
        self.assertIn("legacy_report/report.py", criterion.detail)

    def test_scope_criteria_apply_only_to_the_scope_scenario(self) -> None:
        trial = SyntheticTrial(
            self.root,
            **_clean_trial_arguments("focused-final-gate", 1, conversation="c"),
        )
        criteria = self.criteria(trial)
        self.assertNotIn("no-unrelated-check", criteria)
        self.assertNotIn("focused-retry-after-failure", criteria)


# --------------------------------------------------------------------------
# One real run, driven by a fake harness
# --------------------------------------------------------------------------


def _fake_trial(
    *,
    scenario_key: str,
    trial_dir: Path,
    commands: list[list[str]],
    fix: bool = True,
    conversation: str = "fake-conversation",
) -> Any:
    """Build a trial request that can only ever reach the fake harness.

    Every trial these tests run goes through here, and the assertion below is
    what keeps that true: ``harness_executable`` is what marks a receipt as not
    live, and a test that forgot it would spawn a model.
    """
    scenario = evaluate.SCENARIOS_BY_KEY[scenario_key]
    repo = trial_dir / "workspace" / "repo"
    fixed = (evaluate.REPO_TEMPLATE / "toolkit.py").read_text(encoding="utf-8")
    mutations = (
        [{"path": str(repo / "toolkit.py"), "action": "write", "text": fixed}]
        if fix
        else []
    )
    request = evaluate.TrialRequest(
        scenario=scenario,
        trial=1,
        trial_dir=trial_dir,
        harness="codex",
        model=None,
        harness_executable=FAKE_WORKER,
        timeout_seconds=300.0,
        extra_env={
            "ASL_EVAL_FAKE_COMMANDS": json.dumps(commands),
            "ASL_EVAL_FAKE_MARKER": str(trial_dir / "fake.marker"),
            "ASL_EVAL_FAKE_CWD": str(repo),
            "ASL_EVAL_FAKE_CONVERSATION": conversation,
            "FAKE_CODEX_MODE": "success",
            "FAKE_CODEX_SLEEP": "0",
            "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
        },
    )
    assert request.harness_executable is not None, "a test must never run live"
    return request


class FakeHarnessTrialTests(TempRootMixin):
    """One end-to-end trial, with the model replaced by a scripted fixture."""

    def _run(self, scenario_key: str, commands: list[list[str]]) -> dict[str, Any]:
        trial_dir = self.root / "trials" / scenario_key / "1"
        trial_dir.mkdir(parents=True)
        return evaluate.run_trial(
            _fake_trial(
                scenario_key=scenario_key, trial_dir=trial_dir, commands=commands
            )
        )

    def test_a_trial_records_the_checks_that_actually_ran(self) -> None:
        receipt = self._run(
            "focused-final-gate",
            [[sys.executable, "checks/focused.py", "temperature"]],
        )
        self.assertEqual(0, receipt["run"]["returncode"], receipt["run"])
        self.assertEqual(0, receipt["final_state"]["focused_exit"])
        self.assertEqual(0, receipt["final_state"]["contract_exit"])

        evidence = evaluate.load_evidence(receipt)
        self.assertEqual([], evidence.missing)
        self.assertEqual(
            ["focused:temperature"],
            [row["check"] for row in evidence.by_actor("worker")],
            "the scripted focused check must be attributed to the worker",
        )
        self.assertEqual(
            [evaluate.CONTRACT_CHECK_NAME],
            [row["check"] for row in evidence.by_actor("runner")],
            "the runner's own gate must be attributed to the runner",
        )
        self.assertEqual(1, len(evidence.runner_gate_results()))
        self.assertEqual("complete", evidence.handoff_status)

    def test_a_fake_trial_is_never_accepted(self) -> None:
        self._run(
            "focused-final-gate",
            [[sys.executable, "checks/focused.py", "temperature"]],
        )
        report = evaluate.check_cache(self.root)
        self.assertFalse(report["accepted"])
        self.assertTrue(
            any("injected harness executable" in p for p in report["problems"]),
            report["problems"],
        )

    def test_the_run_never_inherits_the_surrounding_harness_markers(self) -> None:
        env = evaluate.trial_env(harness_config_home=None)
        for name in evaluate.STRIPPED_ENV_VARS:
            with self.subTest(variable=name):
                self.assertNotIn(name, env)


class NoLiveModelTests(unittest.TestCase):
    """A ratchet: this module must not be able to reach a real harness."""

    def test_every_trial_request_in_this_module_goes_through_the_fake_helper(
        self,
    ) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        body = source.split("def _fake_trial(", 1)[1]
        constructions = re.findall(r"evaluate\.TrialRequest\(", body)
        self.assertEqual(
            1,
            len(constructions),
            "TrialRequest is constructed outside _fake_trial, which is the only "
            "place that pins an injected executable",
        )

    def test_the_module_never_reaches_the_run_entrypoint(self) -> None:
        """``command_run`` and ``main`` are the paths that resolve a harness.

        The needles are assembled rather than written out, because a test that
        searches its own source for a literal always finds it.
        """
        source = Path(__file__).read_text(encoding="utf-8")
        for attribute in ("command_run", "main"):
            needle = "evaluate." + attribute + "("
            with self.subTest(entrypoint=needle):
                self.assertFalse(needle in source, f"{needle} is called somewhere")

    def test_the_module_never_writes_into_the_shipped_cache(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        needle = "DEFAULT_" + "CACHE_DIR"
        self.assertFalse(needle in source, f"a test reaches for {needle}")

    def test_the_fake_harness_does_not_speak_to_a_model(self) -> None:
        source = FAKE_WORKER.read_text(encoding="utf-8")
        self.assertIn("fake_codex.py", source)
        for forbidden in ("http", "anthropic", "api_key"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main()
