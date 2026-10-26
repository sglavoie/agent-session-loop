"""Control queue appends: torn-tail repair, short writes and failed appends."""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import control_queue

ASL_TEST_PORTABLE = True


class AppendCommandDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = root / "state"
        self.queue_path = control_queue.queue_path_for_plan(
            self.plan_dir, self.state_dir
        )

    def append(self, command_id: str) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id=command_id,
        )

    def drain_ids(self) -> tuple[list[str], int]:
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        return [command.id for command in result.accepted], len(result.rejected)

    def test_a_torn_tail_is_terminated_before_the_next_command(self) -> None:
        self.append("first")
        with self.queue_path.open("ab") as handle:
            handle.write(b'{"id": "torn", "comm')

        self.append("second")

        ids, rejected = self.drain_ids()
        self.assertEqual(["first", "second"], ids)
        self.assertEqual(1, rejected)

    def test_a_short_write_keeps_writing_until_the_line_is_whole(self) -> None:
        real_write = os.write

        def one_byte_at_a_time(fd: int, data: bytes) -> int:
            return real_write(fd, bytes(data[:1]))

        with mock.patch.object(control_queue.os, "write", one_byte_at_a_time):
            self.append("slow")

        self.assertEqual((["slow"], 0), self.drain_ids())

    def test_a_failed_append_leaves_no_fragment_behind(self) -> None:
        self.append("first")
        before = self.queue_path.read_bytes()
        real_write = os.write
        calls = 0

        def fail_midway(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, bytes(data[:5]))
            raise OSError(errno.ENOSPC, "No space left on device")

        with (
            mock.patch.object(control_queue.os, "write", fail_midway),
            self.assertRaises(OSError),
        ):
            self.append("lost")

        self.assertEqual(before, self.queue_path.read_bytes())
        self.append("after")
        self.assertEqual((["first", "after"], 0), self.drain_ids())


if __name__ == "__main__":
    unittest.main()
