"""ParserAndStateTests (part 13).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path

from agent_session_loop import (
    commit_settlement,
    events,
    pause_policy,
    plans,
    readiness_gate,
    review_cycle,
    review_transitions,
    run_persistence,
    run_termination,
    state,
    state_locks,
    state_status,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_state_json_stays_under_one_megabyte_for_fifty_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            payload = self._base_state_payload(state_path)
            turns = []
            for index in range(50):
                turn = self._detailed_turn()
                turn["turn_index"] = index + 1
                turn["before"] = self._snapshot_dict(50)
                turn["after"] = self._snapshot_dict(50)
                turns.append(turn)
            payload["turns"] = turns
            run_persistence._write_state_payload(payload)

            size = state_path.stat().st_size

        self.assertLess(size, 1_000_000, f"state.json was {size} bytes")

    def test_run_finished_event_is_compact_without_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            persistence = run_persistence._RunPersistence(write_state=False)
            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]

        finished = [r for r in records if r["name"] == "run_finished"]
        self.assertEqual(1, len(finished))
        record = finished[0]
        self.assertEqual(events.EVENT_SCHEMA_VERSION, record["version"])
        event_payload = record["payload"]
        # Compact: run outcome, counts, review summary, control — but no turns.
        self.assertNotIn("turns", event_payload)
        self.assertEqual(payload["control"], event_payload["control"])
        self.assertEqual(1, event_payload["turn_count"])
        self.assertIsNotNone(event_payload["run_finished_at"])
        self.assertEqual(
            {"pending": 0, "complete": 1, "empty": 0, "unknown": 0},
            event_payload["counts"],
        )
        # Serialized event carries no embedded verification output.
        self.assertNotIn("x" * 8192, json.dumps(record))

    def test_run_finished_event_carries_observed_model_beside_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["model"] = "native CLI default"
            payload["model_source"] = "unresolved"
            payload["observed_model"] = "claude-opus-5"
            payload["observed_model_source"] = "harness_reported"
            persistence = run_persistence._RunPersistence(write_state=False)
            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]

        finished = [r for r in records if r["name"] == "run_finished"]
        event_payload = finished[0]["payload"]
        self.assertEqual("native CLI default", event_payload["model"])
        self.assertEqual("unresolved", event_payload["model_source"])
        self.assertEqual("claude-opus-5", event_payload["observed_model"])
        self.assertEqual("harness_reported", event_payload["observed_model_source"])

    def test_an_unresolved_request_resolves_to_what_every_turn_reported(self) -> None:
        projection = self._fold(self._turn(), self._turn())

        self.assertEqual(1, len(projection))
        record = projection[0]
        self.assertEqual("claude-opus-5", record["model"])
        self.assertEqual("harness_reported", record["model_source"])
        # The request itself is preserved: resolving it is not rewriting it.
        self.assertEqual("native CLI default", record["requested_model"])
        self.assertEqual("unresolved", record["requested_model_source"])
        self.assertEqual(2, record["turns"])

    def test_a_requested_model_outranks_what_the_harness_reported(self) -> None:
        projection = self._fold(
            self._turn(model="claude-opus-4-7", model_source="flag")
        )

        self.assertEqual("claude-opus-4-7", projection[0]["model"])
        self.assertEqual("flag", projection[0]["model_source"])
        self.assertEqual(["claude-opus-5"], projection[0]["observed_models"])

    def test_disagreeing_observations_leave_the_request_standing(self) -> None:
        projection = self._fold(
            self._turn(), self._turn(observed_model="claude-opus-4-7")
        )

        # Two answers are not an answer, so the role keeps its unresolved request.
        self.assertEqual("native CLI default", projection[0]["model"])
        self.assertEqual("unresolved", projection[0]["model_source"])
        self.assertEqual(
            ["claude-opus-4-7", "claude-opus-5"], projection[0]["observed_models"]
        )

    def test_a_review_harness_keeps_its_own_record(self) -> None:
        projection = self._fold(
            self._turn(),
            self._turn(
                harness="codex",
                session_kind="review",
                observed_model="gpt-5-codex",
            ),
        )

        self.assertEqual(
            [
                ("implementation", "claude", "claude-opus-5"),
                ("review", "codex", "gpt-5-codex"),
            ],
            [
                (record["session_kind"], record["harness"], record["model"])
                for record in projection
            ],
        )

    def test_a_turn_that_reported_nothing_cannot_erase_an_observation(self) -> None:
        projection = self._fold(self._turn(), self._turn(observed_model=None))

        self.assertEqual("claude-opus-5", projection[0]["model"])
        self.assertEqual(2, projection[0]["turns"])

    def test_action_required_clears_when_an_unpaused_run_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["action_required"] = self._live_action_required()
            persistence = run_persistence._RunPersistence(write_state=False)

            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

        self.assertIsNone(payload["action_required"])

    def test_action_required_survives_a_run_that_finishes_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["paused"] = True
            signal = self._live_action_required()
            payload["action_required"] = signal
            persistence = run_persistence._RunPersistence(write_state=False)

            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

        self.assertEqual(signal, payload["action_required"])

    def test_a_blocking_pause_names_the_command_that_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            repo_root = Path(tmp) / "repo"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason="not-loop-ready",
                message="plan is not loop-ready",
                details=self._baseline_missing_gate(plan_dir, repo_root),
            )

        action = payload["action_required"]
        self.assertEqual("not-loop-ready", action["reason"])
        self.assertEqual("pause", action["source"])
        # A readiness gate is blocked on the plan and its environment, not on
        # an answer, so the ask is to inspect (see ASI-114 / policy v3).
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, action["user_action"])
        hint = action["remediation"][0]
        self.assertEqual("baseline-missing.refresh", hint["check_id"])
        self.assertIn(
            f"`asl baseline {shlex.quote(str(plan_dir))} "
            f"--cwd {shlex.quote(str(repo_root))}`",
            hint["message"],
        )

    def test_a_nested_gate_payload_still_yields_its_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            repo_root = Path(tmp) / "repo"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                message="session 1 wrote outside its declared aliases",
                details={
                    "readiness_gate": self._baseline_missing_gate(plan_dir, repo_root),
                    "workspace_write_guard": {"violations": ["other-repo"]},
                },
            )

        remediation = payload["action_required"]["remediation"]
        self.assertEqual("baseline-missing.refresh", remediation[0]["check_id"])

    def test_a_pause_without_a_command_still_records_the_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason="blocked",
                message="RUNNER_PAUSE: blocked - needs a decision",
            )

        self.assertEqual("blocked", payload["action_required"]["reason"])
        self.assertEqual([], payload["action_required"]["remediation"])

    def test_a_retryable_pause_records_no_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason="verify-failed",
                message="verification failed",
            )

        self.assertIsNone(payload.get("action_required"))

    def test_a_live_signal_outranks_the_pause_derived_ask(self) -> None:
        # The pause names the same ask the turn already raised, so the turn's
        # record — the more specific of the two — stands. A pause the runner
        # declared instead retires it; see
        # ``run_termination._supersede_live_action_signal`` and ASI-114.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._base_state_payload(Path(tmp) / "state.json")
            signal = self._live_action_required()
            payload["action_required"] = signal
            run_termination.set_pause(
                payload,
                reason="runner-pause",
                message="RUNNER_PAUSE: blocked - needs a decision",
            )

        self.assertEqual(signal, payload["action_required"])
        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, payload)

    def test_unresolved_review_findings_survive_an_unpaused_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["review_fix_disposition"] = "exhausted"
            payload["review_verdict"] = {"outcome": "changes-requested"}
            persistence = run_persistence._RunPersistence(write_state=False)

            reason = review_cycle._terminal_review_reason(payload)
            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

        self.assertEqual(review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, reason)
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            payload["action_required"]["reason"],
        )

    def test_run_paused_event_and_state_bound_verification_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(state_path)
            persistence = run_persistence._RunPersistence()
            details = {
                "session": "1",
                "recipe": {"check_count": 1, "kinds": ["command"], "names": ["tests"]},
                "passed": False,
                "checks": [self._verification_check()],
                "failures": [self._verification_check()],
            }
            run_termination._pause_run(
                event_path,
                payload,
                reason="verify-failed",
                message="verification failed",
                details=details,
                persistence=persistence,
            )
            run_persistence._write_state_payload(payload)

            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        paused = [r for r in records if r["name"] == events.RUN_PAUSED_EVENT][0]
        event_check = paused["payload"]["details"]["checks"][0]
        self.assertNotIn("stdout", event_check)
        self.assertEqual(
            "/artifacts/tests-stdout.gz", event_check["stdout_artifact_path"]
        )
        event_failure = paused["payload"]["details"]["failures"][0]
        self.assertNotIn("stderr", event_failure)
        # Persisted state pause details are likewise bounded.
        state_check = persisted["pause_details"]["checks"][0]
        self.assertNotIn("stdout", state_check)
        self.assertEqual("command-failed", state_check["failure_reason"])
        # In-memory pause details retain full output for the run response JSON.
        self.assertIn("stdout", payload["pause_details"]["checks"][0])

    def test_v1_state_without_version_key_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True, exist_ok=True)
            state_path = plan_state / events.STATE_FILE_NAME
            # A pre-v2 state.json carries no ``version`` key.
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "legacy-run",
                        "harness": "codex",
                        "ok": True,
                        "paused": False,
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": {"session": 1, "turn_index": 1},
                        "active_verification": None,
                    }
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            # A run with a turn in flight holds its lock; without one the
            # status backfill would (correctly) declare the run abandoned.
            state_locks.acquire_lock(plan_dir, state_dir)
            try:
                payload = state_status.status_payload(snapshot, state_dir)
            finally:
                state_locks.release_lock(plan_dir, state_dir)

        self.assertEqual("legacy-run", payload["runner_state"]["run_id"])
        self.assertEqual(1, payload["active_turn"]["session"])
