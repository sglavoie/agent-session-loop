"""Focused tests for repository contention between concurrent runs."""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from agent_session_loop import plans_files, run_contention, session_overview, state
from tests.support_base import force_pid_alive as _force_pid_alive
from tests.support_base import write_json as _write_json

DEAD_PID = 4_242_424


def _make_lock(
    state_root: Path,
    plan_dir: Path,
    *,
    lock: dict[str, object],
    repositories: tuple[dict[str, object], ...] = (),
    workspace_root: str | None = None,
    run_id: str = "20260812T000000Z-1",
) -> Path:
    """Publish a lock (and matching state.json) for ``plan_dir``."""
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    state_dir = state_root / plans_files.path_sha256(canonical_plan_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "plan_dir": str(canonical_plan_dir),
        "run_id": run_id,
        "harness": "claude",
    }
    if workspace_root is not None:
        payload["workspace_root"] = workspace_root
        payload["repositories"] = list(repositories)
    _write_json(state_dir / session_overview.STATE_JSON_NAME, payload)
    _write_json(
        state_dir / session_overview.LOCK_JSON_NAME,
        {
            "plan_dir": str(canonical_plan_dir),
            "created_at": "2026-08-12T00:00:00+00:00",
            **lock,
        },
    )
    return state_dir


def _local_lock(pid: int, roots: tuple[str, ...]) -> dict[str, object]:
    return {
        "version": 2,
        "pid": pid,
        "hostname": socket.gethostname(),
        "repositories": sorted(roots),
    }


def _foreign_lock(roots: tuple[str, ...] | None) -> dict[str, object]:
    lock: dict[str, object] = {
        "pid": os.getpid(),
        "hostname": "some-other-host",
    }
    if roots is None:
        return lock
    lock["version"] = 2
    lock["repositories"] = sorted(roots)
    return lock


class DetectConflictsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state_root = root / "state"
        self.workspace = root / "workspace"
        self.plan_dir = root / "plans" / "mine"
        self.other_plan_dir = root / "plans" / "theirs"
        self.plan_dir.mkdir(parents=True)
        self.other_plan_dir.mkdir(parents=True)
        (self.workspace / "shared").mkdir(parents=True)
        (self.workspace / "mine-only").mkdir(parents=True)
        (self.workspace / "theirs-only").mkdir(parents=True)

    def _root(self, name: str) -> str:
        return str(plans_files.canonicalize_path(self.workspace / name))

    def _detect(self) -> tuple[run_contention.RepositoryConflict, ...]:
        return run_contention.detect_conflicts(
            plan_dir=self.plan_dir,
            repository_roots=(self._root("shared"), self._root("mine-only")),
            state_dir=self.state_root,
        )

    def test_overlapping_live_claim_is_reported(self) -> None:
        _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock=_local_lock(
                os.getpid(),
                (self._root("shared"), self._root("theirs-only")),
            ),
        )

        conflicts = self._detect()

        self.assertEqual(1, len(conflicts))
        conflict = conflicts[0]
        self.assertEqual(
            plans_files.canonicalize_path(self.other_plan_dir), conflict.plan_dir
        )
        self.assertEqual(os.getpid(), conflict.pid)
        self.assertEqual(socket.gethostname(), conflict.hostname)
        self.assertEqual("20260812T000000Z-1", conflict.run_id)
        self.assertEqual(run_contention.LIVENESS_RUNNING, conflict.liveness)
        self.assertEqual((self._root("shared"),), conflict.repositories)

    def test_disjoint_live_claim_is_clean(self) -> None:
        _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock=_local_lock(os.getpid(), (self._root("theirs-only"),)),
        )

        self.assertEqual((), self._detect())

    def test_a_lock_declaring_nothing_falls_back_to_the_projected_registry(
        self,
    ) -> None:
        _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock={"pid": os.getpid(), "hostname": socket.gethostname()},
            workspace_root=str(plans_files.canonicalize_path(self.workspace)),
            repositories=(
                {
                    "alias": "Shared",
                    "path": "shared",
                    "origin": "git@example.com:shared.git",
                    "resolved_root": self._root("shared"),
                },
            ),
        )

        conflicts = self._detect()

        self.assertEqual(1, len(conflicts))
        self.assertEqual((self._root("shared"),), conflicts[0].repositories)

    def test_a_workspace_relative_path_is_never_compared(self) -> None:
        other_workspace = Path(self._tmp.name) / "other-workspace"
        (other_workspace / "shared").mkdir(parents=True)
        _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock=_local_lock(
                os.getpid(),
                (str(plans_files.canonicalize_path(other_workspace / "shared")),),
            ),
        )

        self.assertEqual((), self._detect())

    def test_dead_pid_owns_nothing(self) -> None:
        _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock={
                "version": 2,
                "pid": DEAD_PID,
                "repositories": [self._root("shared")],
            },
        )

        with mock.patch.object(state, "is_pid_running", side_effect=_force_pid_alive):
            self.assertEqual((), self._detect())

    def test_foreign_host_overlap_is_reported(self) -> None:
        _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock=_foreign_lock((self._root("shared"),)),
        )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            conflicts = self._detect()

        self.assertEqual(1, len(conflicts))
        conflict = conflicts[0]
        self.assertEqual(run_contention.LIVENESS_FOREIGN_HOST, conflict.liveness)
        self.assertEqual("some-other-host", conflict.hostname)
        self.assertEqual("", conflict.run_id)
        self.assertEqual("2026-08-12T00:00:00+00:00", conflict.started_at)
        self.assertEqual((self._root("shared"),), conflict.repositories)
        self.assertEqual("", stderr.getvalue())

    def test_foreign_host_declaring_nothing_warns_and_proceeds(self) -> None:
        state_dir = _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock=_foreign_lock(None),
        )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            conflicts = self._detect()

        self.assertEqual((), conflicts)
        warning = stderr.getvalue()
        self.assertIn(str(state_dir / session_overview.LOCK_JSON_NAME), warning)
        self.assertIn("asl unlock", warning)
        self.assertIn("some-other-host", warning)

    def test_this_run_is_not_its_own_conflict(self) -> None:
        _make_lock(
            self.state_root,
            self.plan_dir,
            lock=_local_lock(os.getpid(), (self._root("shared"),)),
        )

        self.assertEqual((), self._detect())

    def test_an_empty_claim_reads_nothing(self) -> None:
        _make_lock(
            self.state_root,
            self.other_plan_dir,
            lock=_local_lock(os.getpid(), (self._root("shared"),)),
        )

        self.assertEqual(
            (),
            run_contention.detect_conflicts(
                plan_dir=self.plan_dir,
                repository_roots=(),
                state_dir=self.state_root,
            ),
        )


class ConflictRenderingTests(unittest.TestCase):
    def _conflict(self, **overrides: object) -> run_contention.RepositoryConflict:
        fields: dict[str, object] = {
            "plan_dir": Path("/plans/theirs"),
            "pid": 4321,
            "hostname": "worker-2",
            "run_id": "20260812T000000Z-1",
            "started_at": "2026-08-12T00:00:00+00:00",
            "liveness": run_contention.LIVENESS_RUNNING,
            "repositories": ("/workspace/shared",),
        }
        fields.update(overrides)
        return run_contention.RepositoryConflict(**fields)  # type: ignore[arg-type]

    def test_message_names_owner_and_shared_repositories(self) -> None:
        message = run_contention.conflict_message("/plans/mine", (self._conflict(),))

        self.assertIn("/plans/theirs", message)
        self.assertIn("pid 4321", message)
        self.assertIn("host worker-2", message)
        self.assertIn("20260812T000000Z-1", message)
        self.assertIn("2026-08-12T00:00:00+00:00", message)
        self.assertIn("/workspace/shared", message)
        self.assertNotIn("asl unlock", message)

    def test_message_points_a_foreign_lock_at_asl_unlock(self) -> None:
        message = run_contention.conflict_message(
            "/plans/mine",
            (
                self._conflict(
                    liveness=run_contention.LIVENESS_FOREIGN_HOST,
                    run_id="",
                ),
            ),
        )

        self.assertIn("asl unlock", message)
        self.assertIn("liveness unknown from this host", message)

    def test_message_is_empty_without_conflicts(self) -> None:
        self.assertEqual("", run_contention.conflict_message("/plans/mine", ()))

    def test_details_are_json_serializable(self) -> None:
        details = run_contention.conflict_details(
            (
                self._conflict(),
                self._conflict(
                    pid=None,
                    hostname=None,
                    repositories=("/workspace/other",),
                ),
            )
        )

        json.dumps(details)
        contention = details["repository_contention"]
        self.assertEqual(
            ["/workspace/other", "/workspace/shared"],
            contention["repositories"],
        )
        self.assertEqual(2, len(contention["conflicts"]))
        self.assertIsNone(contention["conflicts"][1]["pid"])


if __name__ == "__main__":
    unittest.main()
