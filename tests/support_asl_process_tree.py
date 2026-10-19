"""Shared fixtures for test_asl_process_tree."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from agent_session_loop import process_tree


def _ps_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        list(process_tree.PS_COMMAND), returncode, stdout=stdout, stderr=""
    )


_BIRTH = "Sun Jul 26 17:57:01 2026"


_LATER_BIRTH = "Mon Jul 27 09:12:43 2026"


_SELF_PID = 7777


_SELF_PGID = 8888


class _UrgentBase(unittest.TestCase):
    """Shared identity for the urgent-termination cases."""

    def setUp(self) -> None:
        for name, value in (("getpid", _SELF_PID), ("getpgrp", _SELF_PGID)):
            patcher = mock.patch.object(process_tree.os, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _birth_result(birth: str | None) -> subprocess.CompletedProcess:
        """A ``ps -o lstart= -p`` answer, or the not-found answer."""
        if birth is None:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")
        return subprocess.CompletedProcess([], 0, stdout=f"{birth}\n", stderr="")
