"""Deterministic tests for the live redirect acceptance runner.

`tests/asl_live_redirect_acceptance.py` is the one module allowed to spend
provider quota. Nothing here launches a real harness turn: these tests pin its
opt-in gate, report shape, prompt construction, and the pause policy it relies
on. The version-window half lives in `test_asl_harness_compatibility`.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import events, pause_policy
from agent_session_loop import harness_compatibility as compat
from tests import asl_live_redirect_acceptance as live
from tests.asl_harness_compatibility_support import (
    _acceptance,
    _ExplodingAdapter,
    _version_script,
    _write_fake_cli,
)


class LiveAcceptanceOptInTests(unittest.TestCase):
    """`tests/asl_live_redirect_acceptance.py` must be inert until paid for.

    The live runner is the one module in this tree allowed to spend provider
    quota, so its default-off behaviour is not a convenience -- it is the reason
    the scheduled compatibility lane and this session's own gate can invoke it
    at all. These tests hold that line without ever launching a harness.
    """

    def setUp(self) -> None:
        compat.clear_probe_cache()
        self.addCleanup(compat.clear_probe_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        real = live.harnesses.get_adapter
        self.adapter_patch = unittest.mock.patch.object(
            live.harnesses,
            "get_adapter",
            lambda name: _ExplodingAdapter(real(name)),
        )
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)

    def _main(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with (
            unittest.mock.patch.dict(
                os.environ, {"PATH": str(self.tmp_path)}, clear=False
            ),
            unittest.mock.patch.dict(
                os.environ, {live.OPT_IN_ENV_VAR: ""}, clear=False
            ),
            redirect_stdout(buffer),
        ):
            code = live.main(argv)
        return code, buffer.getvalue()

    def test_opt_in_needs_an_explicit_switch(self) -> None:
        self.assertFalse(live.live_opt_in({}))
        self.assertFalse(live.live_opt_in({live.OPT_IN_ENV_VAR: ""}))
        self.assertFalse(live.live_opt_in({live.OPT_IN_ENV_VAR: "0"}))
        self.assertFalse(live.live_opt_in({live.OPT_IN_ENV_VAR: "maybe"}))
        self.assertTrue(live.live_opt_in({live.OPT_IN_ENV_VAR: "1"}))
        self.assertTrue(live.live_opt_in({live.OPT_IN_ENV_VAR: " YES "}))
        # The flag is the interactive half and needs no environment at all.
        self.assertTrue(live.live_opt_in({}, flag=True))

    def test_the_default_run_launches_nothing_and_succeeds(self) -> None:
        code, out = self._main(["--all-harnesses", "--json"])
        self.assertEqual(0, code)
        report = json.loads(out)
        self.assertIs(False, report["opted_in"])
        self.assertIs(False, report["model_use"])
        self.assertIs(True, report["ok"])
        self.assertEqual(0, report["counts"]["passed"])
        self.assertEqual(0, report["counts"]["failed"])
        self.assertEqual(
            len(live.SCENARIOS) * len(live.HARNESS_NAMES),
            report["counts"]["skipped"],
        )

    def test_every_scenario_is_reported_for_every_harness(self) -> None:
        # A missing row and a passing row are indistinguishable in a report that
        # lists only what it got to, so the matrix has to be complete.
        _code, out = self._main(["--all-harnesses", "--json"])
        report = json.loads(out)
        self.assertEqual(
            list(live.HARNESS_NAMES),
            [entry["harness"] for entry in report["harnesses"]],
        )
        for entry in report["harnesses"]:
            self.assertEqual(
                list(live.SCENARIO_KEYS),
                [row["scenario"] for row in entry["scenarios"]],
            )

    def test_skipping_says_which_switch_would_have_run_it(self) -> None:
        _code, out = self._main(["--all-harnesses"])
        self.assertIn("live acceptance is opt-in and was not enabled", out)
        self.assertIn("--live", out)
        self.assertIn(live.OPT_IN_ENV_VAR, out)

    def test_an_uninstalled_harness_is_skipped_rather_than_failed(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            acceptance = live.run_harness_acceptance("codex", opted_in=True)
        self.assertEqual(len(live.SCENARIOS), acceptance.skipped)
        self.assertEqual(0, acceptance.failed)
        self.assertTrue(
            any("not installed" in note for note in acceptance.limitations),
            acceptance.limitations,
        )

    def test_a_blocked_version_is_never_exercised(self) -> None:
        # _ExplodingAdapter is still installed: reaching the matrix at all with
        # a below-minimum binary would raise rather than quietly pass.
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.1.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            acceptance = live.run_harness_acceptance("codex", opted_in=True)
        self.assertEqual("incompatible", acceptance.compatibility)
        self.assertEqual(len(live.SCENARIOS), acceptance.skipped)
        self.assertTrue(
            any("known" in note for note in acceptance.limitations),
            acceptance.limitations,
        )


class LiveAcceptanceReportTests(unittest.TestCase):
    """The report must not let installation pass for verification."""

    def test_a_version_is_verified_only_when_a_scenario_passed(self) -> None:
        report = live.build_report(
            [
                _acceptance(
                    "codex",
                    version="0.149.1",
                    statuses={live.SCENARIO_STOPS: live.STATUS_PASSED},
                ),
                _acceptance(
                    "claude",
                    version="2.1.231",
                    statuses={live.SCENARIO_STOPS: live.STATUS_SKIPPED},
                ),
            ],
            opted_in=True,
            requested=["codex", "claude"],
            seconds=1.0,
        )
        # Installed and probed, but nothing ran against it: inventory, not proof.
        self.assertEqual("0.149.1", report["verified_versions"]["codex"])
        self.assertIsNone(report["verified_versions"]["claude"])

    def test_the_report_disclaims_the_versions_it_did_not_run(self) -> None:
        report = live.build_report(
            [
                _acceptance(
                    "codex",
                    version="0.149.1",
                    statuses={live.SCENARIO_STOPS: live.STATUS_PASSED},
                )
            ],
            opted_in=True,
            requested=["codex"],
            seconds=1.0,
        )
        self.assertIn("support policy, not", report["verification_note"])
        self.assertIn("no other version is claimed", report["verification_note"])
        # The policy window travels with the evidence so the two cannot be
        # conflated by a reader who only has the report.
        window = report["harnesses"][0]["policy_window"]
        self.assertEqual(compat.POLICY["codex"].latest, window["latest"])
        self.assertIn(report["verification_note"], live.format_report(report))

    def test_any_failure_makes_the_whole_report_fail(self) -> None:
        report = live.build_report(
            [
                _acceptance(
                    "codex",
                    version="0.149.1",
                    statuses={
                        live.SCENARIO_STOPS: live.STATUS_PASSED,
                        live.SCENARIO_FORCED: live.STATUS_FAILED,
                    },
                )
            ],
            opted_in=True,
            requested=["codex"],
            seconds=1.0,
        )
        self.assertIs(False, report["ok"])
        self.assertEqual(1, report["counts"]["failed"])

    def test_unreported_scenarios_fail_rather_than_vanish(self) -> None:
        matrix = live._Matrix()
        matrix.verdict(live.SCENARIO_STOPS, True, "stopped")
        matrix.fill(live.STATUS_FAILED, "the owning sequence did not report")
        results = {result.key: result for result in matrix.results()}
        self.assertEqual(len(live.SCENARIOS), len(results))
        # An already-recorded verdict is never overwritten by the sweep.
        self.assertEqual(live.STATUS_PASSED, results[live.SCENARIO_STOPS].status)
        self.assertEqual(live.STATUS_FAILED, results[live.SCENARIO_CONTEXT].status)


class LiveAcceptancePromptTests(unittest.TestCase):
    """The synthetic prompts have to be able to report failure."""

    def test_the_context_check_cannot_be_answered_from_disk(self) -> None:
        prompt = live.first_turn_prompt("PASSPHRASE-ABC", "NONCE-1")
        self.assertIn("PASSPHRASE-ABC", prompt)
        self.assertIn("Do not write the passphrase into any file", prompt)
        correction = live.correction_text()
        self.assertIn("do not search the filesystem", correction.lower())
        self.assertIn("it was never written to disk", correction)

    def test_the_worker_is_given_an_honest_way_to_fail(self) -> None:
        # Without this, a worker that lost the conversation would be pushed to
        # invent a passphrase, and the scenario would measure guessing.
        self.assertIn("SECRET=UNKNOWN", live.correction_text())

    def test_the_resumed_prompt_carries_the_journal_block_verbatim(self) -> None:
        prompt = live.resume_turn_prompt(["FIRST LINE", "SECOND LINE"])
        self.assertIn("FIRST LINE\nSECOND LINE", prompt)

    def test_a_provider_refusal_is_not_a_harness_finding(self) -> None:
        # Found by the first live run against copilot: an exhausted monthly
        # quota comes back as exit status 0 with the refusal in the *final
        # message*, which every field a runner normally reads calls a success.
        # Uncaught, it became six confident findings about protocol behaviour
        # in a harness that never ran a single tool.
        quota = "Error: You have exceeded your monthly quota (Request ID: D366)"
        self.assertEqual("exceeded your monthly quota", live.provider_refusal(quota))
        self.assertEqual("unauthorized", live.provider_refusal("401 Unauthorized"))
        self.assertIsNone(live.provider_refusal("READY"))

    def test_an_unreachable_harness_is_skipped_not_failed(self) -> None:
        # Skipped rather than failed, because nothing was learned: a gate that
        # went red here would be reporting the state of somebody's account.
        acceptance = live.HarnessAcceptance(
            harness="copilot",  # type: ignore[arg-type]
            executable="/opt/homebrew/bin/copilot",
            version="1.0.83",
            compatibility="unknown",
            compatibility_reason="newer-than-tested",
            capabilities={"reachable": False},
            results=[
                live.ScenarioResult(key=key, status=live.STATUS_SKIPPED, detail="quota")
                for key in live.SCENARIO_KEYS
            ],
            limitations=["copilot: the provider refused the turn"],
            seconds=1.0,
        )
        report = live.build_report(
            [acceptance], opted_in=True, requested=["copilot"], seconds=1.0
        )
        self.assertIs(True, report["ok"])
        self.assertEqual(0, report["counts"]["failed"])
        # And it is still not claimed verified.
        self.assertIsNone(report["verified_versions"]["copilot"])
        self.assertIn("copilot: the provider refused", report["limitations"][0])

    def test_the_readiness_turn_asks_for_no_tools(self) -> None:
        prompt = live.ready_prompt()
        self.assertIn(live.READY_TOKEN, prompt)
        self.assertIn("do not write any files", prompt.lower())

    def test_a_setup_error_cannot_pass_for_a_refused_conversation(self) -> None:
        # Found by the first live run: `plan_dir` was missing, the adapter
        # raised, and the blanket handler scored it a pass -- which made the row
        # easier to satisfy the more broken the environment was.
        unknown = "0f0e0d0c-0b0a-0908-0706-050403020100"
        self.assertFalse(
            live._mentions_conversation(
                "codex worker turns require a plan_dir", unknown
            )
        )
        self.assertTrue(
            live._mentions_conversation(f"no conversation {unknown}", unknown)
        )
        self.assertTrue(
            live._mentions_conversation("could not resume that thread", unknown)
        )

    def test_the_receipt_parser_tolerates_worker_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resumed.txt"
            path.write_text(
                'secret = PASSPHRASE-ABC\nATTEMPT="run/19/2"\nOPERATION=`op-1`\n',
                encoding="utf-8",
            )
            values = live._read_lines(path)
        self.assertEqual("PASSPHRASE-ABC", values["SECRET"])
        self.assertEqual("run/19/2", values["ATTEMPT"])
        self.assertEqual("op-1", values["OPERATION"])
        self.assertEqual({}, live._read_lines(Path(tmp) / "absent.txt"))


class LiveAcceptancePolicyTests(unittest.TestCase):
    """The half of `unavailable-history` that no live turn can show.

    The live matrix can prove the real binary refuses a conversation it never
    saw. It cannot prove what ASL then does about it, because that decision is
    made by the run loop and not by the harness -- so the runner's report points
    here, and here is where the claim is actually pinned.
    """

    def test_missing_history_stops_for_a_person_instead_of_retrying(self) -> None:
        decision = pause_policy.classify_pause(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
        )
        self.assertIs(True, decision["requires_user"])
        self.assertIs(False, decision["recoverable"])
        self.assertEqual(0, decision["max_retries"])
        # `inspect`, not `reply`: a mailbox answer cannot restore lost context,
        # and authorising a fresh conversation discards it.
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, decision["user_action"])
        self.assertIn(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
            pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS,
        )


if __name__ == "__main__":
    unittest.main()
