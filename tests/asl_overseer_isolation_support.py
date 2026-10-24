from __future__ import annotations

import argparse
import inspect
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    overseer_isolation,
    plans_files,
    run_contention,
    run_entry,
    session_overview,
    session_overview_render,
    state,
    state_locks,
)
from tests.support import assert_no_runner_import
from tests.support_base import force_pid_alive as _force_pid_alive
from tests.support_base import write_json as _write_json

DEAD_PID = 4_242_424


class _CorpusSnapshot:
    """Every file under a root, by content and mtime.

    An observation that rewrote a lock with identical bytes would still be a
    write, so the mtime is compared alongside the content: the promise this
    boundary makes is that the file was not touched, not merely that it still
    says the same thing.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.entries = self._read()

    def _read(self) -> dict[str, tuple[bytes, int]]:
        found: dict[str, tuple[bytes, int]] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            found[str(path.relative_to(self.root))] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
        return found

    def assert_unchanged(self, case: unittest.TestCase) -> None:
        case.assertEqual(self.entries, self._read())


class _Corpus:
    """A state root holding several plans' locks and run records."""

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state_root = self.root / "state"
        self.state_root.mkdir(parents=True)
        self.workspace = self.root / "workspace"
        for name in ("shared", "mine-only", "theirs-only"):
            (self.workspace / name).mkdir(parents=True)
        self.plan_dir = self._plan("mine")

    def _plan(self, name: str) -> Path:
        plan_dir = self.root / "plans" / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plan_dir

    def root_of(self, name: str) -> str:
        return str(plans_files.canonicalize_path(self.workspace / name))

    def state_dir_for(self, plan_dir: Path) -> Path:
        return self.state_root / plans_files.path_sha256(
            plans_files.canonicalize_path(plan_dir)
        )

    def publish(
        self,
        name: str,
        *,
        lock: dict[str, object] | None,
        run_id: str = "20260812T000000Z-1",
        state_payload: dict[str, object] | None = None,
        lock_text: str | None = None,
    ) -> Path:
        """Publish one plan's ``state.json`` and (optionally) its ``lock.json``."""
        plan_dir = self._plan(name)
        canonical = plans_files.canonicalize_path(plan_dir)
        state_dir = self.state_dir_for(plan_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "plan_dir": str(canonical),
            "run_id": run_id,
            "harness": "claude",
        }
        payload.update(state_payload or {})
        _write_json(state_dir / session_overview.STATE_JSON_NAME, payload)
        if lock_text is not None:
            (state_dir / session_overview.LOCK_JSON_NAME).write_text(
                lock_text, encoding="utf-8"
            )
        elif lock is not None:
            _write_json(
                state_dir / session_overview.LOCK_JSON_NAME,
                {
                    "plan_dir": str(canonical),
                    "created_at": "2026-08-12T00:00:00+00:00",
                    **lock,
                },
            )
        return plan_dir

    def local_lock(self, pid: int, roots: tuple[str, ...]) -> dict[str, object]:
        return {
            "version": 2,
            "pid": pid,
            "hostname": socket.gethostname(),
            "repositories": sorted(roots),
        }

    def foreign_lock(self, roots: tuple[str, ...] | None) -> dict[str, object]:
        lock: dict[str, object] = {"pid": os.getpid(), "hostname": "some-other-host"}
        if roots is not None:
            lock["version"] = 2
            lock["repositories"] = sorted(roots)
        return lock

    def observe(self, **overrides: Any) -> overseer_isolation.Observation:
        kwargs: dict[str, Any] = {
            "plan_dir": self.plan_dir,
            "repository_roots": (self.root_of("shared"), self.root_of("mine-only")),
            "state_dir": self.state_root,
        }
        kwargs.update(overrides)
        return overseer_isolation.observe_contention(**kwargs)

    def detect(self, **overrides: Any) -> tuple[run_contention.RepositoryConflict, ...]:
        kwargs: dict[str, Any] = {
            "plan_dir": self.plan_dir,
            "repository_roots": (self.root_of("shared"), self.root_of("mine-only")),
            "state_dir": self.state_root,
        }
        kwargs.update(overrides)
        return run_contention.detect_conflicts(**kwargs)
