"""Shared fixtures for test_workbench_details."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans_files,
    state,
)
from asl_workbench import catalog, details
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _http_request,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)
from tests.workbench_support import WorkbenchAPIFixture

#: A pid this host can prove is not running, which is what makes the fixture
#: lock reclaimable and therefore makes "the read did not reclaim it" a claim
#: about the read.
DEAD_PID = _WORKBENCH_DEAD_PID


#: Every detail route, with the query it needs beyond ``plan_dir``. Driving the
#: observational check off one list is deliberate: a route added to the server
#: without being added here is a route nobody proved is observational, and the
#: route-coverage test below fails on exactly that.
DETAIL_ROUTES: tuple[tuple[str, dict[str, str]], ...] = (
    ("/api/archive", {}),
    ("/api/plan", {}),
    ("/api/artifacts", {}),
    ("/api/state", {}),
    ("/api/events", {}),
    ("/api/logs", {"stream": "stdout"}),
    ("/api/last-message", {}),
    ("/api/result", {}),
    ("/api/verification", {}),
    ("/api/commits", {}),
    ("/api/progress", {}),
    ("/api/final-review", {}),
    ("/api/questions", {}),
    ("/api/recovery", {}),
)


def _reclaimable_lock(plan_dir: Path, state_root: Path) -> Path:
    """A lock ``state`` would reclaim on sight, for a read to leave alone.

    Both halves matter. The pid is one this host can prove is not running, and
    the hostname is this host's -- ``state._read_lock_path`` reclaims only when
    the lock names the process table that refutes it, so a lock without a
    hostname is never reclaimed by anything and "the read left it alone" would
    be true of a read that reclaimed everything it could.
    :meth:`ObservationalDetailTests.test_the_fixture_lock_is_actually_reclaimable`
    is the standing check that this stays true.
    """
    return _write_lock_file(
        plan_dir,
        state_root,
        pid=DEAD_PID,
        hostname=socket.gethostname(),
    )


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under ``root`` by relative path, with its size and bytes.

    Content rather than mtime, for the same reason the catalog tests use
    content: a read that rewrote a file with identical bytes is still a write,
    while a touched mtime is not the failure this program cares about and is
    flaky to assert on a fast machine.
    """
    fingerprint: dict[str, tuple[int, bytes]] = {}
    if not root.is_dir():
        return fingerprint
    for path in sorted(root.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            fingerprint[str(path.relative_to(root))] = (len(payload), payload)
    return fingerprint


def _scope(
    plan_dir: Path,
    plan_state: Path,
    artifacts: dict[str, str | None] | None = None,
) -> details.DetailScope:
    """A scope with no artifact published unless the test publishes one."""
    table: dict[str, str | None] = dict.fromkeys(details.ARTIFACT_NAMES)
    table.update(artifacts or {})
    key = catalog.plan_key(str(plan_dir))
    return details.DetailScope(
        plan_key=key,
        plan_dir=str(plan_dir),
        plan_state_dir=str(plan_state),
        artifacts=table,
        run_key=catalog.run_key(key, "run-1"),
        run_id="run-1",
    )


class _DetailFixture(WorkbenchAPIFixture):
    """A seeded, paused plan with logs, artifacts, and a reclaimable lock."""

    assertEqual: Any
    assertTrue: Any

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))

    def seed_plan(self, name: str = "qna", *, paused: bool = True) -> Path:
        """One plan with the artifacts every detail route needs to answer.

        Modelled on the paused-run fixture the server tests already use: a
        completed turn that recorded a session id and a readable last message
        is what makes ``GET /api/questions`` answerable at all, and the same
        turn supplies the stdout/stderr/result artifacts the log, result, and
        artifact routes resolve.
        """
        plan_dir = self.plans_root / name
        if plan_dir.exists():
            shutil.rmtree(plan_dir)
        write_plan(plan_dir, ("pending", "pending"))
        canonical = plans_files.canonicalize_path(plan_dir)
        plan_state = state.plan_state_dir(canonical, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)

        artifact_dir = plan_state / "turn-0001"
        artifact_dir.mkdir(exist_ok=True)
        last_message = plan_state / "last-message.txt"
        last_message.write_text("Should we use 1a or 1b?\n", encoding="utf-8")
        stdout_path = plan_state / "stdout.log"
        stdout_path.write_text("".join(f"line {i}\n" for i in range(50)), "utf-8")
        stderr_path = plan_state / "stderr.log"
        stderr_path.write_text("warning: nothing\n", encoding="utf-8")
        result_path = plan_state / "result.json"
        result_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        (canonical / "final-review.md").write_text("# Review\n\nAll good.\n", "utf-8")
        (canonical / "final-review.json").write_text(
            json.dumps({"verdict": "pass"}), encoding="utf-8"
        )
        (plan_state / events.EVENTS_FILE_NAME).write_text(
            "".join(
                json.dumps(
                    {
                        "name": "turn_started",
                        "created_at": f"2026-06-19T14:0{i}:00+00:00",
                        "run_id": "run-1",
                        "payload": {"run_id": "run-1", "turn_index": i},
                    }
                )
                + "\n"
                for i in range(5)
            ),
            encoding="utf-8",
        )

        turn: dict[str, object] = {
            "run_id": "run-1",
            "turn_index": 1,
            "session_id": "1",
            "session": 1,
            "session_number": 1,
            "harness": "fake",
            "model": "test-model",
            "model_source": "default",
            "effort": "medium",
            "effort_source": "default",
            "artifact_dir": str(artifact_dir),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "last_message_path": str(last_message),
            "result_path": str(result_path),
        }
        _write_state_payload(
            canonical,
            self.state_dir,
            {
                "plan_dir": str(canonical),
                "ok": not paused,
                "paused": paused,
                "reason": "runner-pause",
                "pause_reason": "runner-pause",
                "pause_message": "agent asked a question",
                "pause_details": {"session": 1},
                "details": {"session": 1},
                "run_id": "run-1",
                "run_dir": str(plan_state / "runs" / "run-1"),
                "harness": "fake",
                "model": "test-model",
                "effort": "medium",
                "active_verification": None,
                "turns": [turn],
            },
        )
        return canonical

    def url(self, server: Any, route: str, **query: str) -> str:
        return f"{self.base_url(server)}{route}?{urllib.parse.urlencode(query)}"

    def get(self, server: Any, route: str, **query: str) -> tuple[int, dict, dict]:
        return _http_request(self.url(server, route, **query))
