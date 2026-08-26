"""State compaction and harness failure classification tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    agent_mux_projection,
    artifacts,
    compact_state,
    events,
    harness_failures,
    harnesses,
    plans,
    runner,
    state,
)
from tests.support import (
    CLAUDE_SESSION_LIMIT_FIXTURE,
    CODEX_USAGE_LIMIT_FIXTURE,
    write_plan,
)


class StateCompactionTests(unittest.TestCase):
    """Cover ``asl compact-state`` v1 -> v2 record compaction."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.tmp_path / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True, exist_ok=True)
        self.big = "X" * 40000

    def _v1_turn(self) -> dict:
        return {
            "session_id": "1",
            "turn_index": 1,
            "before": {
                "counts": {"pending": 1},
                "sessions": [{"id": "1", "status": "pending"}],
            },
            "after": {
                "counts": {"complete": 1},
                "sessions": [{"id": "1", "status": "complete"}],
            },
            "verification": {
                "passed": False,
                "checks": [
                    {
                        "name": "c1",
                        "status": "fail",
                        "stdout": self.big,
                        "stderr": "",
                    }
                ],
            },
        }

    def _write_state(self, payload: dict) -> Path:
        path = self.plan_state / events.STATE_FILE_NAME
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def _write_events(self, records: list[dict]) -> Path:
        path = self.plan_state / events.EVENTS_FILE_NAME
        path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_result(self, checks: list[dict]) -> Path:
        turn_dir = self.plan_state / "runs" / "r" / "turn-0001-session-1"
        turn_dir.mkdir(parents=True, exist_ok=True)
        path = turn_dir / "result.json"
        path.write_text(
            json.dumps(
                {"session_id": "1", "verification": {"checks": checks}},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def test_dry_run_performs_no_writes(self) -> None:
        state_path = self._write_state({"run_id": "r", "turns": [self._v1_turn()]})
        before = state_path.read_bytes()
        result_path = self._write_result(
            [{"name": "c1", "status": "fail", "stdout": self.big, "stderr": "e"}]
        )
        result_before = result_path.read_bytes()

        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )

        self.assertFalse(report.applied)
        self.assertGreater(report.files_rewritten, 0)
        self.assertGreater(report.projected_bytes_reclaimed, 0)
        self.assertEqual(0, report.actual_bytes_reclaimed)
        # No file on disk changed, and no gzip artifact was written.
        self.assertEqual(before, state_path.read_bytes())
        self.assertEqual(result_before, result_path.read_bytes())
        self.assertFalse((result_path.parent / "verification").exists())

    def test_apply_compacts_state_to_schema_v2(self) -> None:
        state_path = self._write_state({"run_id": "r", "turns": [self._v1_turn()]})

        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        # Compaction performs the v2 output bounding and nothing more. It does
        # not attach an AgentMux projection, so it must not claim the v3 that
        # promises one; ``asl migrate-state`` is what upgrades this record.
        self.assertEqual(runner.COMPACT_STATE_SCHEMA_VERSION, payload["version"])
        self.assertNotIn(agent_mux_projection.PROJECTION_KEY, payload)
        turn = payload["turns"][0]
        self.assertNotIn("sessions", turn["before"])
        self.assertNotIn("sessions", turn["after"])
        check = turn["verification"]["checks"][0]
        self.assertNotIn("stdout", check)
        self.assertNotIn("stderr", check)

    def test_apply_keeps_schema_v3_when_a_projection_is_present(self) -> None:
        # A record a state transition already projected keeps its version and
        # its projection: compaction bounds output, it does not demote schemas.
        projection = {
            "schema_version": agent_mux_projection.PROJECTION_SCHEMA_VERSION,
            "run_id": "r",
        }
        state_path = self._write_state(
            {
                "run_id": "r",
                "turns": [self._v1_turn()],
                agent_mux_projection.PROJECTION_KEY: projection,
            }
        )

        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(runner.STATE_SCHEMA_VERSION, payload["version"])
        self.assertEqual(projection, payload[agent_mux_projection.PROJECTION_KEY])

    def test_compaction_preserves_unknown_top_level_run_config(self) -> None:
        # ``run_config`` is an additive top-level key the runner persists for
        # ``asl run --resume``; the compact projection must copy it through
        # unchanged so a resume after compaction still rehydrates.
        run_config = {
            "harness": "claude",
            "harness_model": "claude-opus-4",
            "effort": "high",
            "unsafe_bypass": True,
            "state_dir_override": "/tmp/state",
        }
        state_path = self._write_state(
            {"run_id": "r", "run_config": run_config, "turns": [self._v1_turn()]}
        )

        # Unit-level: the in-memory projection keeps the key verbatim.
        compact = runner._compact_state_payload(
            json.loads(state_path.read_text(encoding="utf-8"))
        )
        self.assertEqual(run_config, compact["run_config"])

        # On-disk: a real compaction round-trip preserves it too.
        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(run_config, payload["run_config"])

    def test_apply_extracts_failed_output_and_strips_pass_output(self) -> None:
        result_path = self._write_result(
            [
                {"name": "c1", "status": "fail", "stdout": self.big, "stderr": "boom"},
                {"name": "c2", "status": "pass", "stdout": self.big, "stderr": ""},
            ]
        )

        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        self.assertTrue(report.applied)
        self.assertEqual(
            report.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        # One passing stdout stream removed (lossy); failed stdout+stderr kept.
        self.assertEqual(1, report.pass_output_removed)
        self.assertEqual(2, report.failed_output_extracted)

        payload = json.loads(result_path.read_text(encoding="utf-8"))
        failed_check = payload["verification"]["checks"][0]
        # Full failed output is byte-for-byte recoverable from the gzip artifact.
        gzip_path = Path(failed_check["stdout_artifact_path"])
        self.assertTrue(gzip_path.exists())
        self.assertEqual(self.big, artifacts.read_gzip(gzip_path).decode("utf-8"))
        self.assertLess(len(failed_check["stdout"]), len(self.big))
        self.assertEqual(len(self.big), failed_check["stdout_byte_count"])
        # Passing output is discarded with only a preview kept — no artifact.
        pass_check = payload["verification"]["checks"][1]
        self.assertIsNone(pass_check["stdout_artifact_path"])
        self.assertLess(len(pass_check["stdout"]), len(self.big))

    def _state_dir_disk_bytes(self) -> int:
        return sum(
            path.stat().st_size for path in self.plan_state.rglob("*") if path.is_file()
        )

    def test_savings_are_net_of_retained_failure_artifacts(self) -> None:
        # Deterministic but poorly compressible failure output (hex digest
        # chain), so the retained gzip artifact stays large enough that
        # JSON-only accounting would visibly overstate the saving.
        digest = b"seed"
        chunks = []
        for _ in range(600):
            digest = hashlib.sha256(digest).digest()
            chunks.append(digest.hex())
        noisy = "".join(chunks)
        self._write_result(
            [{"name": "c1", "status": "fail", "stdout": noisy, "stderr": ""}]
        )
        disk_before = self._state_dir_disk_bytes()

        projected = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )
        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )
        disk_after = self._state_dir_disk_bytes()

        self.assertGreater(report.artifact_bytes_added, 0)
        # Reported savings equal the bytes actually reclaimed on disk,
        # which is strictly less than the JSON-only delta.
        self.assertEqual(disk_before - disk_after, report.actual_bytes_reclaimed)
        self.assertEqual(
            report.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        self.assertLess(
            report.actual_bytes_reclaimed,
            report.bytes_before - report.bytes_after,
        )
        # The dry-run projected the same net figure without writing anything.
        self.assertEqual(
            projected.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        self.assertEqual(report.artifact_bytes_added, projected.artifact_bytes_added)
        self.assertEqual(
            report.artifact_bytes_added, report.to_dict()["artifact_bytes_added"]
        )

    def test_colliding_check_names_keep_distinct_artifacts(self) -> None:
        # Each pair sanitizes to the same `a-b` stem; the second pair also
        # shares the first 8 hex chars of the SHA-256 digests of its
        # "-stdout" artifact names, so a short digest suffix still routed
        # both extractions to one path. The collision-resistant derivation
        # must keep the failure artifacts on distinct paths so the second
        # extraction cannot overwrite the first.
        pairs = (
            ("a/b", "a?b"),
            ("a$%#:??/b", "a%%#%$:/b"),
        )
        first = "first-output " + self.big
        second = "second-output " + self.big
        for first_name, second_name in pairs:
            with self.subTest(pair=(first_name, second_name)):
                result_path = self._write_result(
                    [
                        {
                            "name": first_name,
                            "status": "fail",
                            "stdout": first,
                            "stderr": "",
                        },
                        {
                            "name": second_name,
                            "status": "fail",
                            "stdout": second,
                            "stderr": "",
                        },
                    ]
                )

                projected = compact_state.compact_plan_state(
                    self.plan_dir, state_dir=self.state_dir, apply=False
                )
                report = compact_state.compact_plan_state(
                    self.plan_dir, state_dir=self.state_dir, apply=True
                )

                payload = json.loads(result_path.read_text(encoding="utf-8"))
                checks = payload["verification"]["checks"]
                paths = [Path(check["stdout_artifact_path"]) for check in checks]
                self.assertNotEqual(paths[0], paths[1])
                # Both checks' full output is retained byte-for-byte.
                self.assertEqual(first, artifacts.read_gzip(paths[0]).decode("utf-8"))
                self.assertEqual(second, artifacts.read_gzip(paths[1]).decode("utf-8"))
                # The dry-run projection matches the applied reclaim
                # byte-for-byte; before the fix both checks projected against
                # one shared target and the figures diverged.
                self.assertEqual(
                    projected.projected_bytes_reclaimed,
                    report.actual_bytes_reclaimed,
                )
                self.assertEqual(
                    projected.artifact_bytes_added, report.artifact_bytes_added
                )
                self.assertEqual(2, report.failed_output_extracted)

    def test_overlong_check_names_compact_without_aborting(self) -> None:
        # A ~192-char check name plus the "-stdout" stream suffix and the
        # full 64-hex digest used to push the gzip artifact (and its
        # ``.{name}.tmp`` atomic-write decoration) past the 255-byte
        # per-component limit, so applied compaction aborted with an
        # unhandled ArtifactError at the write. The shared byte-capped stem
        # must keep extraction bounded and recoverable, distinct per name,
        # with dry-run projected reclaim equal to the applied reclaim.
        first_name = "a" * 190 + "/x"
        second_name = "b" + "a" * 189 + "/x"
        first = "first-output " + self.big
        second = "second-output " + self.big
        result_path = self._write_result(
            [
                {
                    "name": first_name,
                    "status": "fail",
                    "stdout": first,
                    "stderr": "",
                },
                {
                    "name": second_name,
                    "status": "fail",
                    "stdout": second,
                    "stderr": "",
                },
            ]
        )

        projected = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )
        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        payload = json.loads(result_path.read_text(encoding="utf-8"))
        checks = payload["verification"]["checks"]
        paths = [Path(check["stdout_artifact_path"]) for check in checks]
        # Distinct long names keep distinct artifacts even after truncation.
        self.assertNotEqual(paths[0], paths[1])
        for path in paths:
            with self.subTest(artifact=path.name):
                self.assertTrue(path.exists())
                self.assertLessEqual(len(path.name.encode("utf-8")), 255)
                self.assertLessEqual(len(f".{path.name}.tmp".encode()), 255)
        # Both outputs stay byte-for-byte recoverable from the artifacts.
        self.assertEqual(first, artifacts.read_gzip(paths[0]).decode("utf-8"))
        self.assertEqual(second, artifacts.read_gzip(paths[1]).decode("utf-8"))
        self.assertEqual(2, report.failed_output_extracted)
        # Dry-run/apply parity holds for the long-name case.
        self.assertEqual(
            projected.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        self.assertEqual(projected.artifact_bytes_added, report.artifact_bytes_added)

    def test_events_compaction_preserves_order_and_pause_evidence(self) -> None:
        turn = self._v1_turn()
        events_path = self._write_events(
            [
                {"created_at": "t1", "name": "run_started", "payload": {"run_id": "r"}},
                {
                    "created_at": "t2",
                    "name": "run_paused",
                    "payload": {
                        "run_id": "r",
                        "reason": "verify-failed",
                        "message": "verification failed",
                        "details": {"checks": [{"name": "c1", "stdout": self.big}]},
                    },
                },
                {
                    "created_at": "t3",
                    "name": "run_finished",
                    "payload": {"run_id": "r", "ok": False, "turns": [turn]},
                },
            ]
        )

        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        lines = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(
            ["run_started", "run_paused", "run_finished"],
            [line["name"] for line in lines],
        )
        self.assertEqual(["t1", "t2", "t3"], [line["created_at"] for line in lines])
        # Pause evidence (reason/message) is kept; raw output is stripped.
        paused = lines[1]["payload"]
        self.assertEqual("verify-failed", paused["reason"])
        self.assertEqual("verification failed", paused["message"])
        self.assertNotIn("stdout", paused["details"]["checks"][0])
        # The finished event no longer embeds every turn.
        self.assertNotIn("turns", lines[2]["payload"])
        self.assertEqual("r", lines[2]["payload"]["run_id"])

    def test_apply_is_idempotent(self) -> None:
        self._write_state({"run_id": "r", "turns": [self._v1_turn()]})
        self._write_result(
            [{"name": "c1", "status": "fail", "stdout": self.big, "stderr": "e"}]
        )

        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )
        second = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        self.assertEqual(0, second.files_rewritten)
        self.assertEqual(0, second.projected_bytes_reclaimed)

    def test_fifty_session_synthetic_state_stays_under_one_megabyte(self) -> None:
        # Final acceptance criterion: a 50-session run's persisted state.json
        # must stay below 1 MB under the schema-v2 compact projection.
        session_rows = [
            {"id": str(number), "status": "complete", "path": f"session-{number}.md"}
            for number in range(1, 51)
        ]
        turns = []
        for number in range(1, 51):
            turn = self._v1_turn()
            turn["session_id"] = str(number)
            turn["turn_index"] = number
            turn["before"]["sessions"] = session_rows
            turn["after"]["sessions"] = session_rows
            turn["verification"]["checks"][0]["stderr"] = self.big
            turn["last_activity_preview"] = "preview " * 20
            turns.append(turn)
        payload = {
            "version": 2,
            "run_id": "r",
            "harness": "codex",
            "turns": turns,
            "pause_details": None,
        }

        compact = runner._compact_state_payload(payload)
        serialized = json.dumps(compact, sort_keys=True, indent=2) + "\n"

        self.assertLess(len(serialized.encode("utf-8")), 1_000_000)
        self.assertEqual(50, len(compact["turns"]))
        self.assertNotIn(self.big, serialized)

    def test_compacted_state_still_readable_by_status(self) -> None:
        self._write_state(
            {
                "run_id": "r",
                "harness": "codex",
                "active_turn": {"session_id": "1", "turn_index": 1},
                "turns": [self._v1_turn()],
            }
        )

        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        payload = state.status_payload(
            plans.inspect_plan(self.plan_dir), self.state_dir
        )
        self.assertTrue(payload["ok"])
        self.assertEqual("r", payload["runner_state"]["run_id"])

    def test_missing_state_directory_reports_nothing(self) -> None:
        empty_plan = self.tmp_path / "empty"
        write_plan(empty_plan, ("pending",))

        report = compact_state.compact_plan_state(
            empty_plan, state_dir=self.state_dir, apply=True
        )

        self.assertEqual(0, report.files_scanned)
        self.assertEqual(0, report.files_rewritten)
        self.assertEqual(0, report.projected_bytes_reclaimed)


class HarnessFailureClassificationTests(unittest.TestCase):
    """Stable, evidence-based classification of nonzero harness turns."""

    def _result(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        last_message: str = "",
        returncode: int = 1,
        interrupted: bool = False,
        timed_out: bool = False,
    ) -> harnesses.HarnessResult:
        return harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            last_message=last_message,
            interrupted=interrupted,
            timed_out=timed_out,
        )

    def test_fixture_files_exist(self) -> None:
        # The observed usage/session-limit fixtures must ship with the tests.
        self.assertTrue(CODEX_USAGE_LIMIT_FIXTURE.is_file())
        self.assertTrue(CLAUDE_SESSION_LIMIT_FIXTURE.is_file())

    def test_codex_usage_limit_fixture_is_quota_non_retryable(self) -> None:
        text = CODEX_USAGE_LIMIT_FIXTURE.read_text(encoding="utf-8")
        classification = runner.classify_harness_failure(self._result(stderr=text))
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertFalse(classification.retryable)
        # Reset time is parsed but does not gate retryability.
        self.assertIsNotNone(classification.retry_at)
        self.assertIn("2026-07-10T09:00:00Z", classification.retry_at)
        self.assertTrue(classification.evidence)

    def test_claude_session_limit_fixture_is_quota_non_retryable(self) -> None:
        text = CLAUDE_SESSION_LIMIT_FIXTURE.read_text(encoding="utf-8")
        classification = runner.classify_harness_failure(
            self._result(last_message=text)
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertFalse(classification.retryable)

    def test_rate_limit_and_429_are_quota_non_retryable(self) -> None:
        for text in (
            "Error: 429 Too Many Requests",
            "rate limit exceeded, slow down",
            "insufficient_quota: you have run out of credits",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_QUOTA_EXHAUSTED,
                    classification.reason,
                )
                self.assertFalse(classification.retryable)

    def test_authentication_failures_are_non_retryable(self) -> None:
        for text in (
            "Error: 401 Unauthorized",
            "authentication failed: invalid api key",
            "You are not logged in. Please sign in.",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)
                self.assertFalse(classification.retryable)

    def test_invalid_model_failures_are_non_retryable(self) -> None:
        for text in (
            "invalid model: gpt-9",
            "Error: model not found",
            "unknown model 'sonnet-99'",
            (
                "There's an issue with the selected model (gpt-9). It may not "
                "exist or you may not have access to it. Run --model to pick a "
                "different model."
            ),
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_INVALID_MODEL, classification.reason
                )
                self.assertFalse(classification.retryable)

    def test_capacity_failures_are_transport_retryable(self) -> None:
        for text in (
            '"message": "Selected model is at capacity. Please try a different model."',
            '"codex_error_info": "server_overloaded"',
            "overloaded_error: the model is currently busy",
            "stream error: Overloaded",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_TRANSPORT_FAILED,
                    classification.reason,
                )
                self.assertTrue(classification.retryable)
                self.assertEqual(
                    harness_failures.MODEL_CAPACITY_MARKER, classification.marker
                )

    def test_invalid_model_outranks_capacity(self) -> None:
        # A typo'd model name must still fail fast even when the same output
        # suggests trying a different model.
        classification = runner.classify_harness_failure(
            self._result(stderr="model not found; please try a different model")
        )
        self.assertEqual(events.PAUSE_REASON_INVALID_MODEL, classification.reason)
        self.assertFalse(classification.retryable)

    def test_oversized_prompt_is_named_without_becoming_retryable(self) -> None:
        for text in (
            "API Error: 400 {'message': 'prompt is too long: 210000 tokens'}",
            "input length exceeds the maximum of 200000 tokens",
            "context window exceeded",
            "Context length exceeded for this model",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                # Nothing here can be relaunched unchanged, so the reason and the
                # retryability are the fail-closed ones. Only the marker is new,
                # and it is stable rather than exit-code-derived so a caller that
                # can send less has something to branch on.
                self.assertEqual(
                    events.PAUSE_REASON_NONZERO_EXIT, classification.reason
                )
                self.assertFalse(classification.retryable)
                self.assertEqual(
                    harness_failures.CONTEXT_OVERFLOW_MARKER, classification.marker
                )
                self.assertEqual(
                    f"{events.PAUSE_REASON_NONZERO_EXIT}:"
                    f"{harness_failures.CONTEXT_OVERFLOW_MARKER}",
                    classification.fingerprint,
                )

    def test_oversized_prompt_outranks_the_vocabulary_it_carries(self) -> None:
        # The audit sends a transcript of ASL itself, so the refused request can
        # quote every word the quota and auth rules match on. The refusal is
        # about the request, not about the account, and must win the race.
        classification = runner.classify_harness_failure(
            self._result(
                stderr=(
                    "prompt is too long: 517000 tokens > 200000 maximum\n"
                    "context: turn quoted 'rate limit reached' and 'invalid api "
                    "key' and 'connection closed' from the run under audit"
                )
            )
        )
        self.assertEqual(
            harness_failures.CONTEXT_OVERFLOW_MARKER, classification.marker
        )
        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, classification.reason)
        self.assertFalse(classification.retryable)

    def test_transport_failures_are_retryable(self) -> None:
        for text in (
            "read ECONNRESET",
            "Error: connection reset by peer",
            "502 Bad Gateway",
            "service unavailable, try again",
            "stream disconnected unexpectedly",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_TRANSPORT_FAILED,
                    classification.reason,
                )
                self.assertTrue(classification.retryable)

    def test_unknown_nonzero_fails_closed(self) -> None:
        classification = runner.classify_harness_failure(
            self._result(stderr="fake codex failure", returncode=3)
        )
        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, classification.reason)
        self.assertFalse(classification.retryable)
        self.assertEqual(3, classification.returncode)
        self.assertIn("exit-3", classification.fingerprint)

    def test_interruption_is_classified_and_non_retryable(self) -> None:
        classification = runner.classify_harness_failure(
            self._result(returncode=130, interrupted=True)
        )
        self.assertEqual(events.PAUSE_REASON_INTERRUPTED, classification.reason)
        self.assertFalse(classification.retryable)

    def test_quota_precedes_transport_when_both_present(self) -> None:
        # A 429 rate-limit alongside a transient marker still classifies as
        # quota (non-retryable), never as a retryable transport failure.
        classification = runner.classify_harness_failure(
            self._result(
                stderr="429 rate limit; connection reset by peer",
            )
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertFalse(classification.retryable)

    def test_quota_without_reset_time_still_non_retryable(self) -> None:
        classification = runner.classify_harness_failure(
            self._result(stderr="you've hit your usage limit")
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertIsNone(classification.retry_at)
        self.assertFalse(classification.retryable)

    def test_fingerprint_is_normalized_and_stable(self) -> None:
        first = runner.classify_harness_failure(
            self._result(stderr="429 Too Many Requests at 10:00")
        )
        second = runner.classify_harness_failure(
            self._result(stderr="rate limit reached at 11:30")
        )
        # Same category -> same normalized fingerprint despite differing text.
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertTrue(first.fingerprint.startswith("quota-exhausted:"))

    def test_evidence_preview_is_bounded(self) -> None:
        noisy = "connection reset " + ("x" * 5000)
        classification = runner.classify_harness_failure(self._result(stderr=noisy))
        self.assertLessEqual(
            len(classification.evidence), runner.FAILURE_EVIDENCE_MAX_CHARS
        )

    def test_to_dict_is_json_serializable_and_complete(self) -> None:
        classification = runner.classify_harness_failure(
            self._result(stderr="401 Unauthorized")
        )
        payload = classification.to_dict()
        json.dumps(payload)
        self.assertEqual(
            {
                "reason",
                "retryable",
                "fingerprint",
                "evidence",
                "returncode",
                "retry_at",
                "marker",
            },
            set(payload),
        )

    def test_all_classifier_reasons_are_registered_pause_reasons(self) -> None:
        reasons = {
            events.PAUSE_REASON_NONZERO_EXIT,
            events.PAUSE_REASON_QUOTA_EXHAUSTED,
            events.PAUSE_REASON_AUTH_FAILED,
            events.PAUSE_REASON_INVALID_MODEL,
            events.PAUSE_REASON_TRANSPORT_FAILED,
            events.PAUSE_REASON_INTERRUPTED,
        }
        self.assertTrue(reasons.issubset(set(events.PAUSE_REASONS)))

    def test_only_transport_is_a_retryable_category(self) -> None:
        self.assertEqual(
            (events.PAUSE_REASON_TRANSPORT_FAILED,),
            events.RETRYABLE_HARNESS_FAILURE_REASONS,
        )

    def test_turn_status_result_uses_classified_reason(self) -> None:
        result = self._result(stderr="429 Too Many Requests")
        status = runner._turn_status_result(
            result=result,
            status_changes=(),
            pending_completed=(),
            session_id="5",
            runner_signals=runner._runner_signals(result),
            plan_error=None,
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, status)

    def test_turn_status_result_generic_failure_stays_nonzero_exit(self) -> None:
        result = self._result(stderr="fake codex failure")
        status = runner._turn_status_result(
            result=result,
            status_changes=(),
            pending_completed=(),
            session_id="5",
            runner_signals=runner._runner_signals(result),
            plan_error=None,
        )
        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, status)


if __name__ == "__main__":
    unittest.main()
