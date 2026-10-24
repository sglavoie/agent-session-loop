"""Tests for test asl overseer resume isolation."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    events,
    intervention,
    overseer_resume,
    run_entry,
    state,
    workbench_launch,
    workbench_registry_models,
)
from tests.support import (
    _FakePopen,
    write_multi_repo_plan,
)
from tests.support_asl_overseer_resume import MOMENT

ASL_TEST_PORTABLE = True


class LaunchIsolationTests(unittest.TestCase):
    """The narrowed lock authorization survives the process boundary.

    Every other test here injects the launch, which is right for the decisions
    around it and blind to the one thing that only exists on the other side of
    a spawn: the child is a *new process*, reached through argv and an
    environment, and ``reclaim_stale_locks=False`` is an argument nothing in
    that crossing carries. So this fixture spawns nothing and skips nothing
    either --- it takes the argv and the environment the real launcher built,
    and drives ``run_entry.run_from_args`` with both, which is exactly what the
    child's own ``python -m agent_session_loop run`` would do.

    The corpus is two plans in one state root: the registered plan the overseer
    was authorized to resume, and an excluded plan holding a lock whose pid
    this host can prove is gone --- the lock the ordinary corpus-wide scan
    reclaims, and one no exclusion the operator wrote could have protected
    from a plan they *did* allow. Isolation is asserted against that file's
    bytes and mtime rather than against a return value.
    """

    #: A pid this host can prove is gone, so the excluded lock is deterministic.
    DEAD_PID = 4_242_424

    #: One repository is enough. What matters is that the plan is *registered*:
    #: a legacy plan resolves no workspace and never reaches the contention
    #: check at all, so the isolation would be untested against it.
    SPECS = (("SVC", "tundra_svc", "git@github.com:tundra/SVC.git"),)

    def setUp(self) -> None:
        from agent_session_loop import (
            plans_files,
            plans_metadata,
            plans_registry,
            run_contention,
            workspaces,
        )

        self.plans_files = plans_files
        self.run_contention = run_contention
        self.workspaces = workspaces

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved because `plans_snapshot.inspect_plan` canonicalizes the macOS
        # `/var/folders/...` symlink and a later comparison would differ.
        self.root = Path(tmp.name).resolve()
        self.state_root = self.root / "state"
        self.state_root.mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.repo = self.workspace / self.SPECS[0][1]
        self.repo.mkdir(parents=True)

        self.plan_dir = self.root / "allowed"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=self.SPECS)
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        self.registry = registry
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir, self.workspace, registry.digest
        )
        self.publish(self.plan_dir, payload={"run_config": self.run_config()})

        self.excluded = self.root / "excluded"
        self.excluded.mkdir()
        self.excluded_lock = self.publish(
            self.excluded,
            payload={"run_id": "20260101T000000Z-9", "harness": "claude"},
            lock={
                "version": 2,
                "pid": self.DEAD_PID,
                "hostname": socket.gethostname(),
                "repositories": [str(self.plans_files.canonicalize_path(self.repo))],
            },
        )

    # -- the corpus --------------------------------------------------------

    def publish(
        self,
        plan_dir: Path,
        *,
        payload: dict[str, Any],
        lock: dict[str, Any] | None = None,
    ) -> Path:
        """Write one plan's ``state.json`` and, when given, its ``lock.json``."""
        directory = state.plan_state_dir(plan_dir, self.state_root)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(plan_dir), **payload}), encoding="utf-8"
        )
        lock_path = directory / "lock.json"
        if lock is not None:
            lock_path.write_text(
                json.dumps(
                    {
                        "plan_dir": str(self.plans_files.canonicalize_path(plan_dir)),
                        "created_at": MOMENT,
                        **lock,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return lock_path

    def repositories(self) -> list[dict[str, str]]:
        """The resolved projection a registered run persists into ``run_config``.

        Spelled out rather than read back from a resolution, because the resume
        binding check compares the persisted block against the live one: a
        value derived from the same call on both sides would agree with itself.
        """
        return [
            {
                "alias": repository.alias,
                "path": repository.path,
                "origin": repository.origin,
                "resolved_root": str(
                    self.plans_files.canonicalize_path(self.workspace / repository.path)
                ),
            }
            for repository in self.registry
        ]

    def run_config(self) -> dict[str, Any]:
        """The launch block the paused run persisted, as ``--resume`` reads it.

        ``claude`` keeps the harness selection deterministic: resolving the
        native model for `codex` or `copilot` reads the developer's own config.
        """
        return {
            "harness": "claude",
            "timeout": 300,
            "commit_mode": "end",
            "unsafe_bypass": False,
            "allow_not_loop_ready": False,
            "cwd": str(self.workspace),
            "state_dir_override": str(self.state_root),
            "workspace_root": str(self.plans_files.canonicalize_path(self.workspace)),
            "registry_digest": self.registry.digest,
            "repositories": self.repositories(),
        }

    def fake_git(self) -> Any:
        """Answer the two Git questions workspace resolution asks, in process.

        This module is in ``PORTABLE_MODULES``, whose rule is that it spawns no
        external process at all, so the repositories are plain directories and
        the inspection is answered rather than run.
        """
        origins = {
            str(self.plans_files.canonicalize_path(self.workspace / path)): origin
            for _alias, path, origin in self.SPECS
        }

        def _git(root: Path, *args: str) -> str:
            if args[0] == "rev-parse":
                return str(root)
            return origins[str(root)]

        return mock.patch.object(self.workspaces, "_git_stdout", _git)

    def snapshot(self) -> tuple[bytes, int]:
        """The excluded lock as bytes and mtime.

        A reclaim that rewrote it with identical content would still be a write
        nothing authorized, so the mtime is part of what is compared.
        """
        stat = self.excluded_lock.stat()
        return self.excluded_lock.read_bytes(), stat.st_mtime_ns

    # -- the launch, and the child it describes -----------------------------

    def context(self) -> overseer_resume.LaunchContext:
        context = overseer_resume.recover_launch_context(
            plan_dir=str(self.plan_dir),
            repositories=(self.SPECS[0][0],),
            state_dir=self.state_root,
        )
        self.assertTrue(context.recovered, msg=context.detail)
        return context

    def capture(self, launch: Any) -> _FakePopen:
        """Run one launch with the launcher's own process seam and return the child."""
        captured: list[_FakePopen] = []

        def factory(argv: Any, **popen_kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **popen_kwargs)
            captured.append(process)
            return process

        with self.fake_git(), self.no_processes():
            launch(factory)

        self.assertEqual(1, len(captured))
        return captured[0]

    def no_processes(self) -> contextlib.ExitStack:
        """Fail loudly on any real process, which is this lane's whole claim.

        ``PORTABLE_MODULES`` membership is a promise about every test in the
        module, and these drive the widest paths in it: a launcher that exists
        to spawn, and an admission gate that resolves a Git workspace.
        """

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("this boundary must not spawn a process")

        stack = contextlib.ExitStack()
        for name in ("Popen", "run", "call", "check_call", "check_output"):
            stack.enter_context(mock.patch.object(subprocess, name, refuse))
        return stack

    def recovery_child(self) -> _FakePopen:
        """The child the overseer's *default* launch route would have started.

        Nothing on the route is replaced except the spawn itself, which the
        launcher already exposes as ``popen_factory``: the adapter's default
        ``launch`` runs, and so do ``intervention.resume_recovered_run`` and
        ``workbench_launch.launch_plan``, which is where the narrowing has to happen.
        """
        context = self.context()

        def launch(factory: Any) -> None:
            real = intervention.resume_recovered_run

            def with_seam(plan_dir: Any, **kwargs: Any) -> Any:
                return real(plan_dir, popen_factory=factory, **kwargs)

            with mock.patch.object(intervention, "resume_recovered_run", with_seam):
                overseer_resume.launch_recovered_run(context)

        return self.capture(launch)

    def manual_child(self) -> _FakePopen:
        """The child an ordinary operator-driven resume starts, for contrast."""
        profile = workbench_registry_models.LaunchProfile(**self.context().profile)

        def launch(factory: Any) -> None:
            workbench_launch.launch_plan(
                self.plan_dir,
                profile=profile,
                reply_text=None,
                state_dir=self.state_root,
                resume=True,
                popen_factory=factory,
            )

        return self.capture(launch)

    def drive(self, child: _FakePopen) -> tuple[int, dict[str, Any], list[Path]]:
        """Be the child: parse its argv, inherit its environment, run the gate.

        This is the boundary the finding was about. ``run_entry.run_from_args``
        is what ``python -m agent_session_loop run`` reaches through the CLI's
        ``handler_ref``, and it is called here exactly as the child would reach
        it --- with the child's own argv and the child's own environment, and
        with no ``reclaim_stale_locks`` argument at all, because a spawned
        child has no way to be passed one.
        """
        self.assertEqual(["-m", "agent_session_loop", "run"], child.argv[1:4])
        tokens = child.argv[3:]
        args = cli.build_parser().parse_args(tokens)
        assert child.env is not None
        held: list[Path] = []
        own_lock = state.plan_state_dir(self.plan_dir, self.state_root) / "lock.json"

        def run_locked(**kwargs: Any) -> tuple[int, dict[str, Any]]:
            # Recorded from inside the locked run: the resumed plan's own lock
            # is held here whatever the corpus-wide hygiene was narrowed to.
            if own_lock.is_file():
                held.append(own_lock)
            return 0, {"plan_dir": str(self.plan_dir), "ok": True}

        out = io.StringIO()
        with (
            self.fake_git(),
            self.no_processes(),
            mock.patch.dict(os.environ, child.env, clear=True),
            mock.patch.object(state, "is_pid_running", self.only_this_process),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = run_entry.run_from_args(args, run_argv=tokens, run_locked=run_locked)
        return code, json.loads(out.getvalue()), held

    @staticmethod
    def only_this_process(pid: int) -> bool:
        """Only this process is alive, which makes ``DEAD_PID`` provably dead."""
        return pid == os.getpid()

    # -- the property -------------------------------------------------------

    def test_resume_child_preserves_excluded_stale_locks(self) -> None:
        child = self.recovery_child()
        before = self.snapshot()

        code, payload, held = self.drive(child)

        self.assertEqual(0, code, msg=payload)
        self.assertTrue(payload.get("ok"), msg=payload)
        # The plan the overseer was never authorized for is byte-identical, and
        # the run that was authorized still held its own lock.
        self.assertTrue(self.excluded_lock.is_file())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, len(held))

    def test_an_ordinary_resume_still_reclaims_that_same_lock(self) -> None:
        """The existing contract, unchanged where it always applied.

        Without this the isolation test would pass against a corpus nothing
        would have reclaimed anyway.
        """
        child = self.manual_child()

        code, payload, _held = self.drive(child)

        self.assertEqual(0, code, msg=payload)
        self.assertFalse(self.excluded_lock.exists())

    def test_only_the_environment_tells_the_two_launches_apart(self) -> None:
        recovery, manual = self.recovery_child(), self.manual_child()

        # Same run, same flags: the narrowing is not a flag on the child's
        # command line, which is what keeps it off `asl run`'s own surface.
        self.assertEqual(manual.argv[3:], recovery.argv[3:])
        assert recovery.env is not None and manual.env is not None
        self.assertNotIn(self.run_contention.LOCK_ISOLATION_ENV_VAR, manual.env)
        self.assertEqual(
            {self.run_contention.LOCK_ISOLATION_ENV_VAR},
            set(recovery.env) - set(manual.env),
        )

    def test_the_isolated_child_still_refuses_an_overlapping_run(self) -> None:
        """Narrower writing, never a different answer.

        A live claim on the same repository is the refusal the contention check
        exists for, and an isolated child that stopped refusing it would let
        two agents into one working tree.
        """
        self.publish(
            self.root / "excluded",
            payload={"run_id": "20260101T000000Z-9"},
            lock={
                "version": 2,
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "repositories": [str(self.plans_files.canonicalize_path(self.repo))],
            },
        )
        child = self.recovery_child()

        code, payload, held = self.drive(child)

        self.assertEqual(1, code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_REPOSITORY_CONTENTION, payload["reason"]
        )
        self.assertEqual([], held)


if __name__ == "__main__":
    unittest.main()
