"""Workspace guard exception tests."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    external_write_evidence,
    harnesses,
    plans_metadata,
    plans_registry,
    plans_snapshot,
    run_loop,
    state,
    state_locks,
    turn_execution_artifacts,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_git_repo,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
)


class WorkspaceGuardExceptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=DEFAULT_MULTI_REPO_SPECS[:2],
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session="1",
                )
            },
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.workspace_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state_locks.release_lock(self.plan_dir, self.state_dir)

    def _pause_reasons(self) -> list[str]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]["reason"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ]

    def _completed_harness_result(
        self, *_args: object, **_kwargs: object
    ) -> harnesses.HarnessResult:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name="codex",
            argv=["fake"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    def test_external_authority_pause_and_action_agree_with_the_guard(self) -> None:
        # The producer's real shape: ``reason`` at the top level, and an
        # ``after`` digest that carries no reason of its own (ASI-153).
        self._assert_external_authority_pause(
            {
                "/tmp/pi-gradle.py": {
                    "reason": "external-write-unclassified",
                    "surface": "backtick-path",
                    "after": {
                        "present": True,
                        "kind": "file",
                        "mtime_ns": 1,
                        "size": 12,
                        "sha256": "a" * 64,
                    },
                }
            }
        )

    def test_recorded_private_var_tmp_row_is_not_a_repository_failure(
        self,
    ) -> None:
        # Verbatim row from run 20260919T111001880618Z-14051, whose pause
        # blamed "repository evidence could not be captured safely" (ASI-153).
        self._assert_external_authority_pause(
            {
                "/private/var/tmp": {
                    "after": {
                        "entry_count": 0,
                        "kind": "directory",
                        "mtime_ns": 1789845003102184572,
                        "present": True,
                        "sha256": (
                            "e3b0c44298fc1c149afbf4c8996fb924"
                            "27ae41e4649b934ca495991b7852b855"
                        ),
                        "size": 64,
                        "truncated": False,
                    },
                    "reason": "external-write-unclassified",
                    "surface": "absolute-path",
                }
            }
        )

    def _assert_external_authority_pause(self, unavailable: dict[str, Any]) -> None:
        with (
            mock.patch.object(
                harnesses, "run_harness", side_effect=self._completed_harness_result
            ),
            mock.patch.object(
                external_write_evidence,
                "_turn_external_write_evidence",
                return_value={"unclassified": unavailable},
            ),
        ):
            exit_code, payload = self._invoke("external-authority-diagnostic")
        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertFalse(guard["passed"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual(unavailable, guard["unavailable"])
        for message in (
            payload["pause_message"],
            payload["action_required"]["message"],
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        ):
            self.assertIn("external paths have unresolved write authority", message)
            self.assertNotIn("repository evidence could not be captured", message)
        self.assertEqual(
            "pending", plans_snapshot.inspect_plan(self.plan_dir).sessions[0].status
        )

    def test_harness_exception_records_scope_violation_and_blocks_rerun(
        self,
    ) -> None:
        target = self.repositories["OAK"] / "README.md"

        def mutate_then_raise(*_args: object, **_kwargs: object) -> None:
            target.write_text("retained unexpected mutation\n", encoding="utf-8")
            raise RuntimeError("synthetic harness crash")

        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=mutate_then_raise,
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic harness crash"),
        ):
            self._invoke("exception-run")

        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        pause_records = [
            record
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ]
        self.assertEqual(1, len(pause_records))
        pause = pause_records[0]["payload"]
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            pause["reason"],
        )
        guard = pause["details"]["workspace_write_guard"]
        self.assertEqual(
            ["OAK:README.md"],
            guard["violations"][0]["qualified_paths"],
        )
        self.assertIn("snapshot_before", guard)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=AssertionError("rerun must stop before the harness"),
        ):
            exit_code, payload = self._invoke("blocked-rerun")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])

    def test_unregistered_sibling_write_does_not_pause_the_run(self) -> None:
        sibling = self.workspace_root / "unrelated-active-work"
        init_git_repo(sibling)

        def mutate_sibling(*args: object, **kwargs: object) -> harnesses.HarnessResult:
            (sibling / "concurrent.txt").write_text(
                "another run is working here\n", encoding="utf-8"
            )
            return self._completed_harness_result(*args, **kwargs)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=mutate_sibling,
        ):
            exit_code, payload = self._invoke("collateral-write-run")

        self.assertEqual(0, exit_code)
        self.assertEqual([], self._pause_reasons())
        self.assertTrue(payload["turns"][0]["workspace_write_guard"]["passed"])
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertTrue((sibling / "concurrent.txt").is_file())

    def test_registered_repository_removal_still_pauses(self) -> None:
        removed = self.repositories["OAK"]

        def remove_then_complete(
            *args: object, **kwargs: object
        ) -> harnesses.HarnessResult:
            shutil.rmtree(removed)
            return self._completed_harness_result(*args, **kwargs)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=remove_then_complete,
        ):
            exit_code, payload = self._invoke("removed-repository-run")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertEqual(["tundra_OAK"], guard["removed"])
        self.assertEqual("repository-removed", guard["violations"][0]["kind"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_registered_repository_symlink_swap_still_pauses(self) -> None:
        registered = self.repositories["OAK"]
        twin = self.tmp_path / "oak-twin"
        subprocess.run(
            ["git", "clone", str(registered), str(twin)],
            text=True,
            capture_output=True,
            check=True,
        )

        def swap_then_complete(
            *args: object, **kwargs: object
        ) -> harnesses.HarnessResult:
            shutil.rmtree(registered)
            registered.symlink_to(twin)
            return self._completed_harness_result(*args, **kwargs)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=swap_then_complete,
        ):
            exit_code, payload = self._invoke("symlinked-repository-run")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertEqual(["tundra_OAK"], guard["removed"])
        self.assertEqual("repository-removed", guard["violations"][0]["kind"])
        self.assertTrue(registered.is_symlink())
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_post_harness_system_exit_records_scope_violation(self) -> None:
        target = self.repositories["OAK"] / "README.md"

        def mutate_then_exit(*_args: object, **_kwargs: object) -> None:
            target.write_text("retained system-exit mutation\n", encoding="utf-8")
            raise SystemExit(9)

        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._completed_harness_result,
            ),
            mock.patch.object(
                turn_execution_artifacts,
                "write_turn_artifacts",
                side_effect=mutate_then_exit,
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            self._invoke("system-exit-run")

        self.assertEqual(9, raised.exception.code)
        self.assertIn(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            self._pause_reasons(),
        )
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_post_harness_keyboard_interrupt_records_scope_violation(self) -> None:
        target = self.repositories["OAK"] / "README.md"

        def mutate_then_interrupt(*_args: object, **_kwargs: object) -> None:
            target.write_text("retained interrupted mutation\n", encoding="utf-8")
            raise KeyboardInterrupt

        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._completed_harness_result,
            ),
            mock.patch.object(
                turn_execution_artifacts,
                "write_turn_artifacts",
                side_effect=mutate_then_interrupt,
            ),
        ):
            exit_code, payload = self._invoke("keyboard-interrupt-run")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        self.assertNotEqual("interrupted", payload["pause_reason"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_scope_repair_requirement_gates_named_later_session(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        contracts_by_id = {
            session_id: _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session=session_id,
            )
            for session_id in ("1", "2")
        }
        write_multi_repo_plan(
            self.plan_dir,
            ("pending", "pending"),
            specs=specs,
            contracts_by_id=contracts_by_id,
        )
        for session_id in ("1", "2"):
            session_path = self.plan_dir / f"session-{session_id}.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "**Reads repositories**: SVC, OAK\n"
                    "**Writes repositories**: SVC, OAK",
                    "**Reads repositories**: SVC\n**Writes repositories**: SVC",
                ),
                encoding="utf-8",
            )
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

        reached: list[str] = []

        def complete_session(name: str, **kwargs: object) -> harnesses.HarnessResult:
            prompt = str(kwargs.get("prompt", ""))
            session_id = "2" if "session-2.md" in prompt else "1"
            reached.append(session_id)
            if session_id == "2":
                (self.repositories["OAK"] / "README.md").write_text(
                    "retained out-of-scope mutation\n",
                    encoding="utf-8",
                )
            session_path = self.plan_dir / f"session-{session_id}.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )
            return harnesses.HarnessResult(
                name=name,
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=complete_session,
        ):
            first_exit, first_payload = self._invoke("scope-run-1")

        self.assertEqual(1, first_exit)
        self.assertEqual(["1", "2"], reached)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            first_payload["pause_reason"],
        )

        first_session = self.plan_dir / "session-1.md"
        first_session.write_text(
            first_session.read_text(encoding="utf-8").replace(
                "- **Status**: complete", "- **Status**: pending", 1
            ),
            encoding="utf-8",
        )
        reached.clear()

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=complete_session,
        ):
            second_exit, second_payload = self._invoke("scope-run-2")

        self.assertEqual(1, second_exit)
        self.assertEqual(["1"], reached)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            second_payload["pause_reason"],
        )


if __name__ == "__main__":
    unittest.main()
