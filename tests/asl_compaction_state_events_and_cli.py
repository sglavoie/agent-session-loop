"""Events and cli cases for StateCompactionTests."""

from __future__ import annotations

import argparse
import contextlib
import io
import json

from agent_session_loop import (
    compact_state,
    events,
    plans_snapshot,
    run_persistence,
    state,
    state_locks,
    state_status,
)
from tests.support import (
    write_plan,
)

from .asl_compaction_state_support import StateCompactionTestsBase


class StateCompactionTests(StateCompactionTestsBase):
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

    def test_torn_event_lines_are_kept_and_the_rest_still_compacts(self) -> None:
        # A crash mid-append leaves a torn final record (here also a torn
        # UTF-8 sequence mid-file). Compaction used to fail the whole plan
        # with a bare JSON error naming no file, leaving state.json and
        # every result.json uncompacted.
        state_path = self._write_state({"run_id": "r", "turns": [self._v1_turn()]})
        result_path = self._write_result(
            [{"name": "c1", "status": "fail", "stdout": self.big, "stderr": ""}]
        )
        good = json.dumps(
            {"created_at": "t1", "name": "run_started", "payload": {"run_id": "r"}},
            sort_keys=True,
        ).encode("utf-8")
        not_utf8 = b'{"name": "z\xe2\x82'
        torn = b'{"name": "y", "creat'
        events_path = self.plan_state / events.EVENTS_FILE_NAME
        events_path.write_bytes(good + b"\n" + not_utf8 + b"\n" + good + b"\n" + torn)

        projected = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )
        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        self.assertEqual(2, projected.unparseable_event_lines)
        self.assertEqual(2, report.to_dict()["unparseable_event_lines"])
        lines = events_path.read_bytes().split(b"\n")
        # Fragments stay byte-for-byte, the torn tail still unterminated.
        self.assertEqual(not_utf8, lines[1])
        self.assertEqual(torn, lines[-1])
        for line in (lines[0], lines[2]):
            record = json.loads(line)
            self.assertEqual("run_started", record["name"])
            self.assertEqual(events.EVENT_SCHEMA_VERSION, record["version"])
        self.assertIn(str(state_path), report.rewritten_paths)
        self.assertIn(str(result_path), report.rewritten_paths)
        self.assertEqual(
            run_persistence.COMPACT_STATE_SCHEMA_VERSION,
            json.loads(state_path.read_text(encoding="utf-8"))["version"],
        )
        self.assertEqual(
            projected.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )

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

        compact = run_persistence._compact_state_payload(payload)
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

        payload = state_status.status_payload(
            plans_snapshot.inspect_plan(self.plan_dir), self.state_dir
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

    def test_apply_refuses_while_a_live_run_holds_the_lock(self) -> None:
        state_path = self._write_state(
            {"run_id": "r", "harness": "codex", "turns": [self._v1_turn()]}
        )
        before = state_path.read_bytes()
        state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        with self.assertRaisesRegex(state.StateError, "plan is locked by pid"):
            compact_state.compact_plan_state(
                self.plan_dir, state_dir=self.state_dir, apply=True
            )
        self.assertEqual(before, state_path.read_bytes())

        # A dry run writes nothing, so a live plan can still be measured.
        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )
        self.assertEqual(1, report.files_rewritten)
        self.assertEqual(before, state_path.read_bytes())

    def test_cli_reports_an_over_nested_result_instead_of_crashing(self) -> None:
        # The decoder raises RecursionError, not a ValueError, for this; it
        # must fail the command with a report like any other malformed file.
        result_path = self._write_result([])
        result_path.write_text("[" * 1_000_000, encoding="utf-8")
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            state_dir=str(self.state_dir),
            apply=False,
            json=True,
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = compact_state.compact_state_from_args(args)

        self.assertEqual(1, code)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual("state-error", payload["reason"])
        self.assertIn(str(result_path), payload["error"])

    def test_over_nested_event_lines_are_kept_verbatim(self) -> None:
        deep = b"[" * 1_000_000
        events_path = self.plan_state / events.EVENTS_FILE_NAME
        events_path.write_bytes(deep + b"\n")

        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        self.assertEqual(1, report.unparseable_event_lines)
        self.assertEqual(deep + b"\n", events_path.read_bytes())

    def test_cli_refuses_a_directory_that_is_not_a_plan(self) -> None:
        not_a_plan = self.tmp_path / "not-a-plan"
        not_a_plan.mkdir()
        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                args = argparse.Namespace(
                    plan_dir=str(not_a_plan),
                    state_dir=str(self.state_dir),
                    apply=False,
                    json=as_json,
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = compact_state.compact_state_from_args(args)
                self.assertEqual(1, code)
                text = stdout.getvalue() if as_json else stderr.getvalue()
                self.assertIn("missing context.md", text)
                if as_json:
                    self.assertFalse(json.loads(text)["ok"])
