"""Process birth identity: a pinned ``ps`` environment and host-rename tolerance."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    overseer_discovery,
    process_sampling,
    state_locks,
    supervision_liveness,
)

_UTC_BIRTH = "Sun Oct 25 14:30:05 2026"
# The same instant as a writer in UTC-4 recorded it before births were pinned.
_LEGACY_LOCAL_BIRTH = "Sun Oct 25 10:30:05 2026"


def _ps_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        list(process_sampling.PS_BIRTH_COMMAND), returncode, stdout=stdout, stderr=""
    )


class PsEnvironmentTests(unittest.TestCase):
    def test_every_ps_sampler_runs_with_utc_and_the_c_locale(self) -> None:
        seen: list[dict[str, str]] = []

        def run_command(*args, **kwargs):
            seen.append(kwargs["env"])
            return _ps_result("")

        with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo", "LC_ALL": "fr_FR"}):
            process_sampling.process_birth(os.getpid(), run_command=run_command)
            process_sampling.sample_process_tree(1, run_command=run_command)
            process_sampling.sample_process_table(run_command=run_command)
            supervision_liveness.probe(
                supervision_liveness.LivenessRecord(
                    supervision_id="00000000-0000-4000-8000-000000000000",
                    pid=os.getpid(),
                    birth=_UTC_BIRTH,
                    host=socket.gethostname(),
                    argv=(),
                    command={},
                    plan_dir="/tmp/plan",
                    started_at="2026-10-25T00:00:00+00:00",
                ),
                run_command=run_command,
            )

        self.assertEqual(4, len(seen))
        for env in seen:
            self.assertEqual("UTC0", env["TZ"])
            self.assertEqual("C", env["LC_ALL"])
            self.assertEqual(os.environ.get("PATH"), env.get("PATH"))

    @unittest.skipUnless(shutil.which("ps"), "needs a real ps")
    def test_parent_time_zone_does_not_change_a_real_birth(self) -> None:
        births = set()
        for zone in ("UTC0", "America/New_York", "Asia/Kolkata", "Pacific/Chatham"):
            with mock.patch.dict(os.environ, {"TZ": zone}):
                births.add(process_sampling.process_birth(os.getpid()))
        self.assertEqual(1, len(births))
        self.assertIsNotNone(births.pop())


class BirthMayMatchTests(unittest.TestCase):
    def test_equal_births_match(self) -> None:
        self.assertTrue(process_sampling.birth_may_match(_UTC_BIRTH, _UTC_BIRTH))

    def test_a_time_zone_shift_of_the_same_instant_cannot_prove_reuse(self) -> None:
        for recorded in (
            _LEGACY_LOCAL_BIRTH,
            "Sun Oct 25 20:00:05 2026",  # UTC+5:30
            "Mon Oct 26 04:15:05 2026",  # UTC+13:45
        ):
            with self.subTest(recorded=recorded):
                self.assertTrue(process_sampling.birth_may_match(recorded, _UTC_BIRTH))

    def test_an_unparsable_legacy_birth_cannot_prove_reuse(self) -> None:
        # A localized month name from a writer that ran ps under its own locale.
        self.assertTrue(
            process_sampling.birth_may_match("dim oct 25 10:30:05 2026", _UTC_BIRTH)
        )

    def test_a_birth_no_offset_explains_is_a_mismatch(self) -> None:
        for recorded in (
            "Sun Oct 25 14:30:06 2026",
            "Sun Oct 25 14:37:05 2026",
            "Thu Jan  1 00:00:00 1970",
            "Fri Oct 23 14:30:05 2026",
        ):
            with self.subTest(recorded=recorded):
                self.assertFalse(process_sampling.birth_may_match(recorded, _UTC_BIRTH))


class LockIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.plan_dir = root / "plan"
        self.plan_dir.mkdir()
        (self.plan_dir / "context.md").write_text("# plan\n", encoding="utf-8")
        self.state_dir = root / "state"
        self.lock_path = state_locks.state.lock_path_for_plan(
            state_locks.plans_files.canonicalize_path(self.plan_dir), self.state_dir
        )
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def write_lock(
        self, *, hostname: str, process_start: str | None, pid: int | None = None
    ) -> None:
        payload = {
            "pid": os.getpid() if pid is None else pid,
            "hostname": hostname,
            "plan_dir": str(state_locks.plans_files.canonicalize_path(self.plan_dir)),
            "created_at": "2026-10-25T00:00:00+00:00",
        }
        if process_start is not None:
            payload["process_start"] = process_start
        self.lock_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_a_legacy_local_time_birth_keeps_a_live_lock(self) -> None:
        self.write_lock(
            hostname=socket.gethostname(), process_start=_LEGACY_LOCAL_BIRTH
        )
        with mock.patch.object(
            state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
        ):
            info = state_locks.read_lock_info(self.plan_dir, self.state_dir)

        assert info is not None
        self.assertTrue(info.active)
        self.assertTrue(self.lock_path.exists())

    def test_this_process_still_owns_its_lock_after_a_host_rename(self) -> None:
        self.write_lock(hostname="before-dhcp.local", process_start=_UTC_BIRTH)
        with mock.patch.object(
            state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
        ):
            info = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)
            self.assertTrue(
                state_locks._lock_names_this_process(
                    json.loads(self.lock_path.read_text())
                )
            )
            state_locks.release_lock(self.plan_dir, self.state_dir)

        assert info is not None
        self.assertTrue(info.active)
        self.assertFalse(self.lock_path.exists())

    def test_another_live_local_run_survives_a_host_rename(self) -> None:
        # A status reader is not the run itself; before the fix it reported
        # the live run stale ("recorded on host ...") after a DHCP rename.
        self.write_lock(
            hostname="before-dhcp.local", process_start=_UTC_BIRTH, pid=os.getppid()
        )
        with mock.patch.object(
            state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
        ):
            info = state_locks.read_lock_info(self.plan_dir, self.state_dir)

        assert info is not None
        self.assertTrue(info.active)
        self.assertTrue(self.lock_path.exists())

    def test_a_renamed_host_lock_needs_an_exact_birth_to_count_as_local(
        self,
    ) -> None:
        # A legacy-offset birth or none at all cannot confirm the owner, so
        # the lock stays foreign rather than weakening cross-host safety.
        for process_start in (_LEGACY_LOCAL_BIRTH, None):
            with self.subTest(process_start=process_start):
                self.write_lock(
                    hostname="other-host",
                    process_start=process_start,
                    pid=os.getppid(),
                )
                with mock.patch.object(
                    state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
                ):
                    info = state_locks.read_lock_info(self.plan_dir, self.state_dir)

                assert info is not None
                self.assertEqual("lock was recorded on host other-host", info.reason)
                self.assertTrue(self.lock_path.exists())

    def test_another_hosts_lock_with_a_different_birth_is_not_ours(self) -> None:
        self.write_lock(hostname="other-host", process_start=_LEGACY_LOCAL_BIRTH)
        with mock.patch.object(
            state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
        ):
            info = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                state_locks.release_lock(self.plan_dir, self.state_dir)

        assert info is not None
        self.assertEqual("lock was recorded on host other-host", info.reason)
        self.assertIn("left the lock", stderr.getvalue())
        self.assertTrue(self.lock_path.exists())

    def test_every_reader_agrees_a_renamed_live_owner_is_local(self) -> None:
        # Before the fix the lock read active yet foreign_host, so redirect
        # admission, control signals and the overseer still refused the run.
        self.write_lock(
            hostname="before-dhcp.local", process_start=_UTC_BIRTH, pid=os.getppid()
        )
        with mock.patch.object(
            state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
        ):
            info = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)
            assert info is not None
            self.assertTrue(info.active)
            self.assertFalse(info.foreign_host)
            ownership = overseer_discovery._read_ownership(
                self.lock_path.parent, warnings=[]
            )
            self.assertFalse(ownership.foreign_host)
            self.assertTrue(ownership.alive)

    def test_a_renamed_host_lock_without_an_exact_birth_stays_foreign(self) -> None:
        for process_start in (_LEGACY_LOCAL_BIRTH, None):
            with self.subTest(process_start=process_start):
                self.write_lock(
                    hostname="other-host",
                    process_start=process_start,
                    pid=os.getppid(),
                )
                with mock.patch.object(
                    state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
                ):
                    info = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)
                    assert info is not None
                    self.assertTrue(info.foreign_host)
                    ownership = overseer_discovery._read_ownership(
                        self.lock_path.parent, warnings=[]
                    )
                    self.assertTrue(ownership.foreign_host)


class LivenessProbeHostRenameTests(unittest.TestCase):
    def record(self, *, host: str) -> supervision_liveness.LivenessRecord:
        return supervision_liveness.LivenessRecord(
            supervision_id="00000000-0000-4000-8000-000000000000",
            pid=os.getppid(),
            birth=_UTC_BIRTH,
            host=host,
            argv=(),
            command={},
            plan_dir="/tmp/plan",
            started_at="2026-10-25T00:00:00+00:00",
        )

    def test_a_renamed_host_record_of_a_live_process_is_probed(self) -> None:
        with mock.patch.object(
            state_locks.process_tree, "process_birth", return_value=_UTC_BIRTH
        ):
            verdict = supervision_liveness.probe(
                self.record(host="before-dhcp.local"),
                run_command=lambda *args, **kwargs: _ps_result(_UTC_BIRTH + "\n"),
            )
        self.assertEqual(supervision_liveness.ALIVE, verdict)

    def test_another_hosts_record_stays_unverified(self) -> None:
        with mock.patch.object(
            state_locks.process_tree,
            "process_birth",
            return_value="Mon Oct 26 09:00:01 2026",
        ):
            verdict = supervision_liveness.probe(
                self.record(host="other-host"),
                run_command=lambda *args, **kwargs: _ps_result(_UTC_BIRTH + "\n"),
            )
        self.assertEqual(supervision_liveness.UNVERIFIED, verdict)


if __name__ == "__main__":
    unittest.main()
