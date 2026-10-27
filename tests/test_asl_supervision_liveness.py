"""Supervision liveness record tests: write/read/prune and every probe verdict."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_session_loop import supervision_liveness

_BIRTH = "Sun Jul 26 17:57:01 2026"
_OTHER_BIRTH = "Mon Jul 27 09:12:43 2026"


def _ps_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        list(supervision_liveness.process_tree.PS_BIRTH_COMMAND),
        returncode,
        stdout=stdout,
        stderr="",
    )


def _record(**overrides) -> supervision_liveness.LivenessRecord:
    fields = {
        "supervision_id": str(uuid.uuid4()),
        "pid": 4242,
        "birth": _BIRTH,
        "host": socket.gethostname(),
        "argv": ("asl_autopilot.py", "--command", "asl run /tmp/plan"),
        "command": {"ok": True, "plan_dir": "/tmp/plan"},
        "plan_dir": "/tmp/plan",
        "started_at": "2026-08-15T12:00:00.000000+00:00",
    }
    fields.update(overrides)
    return supervision_liveness.LivenessRecord(**fields)


class RecordStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = os.path.join(self._tmp.name, "plan")
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.plan_dir)
        self.supervision_id = str(uuid.uuid4())

    def _write(self, **kwargs) -> Path:
        kwargs.setdefault("run_command", lambda *a, **kw: _ps_result(f"{_BIRTH}\n"))
        return supervision_liveness.write_record(
            self.plan_dir,
            self.supervision_id,
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_write_then_read_round_trips_every_field(self) -> None:
        path = self._write(
            command={"ok": True, "plan_dir": self.plan_dir},
            argv=["asl_autopilot.py", "--command", "asl run plan"],
            pid=4242,
            host="build-box",
        )

        record = supervision_liveness.read_record(
            self.plan_dir, self.supervision_id, state_dir=self.state_dir
        )
        self.assertEqual(self.supervision_id, record.supervision_id)
        self.assertEqual(4242, record.pid)
        self.assertEqual(_BIRTH, record.birth)
        self.assertEqual("build-box", record.host)
        self.assertEqual(("asl_autopilot.py", "--command", "asl run plan"), record.argv)
        self.assertEqual({"ok": True, "plan_dir": self.plan_dir}, record.command)
        self.assertEqual(self.plan_dir, record.plan_dir)
        self.assertTrue(record.started_at)
        self.assertEqual(path.name, f"supervision-liveness-{self.supervision_id}.json")

    def test_record_lands_in_the_plan_state_dir_under_the_shared_glob(self) -> None:
        path = self._write()

        self.assertEqual(
            supervision_liveness.record_path_for_plan(
                self.plan_dir, self.supervision_id, self.state_dir
            ),
            path,
        )
        self.assertEqual(
            [path],
            sorted(path.parent.glob(supervision_liveness.SUPERVISION_LIVENESS_GLOB)),
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            supervision_liveness.SUPERVISION_LIVENESS_VERSION, payload["version"]
        )

    def test_write_leaves_no_temp_file_behind(self) -> None:
        path = self._write()

        self.assertEqual([path.name], [entry.name for entry in path.parent.iterdir()])

    def test_defaults_come_from_this_process(self) -> None:
        self._write()

        record = supervision_liveness.read_record(
            self.plan_dir, self.supervision_id, state_dir=self.state_dir
        )
        self.assertEqual(os.getpid(), record.pid)
        self.assertEqual(socket.gethostname(), record.host)
        self.assertEqual(tuple(supervision_liveness.sys.argv), record.argv)

    def test_unreportable_birth_still_writes_a_record(self) -> None:
        self._write(run_command=lambda *a, **kw: _ps_result("", returncode=1))

        record = supervision_liveness.read_record(
            self.plan_dir, self.supervision_id, state_dir=self.state_dir
        )
        self.assertIsNone(record.birth)

    def test_missing_record_reads_as_none(self) -> None:
        self.assertIsNone(
            supervision_liveness.read_record(
                self.plan_dir, str(uuid.uuid4()), state_dir=self.state_dir
            )
        )

    def test_unusable_payloads_read_as_none(self) -> None:
        path = self._write()
        good = json.loads(path.read_text(encoding="utf-8"))
        cases = {
            "not-json": "{",
            # Deep enough that the decoder raises RecursionError instead.
            "deeply-nested": "[" * 1_000_000,
            "not-an-object": json.dumps([good]),
            "wrong-version": json.dumps({**good, "version": 99}),
            "other-launch": json.dumps({**good, "supervision_id": str(uuid.uuid4())}),
            "pid-not-int": json.dumps({**good, "pid": "4242"}),
            "pid-bool": json.dumps({**good, "pid": True}),
            "birth-not-str": json.dumps({**good, "birth": 17}),
            "command-not-object": json.dumps({**good, "command": "asl run"}),
            "argv-not-strings": json.dumps({**good, "argv": [1, 2]}),
            "host-missing": json.dumps({k: v for k, v in good.items() if k != "host"}),
        }
        for name, raw in cases.items():
            with self.subTest(payload=name):
                path.write_text(raw, encoding="utf-8")
                self.assertIsNone(
                    supervision_liveness.read_record(
                        self.plan_dir, self.supervision_id, state_dir=self.state_dir
                    )
                )

    def test_non_uuid_launch_identity_is_rejected(self) -> None:
        for value in ("", "../escape", "not-a-uuid"):
            with self.subTest(supervision_id=value), self.assertRaises(ValueError):
                supervision_liveness.record_path_for_plan(
                    self.plan_dir, value, self.state_dir
                )


class PruneRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = os.path.join(self._tmp.name, "plan")
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.plan_dir)

    def _write(self, *, age_seconds: float = 0.0) -> Path:
        path = supervision_liveness.write_record(
            self.plan_dir,
            str(uuid.uuid4()),
            state_dir=self.state_dir,
            run_command=lambda *a, **kw: _ps_result(f"{_BIRTH}\n"),
        )
        if age_seconds:
            stamp = path.stat().st_mtime - age_seconds
            os.utime(path, (stamp, stamp))
        return path

    def test_only_records_older_than_the_cutoff_are_removed(self) -> None:
        stale = self._write(age_seconds=40 * 24 * 60 * 60)
        fresh = self._write()

        self.assertEqual(
            1,
            supervision_liveness.prune_records(self.plan_dir, state_dir=self.state_dir),
        )
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_cutoff_spans_a_quota_sleep(self) -> None:
        sleeping = self._write(age_seconds=8 * 24 * 60 * 60)

        self.assertEqual(
            0,
            supervision_liveness.prune_records(self.plan_dir, state_dir=self.state_dir),
        )
        self.assertTrue(sleeping.exists())

    def test_neighbouring_state_files_are_never_touched(self) -> None:
        record = self._write(age_seconds=40 * 24 * 60 * 60)
        neighbour = record.parent / "mailbox.jsonl"
        neighbour.write_text("{}\n", encoding="utf-8")
        stamp = record.stat().st_mtime
        os.utime(neighbour, (stamp, stamp))

        supervision_liveness.prune_records(self.plan_dir, state_dir=self.state_dir)
        self.assertTrue(neighbour.exists())

    def test_missing_state_dir_prunes_nothing(self) -> None:
        self.assertEqual(
            0,
            supervision_liveness.prune_records(
                self.plan_dir, state_dir=os.path.join(self._tmp.name, "absent")
            ),
        )


class ProbeTests(unittest.TestCase):
    def test_matching_birth_is_alive(self) -> None:
        run_command = mock.Mock(return_value=_ps_result(f"  {_BIRTH}  \n"))

        self.assertEqual(
            supervision_liveness.ALIVE,
            supervision_liveness.probe(_record(), run_command=run_command, timeout=2.5),
        )
        args, kwargs = run_command.call_args
        self.assertEqual(["ps", "-o", "lstart=", "-p", "4242"], args[0])
        self.assertEqual(2.5, kwargs["timeout"])
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["text"])

    def test_reused_pid_is_dead(self) -> None:
        self.assertEqual(
            supervision_liveness.DEAD,
            supervision_liveness.probe(
                _record(), run_command=lambda *a, **kw: _ps_result(f"{_OTHER_BIRTH}\n")
            ),
        )

    def test_no_such_process_is_dead(self) -> None:
        self.assertEqual(
            supervision_liveness.DEAD,
            supervision_liveness.probe(
                _record(), run_command=lambda *a, **kw: _ps_result("", returncode=1)
            ),
        )

    def test_another_host_is_unverified(self) -> None:
        def unreachable(*args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("a foreign host must not be probed locally")

        self.assertEqual(
            supervision_liveness.UNVERIFIED,
            supervision_liveness.probe(
                _record(host="somewhere-else"), run_command=unreachable
            ),
        )

    def test_ps_command_failures_do_not_prove_the_helper_died(self) -> None:
        cases = (
            (1, "", "ps: permission denied\n"),
            (2, "", "ps: invalid option\n"),
            (2, "", ""),
            (-9, "", ""),
            (1, f"{_BIRTH}\n", ""),
        )
        for returncode, stdout, stderr in cases:
            with self.subTest(returncode=returncode, stdout=stdout, stderr=stderr):
                result = subprocess.CompletedProcess(
                    ["ps"], returncode, stdout=stdout, stderr=stderr
                )
                self.assertEqual(
                    supervision_liveness.UNVERIFIED,
                    supervision_liveness.probe(
                        _record(), run_command=mock.Mock(return_value=result)
                    ),
                )

    def test_non_positive_pid_is_unverified(self) -> None:
        def unreachable(*args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("an impossible pid must not be probed")

        for pid in (0, -1):
            with self.subTest(pid=pid):
                self.assertEqual(
                    supervision_liveness.UNVERIFIED,
                    supervision_liveness.probe(
                        _record(pid=pid), run_command=unreachable
                    ),
                )

    def test_failed_ps_is_unverified(self) -> None:
        failures = {
            "spawn-error": OSError("ps is missing"),
            "timeout": subprocess.TimeoutExpired(cmd="ps", timeout=5.0),
        }
        for name, error in failures.items():
            with self.subTest(failure=name):

                def raise_error(*args, _error=error, **kwargs):
                    raise _error

                self.assertEqual(
                    supervision_liveness.UNVERIFIED,
                    supervision_liveness.probe(_record(), run_command=raise_error),
                )

    def test_unparsable_ps_output_is_unverified(self) -> None:
        for stdout in ("", "\n", "no start time here\n"):
            with self.subTest(stdout=stdout):
                self.assertEqual(
                    supervision_liveness.UNVERIFIED,
                    supervision_liveness.probe(
                        _record(),
                        run_command=lambda *a, _out=stdout, **kw: _ps_result(_out),
                    ),
                )

    def test_record_without_a_birth_cannot_confirm_a_living_pid(self) -> None:
        self.assertEqual(
            supervision_liveness.UNVERIFIED,
            supervision_liveness.probe(
                _record(birth=None),
                run_command=lambda *a, **kw: _ps_result(f"{_BIRTH}\n"),
            ),
        )

    def test_record_without_a_birth_still_reports_a_vacant_pid_dead(self) -> None:
        self.assertEqual(
            supervision_liveness.DEAD,
            supervision_liveness.probe(
                _record(birth=None),
                run_command=lambda *a, **kw: _ps_result("", returncode=1),
            ),
        )

    def test_probe_does_not_delegate_to_process_birth(self) -> None:
        # ``process_birth`` returns None for both "no such process" and "ps
        # failed"; a probe built on it could not report either verdict.
        with mock.patch.object(
            supervision_liveness.process_tree,
            "process_birth",
            side_effect=AssertionError("probe must not reuse process_birth"),
        ):
            self.assertEqual(
                supervision_liveness.ALIVE,
                supervision_liveness.probe(
                    _record(), run_command=lambda *a, **kw: _ps_result(f"{_BIRTH}\n")
                ),
            )


if __name__ == "__main__":
    unittest.main()
