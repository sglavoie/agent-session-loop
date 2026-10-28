"""`asl tail --json`: the run's event records as JSON lines, then followed."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import threading
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import tail
from tests.asl_tail_support import TailFollowerTestCase
from tests.support import wait_until

ASL_TEST_PORTABLE = True


def _record(name: str, run_id: str, **payload: object) -> dict[str, object]:
    return {
        "created_at": "2026-10-25T00:00:00+00:00",
        "name": name,
        "payload": {"run_id": run_id, **payload},
        "version": 1,
    }


def _append(path: Path, *records: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class TailJsonTests(TailFollowerTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        (
            self.plan_dir,
            self.state_dir,
            self.state_path,
            self.events_path,
        ) = self.setup_tail_files(Path(self._tmp.name))
        self.write_state_payload(self.state_path, {"run_id": "run-live"})

    def names(self, text: str) -> list[str]:
        return [json.loads(line)["name"] for line in text.splitlines()]

    def test_prints_only_the_latest_runs_recent_records(self) -> None:
        _append(
            self.events_path,
            _record("run_started", "run-old"),
            _record("run_started", "run-live"),
            _record("turn_started", "run-live", session_id="1"),
            _record("turn_finished", "run-live", session_id="1"),
        )
        out = io.StringIO()

        tail.tail_plan_json(
            self.plan_dir, state_dir=self.state_dir, lines=2, follow=False, out=out
        )

        self.assertEqual(["turn_started", "turn_finished"], self.names(out.getvalue()))

    def test_follows_until_the_run_finishes(self) -> None:
        _append(self.events_path, _record("run_started", "run-live"))
        out = io.StringIO()
        stop = threading.Event()
        follower = threading.Thread(
            target=tail.tail_plan_json,
            args=(self.plan_dir,),
            kwargs={
                "state_dir": self.state_dir,
                "out": out,
                "poll_interval": 0.01,
                "stop_event": stop,
            },
        )
        follower.start()
        self.addCleanup(stop.set)
        # Only once the initial read is out, so what follows has to be followed.
        wait_until(lambda: "run_started" in out.getvalue())
        _append(
            self.events_path,
            _record("turn_started", "run-live", session_id="1"),
            _record("run_finished", "run-live"),
        )

        follower.join(10)

        self.assertFalse(follower.is_alive())
        self.assertEqual(
            ["run_started", "turn_started", "run_finished"], self.names(out.getvalue())
        )

    def test_finished_state_ends_follow_without_a_final_event(self) -> None:
        _append(self.events_path, _record("run_started", "run-live"))
        self.write_state_payload(
            self.state_path,
            {"run_id": "run-live", "run_finished_at": "2026-10-25T00:01:00Z"},
        )
        out = io.StringIO()
        stop = threading.Event()
        follower = threading.Thread(
            target=tail.tail_plan_json,
            args=(self.plan_dir,),
            kwargs={
                "state_dir": self.state_dir,
                "out": out,
                "poll_interval": 0.01,
                "stop_event": stop,
            },
        )
        follower.start()
        self.addCleanup(stop.set)

        follower.join(0.5)

        self.assertFalse(follower.is_alive())
        self.assertEqual(["run_started"], self.names(out.getvalue()))

    def test_next_runs_finished_state_does_not_end_current_follow(self) -> None:
        _append(self.events_path, _record("run_started", "run-live"))
        out = io.StringIO()
        stop = threading.Event()
        follower = threading.Thread(
            target=tail.tail_plan_json,
            args=(self.plan_dir,),
            kwargs={
                "state_dir": self.state_dir,
                "out": out,
                "poll_interval": 0.01,
                "stop_event": stop,
            },
        )
        follower.start()
        self.addCleanup(stop.set)
        wait_until(lambda: "run_started" in out.getvalue())

        self.write_state_payload(
            self.state_path,
            {"run_id": "run-next", "run_finished_at": "2026-10-25T00:01:00Z"},
        )
        stop.wait(0.05)
        self.assertTrue(follower.is_alive())

        _append(self.events_path, _record("run_finished", "run-live"))
        follower.join(10)
        self.assertFalse(follower.is_alive())
        self.assertEqual(["run_started", "run_finished"], self.names(out.getvalue()))

    def test_a_missing_plan_is_a_json_error(self) -> None:
        out = io.StringIO()
        args = argparse.Namespace(
            plan_dir=str(Path(self._tmp.name) / "missing"),
            state_dir=str(self.state_dir),
            lines=5,
            no_follow=True,
            json=True,
        )
        with redirect_stdout(out):
            code = tail.tail_from_args(args)

        self.assertEqual(1, code)
        self.assertEqual("plan-not-found", json.loads(out.getvalue())["reason"])
