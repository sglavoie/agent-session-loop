"""Turn anomaly, reopened readiness, review attestation, and validator diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    contracts,
    events,
    plans_session_ids,
    review,
    review_session_contract,
    supervisor_audit_evidence_collector,
    supervisor_audit_evidence_constants,
    supervisor_audit_evidence_models,
    verification_checks,
    verification_cli,
    verification_diagnostics,
)
from tests.support_supervisor_audit import RUN_ID, RunHistoryFixture


class TurnAnomalyClassificationTests(RunHistoryFixture, unittest.TestCase):
    """A clean harness exit cannot conceal a failed gate or a refused turn."""

    def _turn(self, history: tuple[Path, Path, Path, Path]) -> dict[str, Any]:
        return self._bundle(history).payload["turns"][0]

    def test_a_failed_gate_under_a_zero_exit_is_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, _, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["verification"] = {
                "passed": False,
                "checks": [{"name": "types", "passed": False, "returncode": 1}],
                "skipped_checks": [],
            }
            self._write_result(turn_dir, result)

            turn = self._turn(history)

            self.assertEqual(0, turn["result"]["returncode"])
            self.assertTrue(turn["anomalous"])

    def test_a_turn_the_guard_refused_is_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, state_dir, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["turn_index"] = 1
            result["status_result"] = "complete"
            self._write_result(turn_dir, result)
            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_REJECTED_EVENT,
                        {"run_id": RUN_ID, "turn_index": 1, "reopened": True},
                    ),
                    (
                        events.SESSION_REOPENED_EVENT,
                        {"run_id": RUN_ID, "turn_index": 1},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            turn = self._turn(history)

            self.assertEqual("complete", turn["result"]["status_result"])
            self.assertTrue(turn["anomalous"])
            self.assertTrue(turn["rejected"])

    def test_a_rejection_naming_another_turn_leaves_this_one_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, state_dir, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["turn_index"] = 1
            self._write_result(turn_dir, result)
            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_REJECTED_EVENT,
                        {"run_id": RUN_ID, "turn_index": 7, "reopened": True},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            turn = self._turn(history)

            self.assertFalse(turn["anomalous"])
            self.assertNotIn("rejected", turn)

    def test_a_clean_turn_stays_unremarkable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            turn = self._turn(self._history(Path(tmp)))

            self.assertFalse(turn["anomalous"])
            self.assertNotIn("rejected", turn)

    def _deferred_turn(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        pause_details: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Write a turn whose broad-check failures a findings review deferred."""
        workspace, _, state_dir, run_dir = history
        turn_dir = run_dir / "turn-0002-01"
        turn_dir.mkdir()
        result = self._clean_result(workspace, turn_dir)
        result["turn_index"] = 2
        result["status_result"] = "complete"
        result["verification"] = {
            "passed": False,
            "session": "2",
            "checks": [{"name": "broad:tests", "passed": False, "returncode": 1}],
            "skipped_checks": [],
            "deferred_failures": [{"name": "broad:tests"}],
            "acceptance": {
                "accepted": True,
                "reason": "findings-defer-broad-failures",
                "review_outcome": "findings",
                "deferred_failure_names": ["broad:tests"],
                "deferred_failure_count": 1,
            },
        }
        self._write_result(turn_dir, result)
        state_payload = self._clean_state(workspace)
        records: list[tuple[str, dict[str, Any]]] = [
            ("run_started", {"run_id": RUN_ID})
        ]
        if pause_details is None:
            records.append((events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}))
        else:
            state_payload["ok"] = False
            state_payload["paused"] = True
            state_payload["pause_reason"] = (
                events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE
            )
            state_payload["pause_details"] = pause_details
            records.append(
                (
                    events.RUN_PAUSED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "reason": events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE,
                        "details": pause_details,
                    },
                )
            )
        self._write_state(state_dir, state_payload)
        self._write_events(state_dir, records)
        return self._bundle(history).payload["turns"][1]

    def test_a_deferral_the_run_never_redeemed_is_anomalous(self) -> None:
        """ASI-15: the exemption dies with the promise that bought it."""
        with tempfile.TemporaryDirectory() as tmp:
            turn = self._deferred_turn(
                self._history(Path(tmp)),
                pause_details={
                    "deferred_unresolved": [
                        {
                            "run_id": RUN_ID,
                            "session_id": "2",
                            "turn_index": 2,
                            "checks": ["broad:tests"],
                            "review_outcome": "findings",
                        }
                    ],
                    "unpassed_verifications": [],
                },
            )

            self.assertEqual(0, turn["result"]["returncode"])
            self.assertTrue(turn["anomalous"])
            self.assertTrue(turn["deferral_unredeemed"])
            self.assertEqual(["broad:tests"], turn["unredeemed_checks"])

    def test_a_deferral_no_pause_disputes_stays_unremarkable(self) -> None:
        """A run that never declared the deferral unresolved keeps the exemption."""
        with tempfile.TemporaryDirectory() as tmp:
            turn = self._deferred_turn(self._history(Path(tmp)), pause_details=None)

            self.assertFalse(turn["anomalous"])
            self.assertNotIn("deferral_unredeemed", turn)

    def test_another_runs_unredeemed_deferral_leaves_this_turn_alone(self) -> None:
        """Turn indexes are per-run, so a foreign entry may not select by one."""
        with tempfile.TemporaryDirectory() as tmp:
            turn = self._deferred_turn(
                self._history(Path(tmp)),
                pause_details={
                    "deferred_unresolved": [
                        {
                            "run_id": "20260101T000000000000Z-0001",
                            "session_id": "2",
                            "turn_index": 2,
                            "checks": ["broad:tests"],
                            "review_outcome": "findings",
                        }
                    ],
                    "unpassed_verifications": [],
                },
            )

            self.assertFalse(turn["anomalous"])
            self.assertNotIn("deferral_unredeemed", turn)


class ReopenedReadinessProjectionTests(RunHistoryFixture, unittest.TestCase):
    """The re-gate verdict that licenses a grown plan has to reach the audit."""

    def _regated_bundle(
        self,
        tmp: str,
        *,
        trigger: str = events.REVIEW_REPAIR_REOPENED_EVENT,
        repair_session_ids: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        history = self._history(Path(tmp))
        payload = self._read_state(history[2])
        payload["readiness_gate"] = {"ok": True, "core_skipped": True, "findings": []}
        gate: dict[str, Any] = {
            "trigger": trigger,
            "ok": True,
            "total_sessions": 20,
            "pending_sessions": 3,
            "repair_session_ids": (
                ["7.1", "7.2", "7.3"]
                if repair_session_ids is None
                else repair_session_ids
            ),
            "findings": [],
        }
        gate.update(details or {})
        payload["readiness_gate_reopened"] = gate
        self._write_state(history[2], payload)
        return self._bundle(history).payload

    def test_the_reopen_regate_verdict_reaches_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = self._regated_bundle(tmp)["status"]

            gate = status["readiness_gate_reopened"]
            self.assertTrue(gate["ok"])
            self.assertEqual(20, gate["total_sessions"])
            self.assertEqual(["7.1", "7.2", "7.3"], gate["repair_session_ids"])
            # The stale gate stays visible beside it: an auditor that sees only
            # core_skipped cannot tell a re-gated plan from an unauthorized one.
            self.assertTrue(status["readiness_gate"]["core_skipped"])

    def test_the_review_append_regate_verdict_reaches_the_audit(self) -> None:
        # Appending a generated review session grows the plan the same way
        # materializing repairs does, and it re-gates under its own trigger.
        # An auditor reading the bundle has to be able to tell which growth
        # the verdict authorized, so the trigger has to survive the projection.
        with tempfile.TemporaryDirectory() as tmp:
            status = self._regated_bundle(
                tmp,
                trigger=events.REVIEW_SESSION_CREATED_EVENT,
                repair_session_ids=[],
                details={"review_session_id": "20"},
            )["status"]

            gate = status["readiness_gate_reopened"]
            self.assertEqual(events.REVIEW_SESSION_CREATED_EVENT, gate["trigger"])
            self.assertNotEqual(events.REVIEW_REPAIR_REOPENED_EVENT, gate["trigger"])
            self.assertTrue(gate["ok"])
            # No repair children grew this plan; the appended review did.
            self.assertEqual([], gate["repair_session_ids"])
            self.assertEqual("20", gate["review_session_id"])
            self.assertTrue(status["readiness_gate"]["core_skipped"])

    def test_a_run_that_never_reopened_projects_no_regate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            self.assertNotIn(
                "readiness_gate_reopened", self._bundle(history).payload["status"]
            )


class ReviewAttestationProjectionTests(RunHistoryFixture, unittest.TestCase):
    """A review that ran without committing must still be auditable."""

    def test_the_attested_review_reaches_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_requested"] = True
            payload["review_completed"] = False
            payload["review_skipped_reason"] = None
            payload["review_session_executed"] = "11"
            payload["review_verdict"] = {
                "status_line": "Status: 3 findings",
                "outcome": "findings",
                "total_findings": 3,
                "findings_by_severity": {"medium": 3},
            }
            payload["review_output_sha256"] = "a" * 64
            payload["review_artifact_sha256"] = "b" * 64
            self._write_state(history[2], payload)

            section = self._bundle(history).payload["review"]

            self.assertEqual("11", section["review_session_executed"])
            self.assertFalse(section["review_completed"])
            self.assertIsNone(section["review_skipped_reason"])
            self.assertEqual(3, section["review_verdict"]["total_findings"])
            self.assertEqual("a" * 64, section["review_output_sha256"])
            self.assertEqual("b" * 64, section["review_artifact_sha256"])


class ValidatorDiagnosticProjectionTests(RunHistoryFixture, unittest.TestCase):
    """ASI-105: a rejected review gate must say which requirement failed.

    Its structured reason used to reach the bundle three ways and be lost in
    all three: the inline ``detail`` truncated behind a ~2 KB ``python3 -c``
    argv, the ``stderr`` preview content-stubbed, and the artifact holding the
    full stream named but never indexed as evidence.
    """

    def _rejected_gate(self, history: tuple[Path, Path, Path, Path]) -> dict[str, Any]:
        """Run the real generated gate against a heading-less review artifact.

        The command is taken from the runner's own rendering and stamped by the
        runner's own authenticator, because a diagnostic only reaches a bundle
        for a gate the runner generated.
        """
        _, plan_dir, _, run_dir = history
        review_path = plan_dir / review.FINAL_REVIEW_FILENAME
        review_path.write_text("no heading anywhere here\n", encoding="utf-8")
        payload = next(
            item
            for item in review_session_contract._generated_review_gate_payloads(
                plan_dir=plan_dir
            )
            if item["name"] == "final-review-output-evidence-based"
        )
        contract = review_session_contract._authenticated_generated_review_contract(
            contracts.CompletionContract(
                session_id=plans_session_ids.parse_session_id("1"),
                done_when=("reviewed",),
                verify=contracts.VerifyRecipe(
                    checks=(
                        contracts.CommandCheck(
                            name=payload["name"],
                            argv=tuple(payload["argv"]),
                            timeout_seconds=float(payload["timeout_seconds"]),
                            cwd=payload["cwd"],
                        ),
                    )
                ),
            ),
            plan_dir=plan_dir,
        )
        turn_dir = run_dir / "turn-0001-01"
        result = verification_checks.run_check(
            contract.verify.checks[0],
            repo_root=run_dir,
            artifact_dir=turn_dir / "verification",
        )
        self.assertEqual("fail", result.status)
        payload = self._read_result(run_dir)
        payload["verification"] = {
            "passed": False,
            "checks": [verification_cli._check_result_payload(result)],
        }
        self._write_result(turn_dir, payload)
        return verification_cli._check_result_payload(result)

    def test_the_bundle_carries_the_reason_the_streams_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            recorded = self._rejected_gate(history)

            check = self._bundle(history).payload["turns"][0]["result"]["verification"][
                "checks"
            ][0]

            self.assertEqual(
                {
                    "check": "final-review-output-evidence-based",
                    "reason": "missing-required-heading",
                },
                check["failure_diagnostic"],
            )
            # The two surfaces it had to survive: the stream is still withheld
            # as application content, and the detail no longer spends its inline
            # budget on the gate's own kilobytes of argv, so the reason arrives
            # whole instead of at the edge of a truncation.
            self.assertIn(
                supervisor_audit_evidence_constants.REASON_APPLICATION_CONTENT,
                check["stderr"],
            )
            self.assertIn("missing-required-heading", check["detail"])
            self.assertNotIn("path_re", check["detail"])
            self.assertEqual(recorded["detail"], check["detail"])
            self.assertLess(
                len(check["detail"]),
                supervisor_audit_evidence_constants.MAX_INLINE_STRING_BYTES,
            )

    def test_the_failed_gate_stderr_artifact_is_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._rejected_gate(history)

            evidence = self._bundle(history).payload["evidence"]

            entry = next(
                item
                for item in evidence
                if item["locator"].endswith(
                    "final-review-output-evidence-based-stderr.gz"
                )
            )
            self.assertEqual("artifact", entry["kind"])
            self.assertEqual(
                "final-review-output-evidence-based stderr", entry["detail"]
            )

    def test_a_passing_check_indexes_no_stream_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            evidence = self._bundle(history).payload["evidence"]

            self.assertFalse([item for item in evidence if ".gz" in item["locator"]])

    # --- Application-content boundary on failed-command diagnostics ---
    # A rejection reaching the bundle as structure is a promotion, and only
    # ASL's own gates are promoted. Neither what a forger attaches to a copied
    # marker nor what an ordinary failed command prints may cross with it.

    APPLICATION_TEXT = "internal proprietary application text"

    #: Application content shaped exactly like a generated gate's own values.
    APPLICATION_TOKEN = "INTERNAL_PROPRIETARY_TOKEN_ABC123"

    #: A name the runner gives one of its generated review gates.
    GATE = "final-review-artifact-consistent"

    def _recorded_command(
        self,
        history: tuple[Path, Path, Path, Path],
        stderr_line: str,
        *,
        name: str = "user-authored-command",
        generated_gate: bool = False,
    ) -> dict[str, Any]:
        """Record one real failed command and project it through a bundle."""
        _, _, _, run_dir = history
        turn_dir = run_dir / "turn-0001-01"
        result = verification_checks.run_check(
            contracts.CommandCheck(
                name=name,
                argv=(
                    "python3",
                    "-c",
                    f"import sys; sys.stderr.write({stderr_line!r}); sys.exit(1)",
                ),
                timeout_seconds=60.0,
                generated_gate=generated_gate,
            ),
            repo_root=run_dir,
            artifact_dir=turn_dir / "verification",
        )
        payload = self._read_result(run_dir)
        payload["verification"] = {
            "passed": False,
            "checks": [verification_cli._check_result_payload(result)],
        }
        self._write_result(turn_dir, payload)
        return self._bundle(history).payload["turns"][0]["result"]["verification"][
            "checks"
        ][0]

    def _marked_line(self, **fields: Any) -> str:
        """One rejection line carrying the exact public marker literal."""
        return (
            json.dumps(
                {
                    **fields,
                    verification_diagnostics.FAILURE_DIAGNOSTIC_MARKER_KEY: (
                        verification_diagnostics.FAILURE_DIAGNOSTIC_MARKER_VALUE
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )

    def test_a_gates_extra_field_is_withheld_as_application_content(self) -> None:
        line = self._marked_line(
            check=self.GATE,
            reason="artifact-unparsable",
            customer_payload=self.APPLICATION_TEXT,
        )
        with tempfile.TemporaryDirectory() as tmp:
            check = self._recorded_command(
                self._history(Path(tmp)), line, name=self.GATE, generated_gate=True
            )

            diagnostic = check["failure_diagnostic"]
            self.assertEqual(self.GATE, diagnostic["check"])
            self.assertEqual("artifact-unparsable", diagnostic["reason"])
            self.assertIn(
                supervisor_audit_evidence_constants.REASON_APPLICATION_CONTENT,
                diagnostic["customer_payload"],
            )
            self.assertNotIn(self.APPLICATION_TEXT, json.dumps(check))

    def test_a_copied_marker_carries_no_application_token(self) -> None:
        """Copying the public marker buys neither provenance nor a content route.

        The forger's own check is not a gate the runner generates, and the
        token is shaped like a gate's own values, so the shape bound that used
        to guard these two fields would have let it through. What denies it is
        the closed vocabulary, on both the parse side and the collector's.
        """
        cases = {
            "token-in-reason": {
                "check": "user-authored-command",
                "reason": self.APPLICATION_TOKEN,
            },
            "token-in-check": {
                "check": self.APPLICATION_TOKEN,
                "reason": "artifact-missing",
            },
        }
        for label, fields in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                check = self._recorded_command(
                    self._history(Path(tmp)), self._marked_line(**fields)
                )

                self.assertIsNone(check["failure_diagnostic"])
                self.assertEqual("command exited 1: python3", check["detail"])
                self.assertNotIn(self.APPLICATION_TOKEN, json.dumps(check))

    def test_a_generated_gate_name_reaches_the_bundle_as_nothing(self) -> None:
        """The collision the finding describes, projected all the way through.

        A hand-authored command may be named after a generated gate and may
        print that gate's marker and one of its real reasons; only the runner's
        own recipe is a gate, so the bundle carries neither the diagnostic nor
        a diagnostic-led detail.
        """
        line = self._marked_line(check=self.GATE, reason="artifact-missing")
        with tempfile.TemporaryDirectory() as tmp:
            check = self._recorded_command(
                self._history(Path(tmp)), line, name=self.GATE
            )

            self.assertIsNone(check["failure_diagnostic"])
            self.assertEqual("command exited 1: python3", check["detail"])

    def test_the_collector_stubs_a_token_in_either_diagnostic_field(self) -> None:
        """The boundary holds for a diagnostic the collector did not parse.

        ``_Collector.redact`` is the last gate before the bundle and is reached
        by recorded evidence the current parser never saw, so it re-derives the
        vocabulary rather than trusting that promotion already checked it.
        """
        roots = supervisor_audit_evidence_models.EvidenceRoots.build(
            run_dir=Path.cwd(), state_dir=Path.cwd(), plan_dir=Path.cwd()
        )
        for field in ("check", "reason"):
            with self.subTest(field=field):
                diagnostic = {"check": self.GATE, "reason": "artifact-missing"}
                diagnostic[field] = self.APPLICATION_TOKEN

                projected = supervisor_audit_evidence_collector._Collector(
                    roots
                ).redact({"failure_diagnostic": diagnostic})

                self.assertNotIn(self.APPLICATION_TOKEN, json.dumps(projected))
                self.assertIn(
                    supervisor_audit_evidence_constants.REASON_APPLICATION_CONTENT,
                    projected["failure_diagnostic"][field],
                )

    def test_the_collector_withholds_a_nested_diagnostic_field(self) -> None:
        """A vocabulary is a set, and a recorded bundle may hold anything.

        The collector reads evidence the current parser never produced, so a
        control-plane field holding a structure has to be withheld rather than
        raising on the membership test that guards it.
        """
        roots = supervisor_audit_evidence_models.EvidenceRoots.build(
            run_dir=Path.cwd(), state_dir=Path.cwd(), plan_dir=Path.cwd()
        )

        projected = supervisor_audit_evidence_collector._Collector(roots).redact(
            {
                "failure_diagnostic": {
                    "check": {"nested": self.APPLICATION_TEXT},
                    "reason": ["artifact-missing"],
                }
            }
        )

        self.assertNotIn(self.APPLICATION_TEXT, json.dumps(projected))
        for field in ("check", "reason"):
            self.assertIn(
                supervisor_audit_evidence_constants.REASON_APPLICATION_CONTENT,
                projected["failure_diagnostic"][field],
            )

    def test_a_plain_failed_command_leaves_no_text_in_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._recorded_command(
                self._history(Path(tmp)), f"{self.APPLICATION_TEXT}\n"
            )

            self.assertIsNone(check["failure_diagnostic"])
            self.assertEqual("command exited 1: python3", check["detail"])
            self.assertIn(
                supervisor_audit_evidence_constants.REASON_APPLICATION_CONTENT,
                check["stderr"],
            )
            self.assertNotIn(self.APPLICATION_TEXT, json.dumps(check))


if __name__ == "__main__":
    unittest.main()
