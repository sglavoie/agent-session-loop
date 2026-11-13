"""Projections tests extracted from test_asl_run_persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    agent_mux_projection,
    compact_state,
    events,
    recovery_card_state,
    run_persistence,
    state_payloads,
)


class CompactStateTests(unittest.TestCase):
    """A compact record bounds what it embeds and claims only what it carries."""

    def test_raw_verification_output_never_reaches_a_compact_check(self) -> None:
        check = {
            "name": "suite",
            "stdout": "x" * 4096,
            "stderr": "boom",
            "stdout_artifact_path": "/runs/1/suite.stdout",
            "stdout_byte_count": 4096,
            "stdout_sha256": "abc",
        }
        stripped = run_persistence._strip_check_output(check)
        self.assertNotIn("stdout", stripped)
        self.assertNotIn("stderr", stripped)
        # The metadata that makes the full output recoverable survives.
        self.assertEqual("/runs/1/suite.stdout", stripped["stdout_artifact_path"])
        self.assertEqual(4096, stripped["stdout_byte_count"])
        self.assertEqual("abc", stripped["stdout_sha256"])

    def test_bounded_details_strip_checks_and_failures_alike(self) -> None:
        details = {
            "checks": [{"name": "a", "stdout": "raw"}],
            "failures": [{"name": "b", "stderr": "raw"}],
            "reason": "verification_failed",
        }
        bounded = run_persistence._bounded_verification_details(details)
        self.assertEqual([{"name": "a"}], bounded["checks"])
        self.assertEqual([{"name": "b"}], bounded["failures"])
        self.assertEqual("verification_failed", bounded["reason"])
        # The caller's dict is not mutated in place.
        self.assertEqual("raw", details["checks"][0]["stdout"])

    def test_every_check_list_is_stripped_in_details_and_turn_summaries(
        self,
    ) -> None:
        lists = (
            "checks",
            "failures",
            "diagnostic_failures",
            "deferred_failures",
            "skipped_checks",
        )
        details = {
            key: [{"name": key, "stdout": "raw", "stderr": "raw"}] for key in lists
        }
        bounded = run_persistence._bounded_verification_details(details)
        summary = run_persistence._compact_turn_summary({"verification": details})
        for key in lists:
            with self.subTest(key=key):
                self.assertEqual([{"name": key}], bounded[key])
                self.assertEqual([{"name": key}], summary["verification"][key])

    def test_a_non_mapping_passes_through_untouched(self) -> None:
        self.assertEqual("nope", run_persistence._bounded_verification_details("nope"))
        self.assertEqual("nope", run_persistence._strip_check_output("nope"))
        self.assertEqual("nope", run_persistence._compact_snapshot("nope"))
        self.assertEqual("nope", run_persistence._compact_turn_summary("nope"))

    def test_a_turn_summary_drops_snapshots_but_keeps_their_counts(self) -> None:
        turn = {
            "session_id": "3",
            "before": {"counts": {"pending": 2}, "sessions": [{"id": "3"}]},
            "after": {"counts": {"pending": 1}, "sessions": [{"id": "3"}]},
            "verification": {"checks": [{"name": "lint", "stdout": "raw"}]},
        }
        summary = run_persistence._compact_turn_summary(turn)
        self.assertEqual({"counts": {"pending": 2}}, summary["before"])
        self.assertEqual({"counts": {"pending": 1}}, summary["after"])
        self.assertEqual([{"name": "lint"}], summary["verification"]["checks"])
        self.assertEqual("3", summary["session_id"])
        # The detailed payload keeps its sessions for `result.json`.
        self.assertIn("sessions", turn["before"])

    def test_a_projection_free_record_claims_only_v2(self) -> None:
        compact = run_persistence._compact_state_payload({"turns": []})
        self.assertEqual(run_persistence.COMPACT_STATE_SCHEMA_VERSION, 2)
        self.assertEqual(2, compact["version"])

    def test_a_record_carrying_a_projection_claims_v3(self) -> None:
        payload = {agent_mux_projection.PROJECTION_KEY: {"generation": 1}}
        compact = run_persistence._compact_state_payload(payload)
        self.assertEqual(agent_mux_projection.STATE_SCHEMA_VERSION, compact["version"])

    def test_compaction_returns_a_record_that_aliases_no_live_turn(self) -> None:
        turn = {"session_id": "3", "before": {"sessions": [{"id": "3"}]}}
        payload = {"turns": [turn], "pause_details": {"checks": [{"stdout": "raw"}]}}
        compact = run_persistence._compact_state_payload(payload)
        self.assertIsNot(payload, compact)
        self.assertIsNot(turn, compact["turns"][0])
        self.assertEqual([{}], compact["pause_details"]["checks"])
        self.assertEqual("raw", payload["pause_details"]["checks"][0]["stdout"])


class PausedWorkCommitProjectionTests(unittest.TestCase):
    """The settlement's record reaches its readers without a schema of its own.

    Session 8's observability work names ``paused_work_commit`` in three
    places on purpose — a progress line, a metric, an audit key. This class is
    the other half of that decision: everywhere else the record travels as an
    ordinary member of a free-form block, and that has to be measured rather
    than inspected. A compaction step that dropped unknown keys would leave the
    record present in memory, absent from ``state.json``, and invisible until
    an operator went looking for evidence that had never been written.
    """

    RECORD = {
        "guard": "paused-work-commit",
        "pause_reason": "verify-failed",
        "commit_mode": "per-session",
        "result": "committed",
        "verification_repair_attempt": 1,
        "verification_repair_max_attempts": 2,
        "verification_repair_exhausted": False,
        "commit_group": {"group_id": "g1", "scope": "session-paused"},
        "commit": None,
        "error": None,
        "residue": {"repositories": []},
    }

    def test_a_compact_turn_summary_keeps_the_settlement_record(self) -> None:
        turn = {
            "session_id": "3",
            "before": {"counts": {"pending": 1}, "sessions": [{"id": "3"}]},
            "paused_work_commit": self.RECORD,
        }
        summary = run_persistence._compact_turn_summary(turn)
        # Verbatim: the compaction bounds snapshots and check output by name,
        # and passes everything it was not told about through untouched.
        self.assertEqual(self.RECORD, summary["paused_work_commit"])
        self.assertEqual({"counts": {"pending": 1}}, summary["before"])

    def test_bounded_pause_details_keep_the_settlement_record(self) -> None:
        details = {
            "session": "3",
            "checks": [{"name": "suite", "stdout": "raw"}],
            "paused_work_commit": self.RECORD,
        }
        bounded = run_persistence._bounded_verification_details(details)
        self.assertEqual([{"name": "suite"}], bounded["checks"])
        self.assertEqual(self.RECORD, bounded["paused_work_commit"])

    def test_the_compact_state_record_carries_every_place_it_was_written(
        self,
    ) -> None:
        # The three mirrors `run_loop` writes, through the one seam that turns
        # a live payload into the bytes of `state.json`.
        payload = {
            "paused": True,
            "pause_reason": "verify-failed",
            "paused_work_commit": self.RECORD,
            "pause_details": {"paused_work_commit": self.RECORD},
            "turns": [{"session_id": "3", "paused_work_commit": self.RECORD}],
        }
        compact = run_persistence._compact_state_payload(payload)
        self.assertEqual(self.RECORD, compact["paused_work_commit"])
        self.assertEqual(self.RECORD, compact["pause_details"]["paused_work_commit"])
        self.assertEqual(self.RECORD, compact["turns"][0]["paused_work_commit"])

    def test_the_settlement_event_survives_event_log_compaction(self) -> None:
        record = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "name": events.PAUSED_WORK_COMMITTED_EVENT,
            "payload": {"run_id": "run-1", "session_id": "3", **self.RECORD},
            "version": 2,
        }
        compact = compact_state._compact_event_record(record)
        self.assertEqual(record["payload"], compact["payload"])

    def test_the_recovery_centers_pause_details_reader_names_no_key(self) -> None:
        # Recovery cards and the paused-Q&A projection both reach the pause
        # details through this one reader, which copies the block wholesale.
        details = recovery_card_state._pause_details(
            {"pause_details": {"paused_work_commit": self.RECORD}}
        )
        self.assertEqual(self.RECORD, details["paused_work_commit"])

    def test_the_status_projection_carries_no_pause_details_to_extend(self) -> None:
        # Measured, not assumed: `asl status`'s runner-state block is a fixed
        # key list that has never carried pause details, so the record is not
        # missing from a schema — there is no schema there for it to join.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "paused": True,
                        "reason": "verify-failed",
                        "paused_work_commit": self.RECORD,
                        "pause_details": {"paused_work_commit": self.RECORD},
                    }
                ),
                encoding="utf-8",
            )
            projection = state_payloads._runner_state_payload(
                state_path, lock_info=None
            )
        self.assertNotIn("pause_details", projection)
        self.assertNotIn("details", projection)
        self.assertNotIn("paused_work_commit", projection)
        # What it does carry about the pause is unchanged by the settlement.
        self.assertTrue(projection["paused"])
        self.assertEqual("verify-failed", projection["reason"])


class UnreadableStateProjectionTests(unittest.TestCase):
    """A ``state.json`` that cannot be decoded projects as an error, not a crash."""

    def test_state_that_is_not_utf8_reports_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            # UnicodeDecodeError is raised before any JSON is parsed, so a
            # handler for JSONDecodeError alone let it escape as a traceback.
            state_path.write_bytes(b"\xff\xfe{")

            projection = state_payloads._runner_state_payload(
                state_path, lock_info=None
            )

        self.assertTrue(projection["present"])
        self.assertIn("utf-8", projection["error"])


class RunFinishedEventPayloadTests(unittest.TestCase):
    """The terminal event references the run rather than duplicating it."""

    def test_the_payload_counts_turns_instead_of_embedding_them(self) -> None:
        payload = run_persistence._run_finished_event_payload(
            {
                "run_id": "run-1",
                "ok": True,
                "turns": [{"stdout": "raw"}, {"stdout": "raw"}],
                "final_snapshot": {"counts": {"complete": 2}, "sessions": ["big"]},
                "state_path": "/runs/1/state.json",
            }
        )
        self.assertEqual(2, payload["turn_count"])
        self.assertNotIn("turns", payload)
        self.assertNotIn("final_snapshot", payload)
        self.assertEqual({"complete": 2}, payload["counts"])
        self.assertEqual("/runs/1/state.json", payload["state_path"])

    def test_a_run_without_turns_or_counts_still_projects(self) -> None:
        payload = run_persistence._run_finished_event_payload({"run_id": "run-1"})
        self.assertEqual(0, payload["turn_count"])
        self.assertIsNone(payload["counts"])

    def test_workspace_identity_appears_only_for_a_workspace_run(self) -> None:
        legacy = run_persistence._run_finished_event_payload({"cwd": "/repo"})
        self.assertNotIn("cwd", legacy)
        self.assertNotIn("workspace_root", legacy)

        workspace = run_persistence._run_finished_event_payload(
            {"cwd": "/repo", "workspace_root": "/ws", "registry_digest": "d"}
        )
        self.assertEqual("/repo", workspace["cwd"])
        self.assertEqual("/ws", workspace["workspace_root"])
        self.assertEqual("d", workspace["registry_digest"])
