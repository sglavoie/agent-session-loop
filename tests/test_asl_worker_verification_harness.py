"""End-to-end worker verification using an explicitly injected fake harness."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any

from .support_asl_worker_verification import FAKE_WORKER, TempRootMixin, evaluate

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
    """A ratchet: no shard in this family may reach a real harness."""

    def sources(self) -> str:
        directory = Path(__file__).parent
        paths = sorted(directory.glob("test_asl_worker_verification*.py"))
        paths.append(directory / "support_asl_worker_verification.py")
        return "\n".join(path.read_text(encoding="utf-8") for path in paths)

    def test_every_trial_request_in_this_module_goes_through_the_fake_helper(
        self,
    ) -> None:
        constructions = re.findall(r"evaluate\.TrialRequest\(", self.sources())
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
        source = self.sources()
        for attribute in ("command_run", "main"):
            needle = "evaluate." + attribute + "("
            with self.subTest(entrypoint=needle):
                self.assertFalse(needle in source, f"{needle} is called somewhere")

    def test_the_module_never_writes_into_the_shipped_cache(self) -> None:
        source = self.sources()
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
