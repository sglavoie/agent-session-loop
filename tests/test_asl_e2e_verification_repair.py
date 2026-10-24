"""End-to-end packet mode and verification repair loop tests.

Split from tests/test_asl_e2e.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    config,
    events,
    plans_content_hash,
    plans_models,
    plans_readiness,
    review_delta,
    task_packet,
    verification_transitions,
)
from tests.asl_e2e_support import (
    run_to_completion,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    init_git_repo,
    install_fake_codex,
    write_plan,
)

ASL_TEST_LANE = "integration"


class VerificationRepairLoopTests(unittest.TestCase):
    """Packet mode + fingerprint-aware verification repair (Session 10).

    Covers three guarantees: autonomous runs use the Python-built task packet
    instead of the ``session-run`` skill (with an ``ASL_WORKER_MODE=skill``
    rollback), a relaunch injects prior-failure repair context into the packet,
    and an identical verification failure is fingerprinted and marked exhausted
    so it cannot loop while a changed failure stays eligible.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(self, *, path: str = "MISSING.md", name: str = "missing") -> None:
        contracts_by_id = {
            "1": _contract_block(
                f'{{"type": "file_exists", "path": "{path}", "name": "{name}"}}',
                session="1",
            )
        }
        write_plan(self.plan_dir, ("pending",), contracts_by_id=contracts_by_id)
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(
        self, *, worker_mode: str | None = None
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
            "--allow-not-loop-ready",
        ]
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        if worker_mode is not None:
            env["ASL_WORKER_MODE"] = worker_mode
        completed = run_to_completion(
            argv,
            cwd=REPO_ROOT,
            env=env,
            budget_seconds=20,
            label="run",
        )
        return completed, json.loads(completed.stdout)

    @staticmethod
    def _turn_prompt(payload: dict) -> str:
        # Codex passes the prompt as the final argv token.
        return str(payload["turns"][0]["argv"][-1])

    # --- packet vs skill prompt wiring -----------------------------------

    def test_packet_mode_is_default_and_avoids_session_run(self) -> None:
        self._write_plan(path="README.md", name="readme")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        prompt = self._turn_prompt(payload)
        self.assertTrue(prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX))
        self.assertNotIn("session-run", prompt)

    def test_skill_worker_mode_restores_session_run_invocation(self) -> None:
        self._write_plan(path="README.md", name="readme")

        completed, payload = self._run(worker_mode="skill")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        prompt = self._turn_prompt(payload)
        self.assertTrue(prompt.startswith("$session-run "))
        self.assertIn("--runner-managed", prompt)

    # --- fingerprint recording + repair context --------------------------

    def test_first_failure_records_fingerprint_and_attempt(self) -> None:
        self._write_plan()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertIsInstance(details["verification_fingerprint"], str)
        self.assertTrue(
            details["verification_fingerprint"].startswith("verify-failed:")
        )
        self.assertEqual(1, details["verification_repair_attempt"])
        self.assertFalse(details["verification_repair_exhausted"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual(
            details["verification_fingerprint"],
            verify_payload["verification_fingerprint"],
        )

    def test_relaunch_injects_prior_failure_into_packet(self) -> None:
        self._write_plan()

        _first_completed, first_payload = self._run()
        fingerprint = first_payload["pause_details"]["verification_fingerprint"]

        _second_completed, second_payload = self._run()

        prompt = self._turn_prompt(second_payload)
        self.assertTrue(prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX))
        self.assertIn("Verification repair references", prompt)
        self.assertIn("Prior verification attempt 1 failed", prompt)
        self.assertIn(fingerprint, prompt)
        self.assertIn("Failed check `missing`", prompt)

    def test_identical_failure_marks_repair_exhausted(self) -> None:
        self._write_plan()

        _first_completed, first_payload = self._run()
        _second_completed, second_payload = self._run()

        first_details = first_payload["pause_details"]
        second_details = second_payload["pause_details"]
        self.assertEqual(
            first_details["verification_fingerprint"],
            second_details["verification_fingerprint"],
        )
        self.assertEqual(2, second_details["verification_repair_attempt"])
        self.assertTrue(second_details["verification_repair_exhausted"])

    # --- pure helper coverage --------------------------------------------

    def test_fingerprint_is_stable_and_output_sensitive(self) -> None:
        base = {
            "failures": [
                {
                    "name": "unit",
                    "kind": "command",
                    "failure_reason": "command-failed",
                    "returncode": 1,
                    "stdout_sha256": "abc",
                    "stderr_sha256": "",
                }
            ]
        }
        same = json.loads(json.dumps(base))
        changed = json.loads(json.dumps(base))
        changed["failures"][0]["stdout_sha256"] = "def"

        self.assertEqual(
            verification_transitions.verification_fingerprint(base),
            verification_transitions.verification_fingerprint(same),
        )
        self.assertNotEqual(
            verification_transitions.verification_fingerprint(base),
            verification_transitions.verification_fingerprint(changed),
        )
        self.assertIsNone(
            verification_transitions.verification_fingerprint({"failures": []})
        )
        self.assertIsNone(verification_transitions.verification_fingerprint(None))

    def test_repair_exhaustion_decision(self) -> None:
        # An identical fingerprint that already appeared is exhausted.
        self.assertTrue(
            verification_transitions.is_verification_repair_exhausted(
                "verify-failed:x", ("verify-failed:x",), 2
            )
        )
        # A changed fingerprint under the attempt cap stays eligible.
        self.assertFalse(
            verification_transitions.is_verification_repair_exhausted(
                "verify-failed:y", ("verify-failed:x",), 2
            )
        )
        # Exceeding the attempt cap is exhausted regardless of fingerprint.
        self.assertTrue(
            verification_transitions.is_verification_repair_exhausted(
                "verify-failed:z",
                ("verify-failed:x", "verify-failed:y"),
                verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS + 1,
            )
        )
        # No fingerprint on the first attempt is not exhausted.
        self.assertFalse(
            verification_transitions.is_verification_repair_exhausted(None, (), 1)
        )

    def test_read_prior_verification_failures_filters_by_session(self) -> None:
        event_path = self.tmp_path / "events.jsonl"

        def _pause(session: str, reason: str, sha: str) -> None:
            events.append_event(
                event_path,
                events.Event(
                    name=events.RUN_PAUSED_EVENT,
                    payload={
                        "reason": reason,
                        "details": {
                            "session": session,
                            "failures": [
                                {
                                    "name": "missing",
                                    "kind": "file_exists",
                                    "failure_reason": "file-missing",
                                    "returncode": None,
                                    "stdout_sha256": sha,
                                    "stderr_sha256": "",
                                }
                            ],
                        },
                    },
                ),
            )

        _pause("1", "verify-failed", "a")
        _pause("2", "verify-failed", "b")
        _pause("1", "timeout", "c")
        _pause("1", "verify-failed", "d")

        priors = verification_transitions.read_prior_verification_failures(
            event_path, "1"
        )
        self.assertEqual(2, len(priors))
        self.assertEqual(
            ["a", "d"], [p["failures"][0]["stdout_sha256"] for p in priors]
        )

        state_info = review_delta._verification_repair_state(event_path, "1")
        self.assertEqual(3, state_info["attempt"])
        self.assertEqual(2, len(state_info["prior_fingerprints"]))
        references = state_info["references"]
        self.assertTrue(
            any("Prior verification attempt 2 failed" in ref for ref in references)
        )
        self.assertTrue(any("Failed check `missing`" in ref for ref in references))


if __name__ == "__main__":
    unittest.main()
