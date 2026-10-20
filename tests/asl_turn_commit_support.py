"""Shared fixtures for the ``test_asl_turn_commit_*`` modules."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_message,
    commit_settlement,
    events,
    harnesses,
    plans_content_hash,
    plans_handoff,
    plans_metadata,
    plans_models,
    plans_payloads,
    plans_readiness,
    plans_registry,
    review_guard,
    review_repair,
    run_commit,
    run_commit_paused,
    run_loop,
    run_persistence,
    run_selection,
    state,
    state_locks,
)
from agent_session_loop import verification_models as verification_module
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_git_repo,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
)

SPECS = DEFAULT_MULTI_REPO_SPECS[:2]


class PausedRetainedWorkCase(unittest.TestCase):
    """What a recoverable pause commits of the work its turn retained.

    The settlement is driven directly here: no loop is wired to it yet, and the
    claims are about which repositories it stages, what survives the commit,
    and what the record it leaves says. The commit subprocess is replaced by a
    fake that performs the real ``git add``/``git commit`` the staging asks
    for, so the re-probe that names residue is answered by a real tree rather
    than by the fake's own opinion of what it committed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, SPECS)
        self.plan_dir = self.tmp_path / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.registered = verification_module.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        self.legacy_root = self.tmp_path / "legacy"
        init_git_repo(self.legacy_root)
        self.legacy = verification_module.VerificationContext.legacy(self.legacy_root)
        self.run_dir = self.tmp_path / "run"
        self.event_path = self.tmp_path / "events.jsonl"
        self.artifact_dir = self.tmp_path / "turn-0001"
        self.payload: dict[str, Any] = {"commits": []}
        self.launched: list[dict[str, Any]] = []
        self.soft_failures: list[dict[str, Any]] = []

    # -- fixtures ---------------------------------------------------------

    def _write(self, root: Path, relative: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n", encoding="utf-8")

    def _guard(
        self,
        *,
        alias: str = "SVC",
        added_paths: tuple[str, ...] = (),
        passed: bool = True,
    ) -> dict[str, Any]:
        """The write-guard evidence a passing registered turn leaves behind."""
        return {
            "passed": passed,
            "authorized_aliases": [alias],
            "changed": {
                alias: {
                    "alias": alias,
                    "qualified_paths": [f"{alias}:{path}" for path in added_paths],
                    "tracked": [],
                    "untracked_added": list(added_paths),
                    "untracked_changed": [],
                    "worktree_of": None,
                }
            },
        }

    def _fake_commit(self, **kwargs: Any) -> dict[str, Any]:
        """Stage and commit exactly what the settlement asked for."""
        self.launched.append(dict(kwargs))
        root = Path(str(kwargs["cwd"]))
        staging = list(kwargs["staging_paths"])
        if staging:
            review_guard._run_git(
                root, ["add", "--", *commit_message.dequalify_paths(staging)]
            )
        else:
            review_guard._run_git(root, ["add", "-A"])
        message_file = kwargs["generation"].message_file
        assert message_file is not None
        review_guard._run_git(root, ["commit", "--no-verify", "-F", str(message_file)])
        head = review_guard._run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
        return {
            "success": True,
            "commit_sha": head,
            "commit_index": kwargs["commit_index"],
            "repository_key": kwargs["repository_key"],
        }

    def _refused_commit(self, **kwargs: Any) -> dict[str, Any]:
        self.launched.append(dict(kwargs))
        return {
            "success": False,
            "returncode": 1,
            "status_result": "nonzero-exit",
            "repository_key": kwargs["repository_key"],
        }

    def _soft_fail(self, **kwargs: Any) -> None:
        self.soft_failures.append(dict(kwargs))

    def _settle(
        self,
        context: verification_module.VerificationContext,
        *,
        guard: dict[str, Any] | None,
        commit_mode: str | None = run_selection.COMMIT_MODE_PER_SESSION,
        pause_reason: str = "verify-failed",
        pause_details: dict[str, Any] | None = None,
        is_review_session: bool = False,
        is_repair_session: bool = False,
        commit: Any = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        turn_payload: dict[str, Any] = {"turn_index": 1, "session": "1"}
        if guard is not None:
            turn_payload["workspace_write_guard"] = guard
        retained = commit_settlement.RetainedWorkContext(
            session_id="1",
            session_label=plans_payloads.session_label("1"),
            turn_payload=turn_payload,
            result_path=self.artifact_dir / "result.json",
            artifact_dir=self.artifact_dir,
            is_review_session=is_review_session,
            is_repair_session=is_repair_session,
        )
        with mock.patch.object(
            run_commit, "_run_asl_commit", side_effect=commit or self._fake_commit
        ):
            record = commit_settlement._settle_paused_retained_work(
                event_path=self.event_path,
                payload=self.payload,
                progress=mock.Mock(),
                verification_context=context,
                run_dir=self.run_dir,
                run_id="run-paused",
                retained=retained,
                pause_reason=pause_reason,
                pause_details=(
                    {
                        "verification_repair_attempt": 1,
                        "verification_repair_max_attempts": 2,
                        "verification_repair_exhausted": False,
                    }
                    if pause_details is None
                    else pause_details
                ),
                commit_mode=commit_mode,
                generation=run_commit._CommitGeneration(),
                soft_fail=self._soft_fail,
                persistence=run_persistence._RunPersistence(),
            )
        return record, turn_payload

    def _message_file(self) -> Path:
        return self.artifact_dir / run_commit_paused.PAUSED_WORK_MESSAGE_FILE_NAME

    def _events(self) -> list[dict[str, Any]]:
        # A settlement that owed nothing appends nothing, so the log may not
        # exist at all -- which is the same claim as an empty one.
        if not self.event_path.exists():
            return []
        return [
            record["payload"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.PAUSED_WORK_COMMITTED_EVENT
        ]

    def _dirty(self, root: Path) -> list[str]:
        return review_guard._porcelain_dirty_paths(
            review_guard._run_git(
                root, ["status", "--porcelain", "-z", "--untracked-files=all"]
            ).stdout
        )

    def _head(self, root: Path) -> str:
        return review_guard._run_git(root, ["rev-parse", "HEAD"]).stdout.strip()


class TurnCommitRunCase(unittest.TestCase):
    """Full runs, so the assertions are about the durable record, not calls."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=SPECS,
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
            self.plan_dir, self.workspace_root, registry.digest
        )
        # Real runs carry readiness metadata, so every turn rewrites manifest.md.
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self.context = verification_module.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        args = argparse.Namespace(
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
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=args,
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

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def _names(self) -> list[str]:
        return [record["name"] for record in self._records()]

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        return [
            record["payload"] for record in self._records() if record["name"] == name
        ]

    def _harness(self, *, writes: str, declares: str):
        """Complete the session by writing one file and one handoff line."""

        def run(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            alias, _, relative = writes.partition(":")
            (self.repositories[alias] / relative).write_text(
                "changed by the harness\n", encoding="utf-8"
            )
            session_path = self.plan_dir / "session-1.md"
            lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
            for index, line in enumerate(lines):
                if line.startswith(plans_handoff.STATUS_PREFIX):
                    lines[index] = f"{plans_handoff.STATUS_PREFIX} complete\n"
                elif line.startswith(plans_handoff.HANDOFF_FILES_PREFIX):
                    lines[index] = f"{plans_handoff.HANDOFF_FILES_PREFIX} {declares}\n"
            session_path.write_text("".join(lines), encoding="utf-8")
            return harnesses.HarnessResult(
                name="codex",
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        return run

    def _run_accepted(self) -> dict[str, Any]:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness(writes="SVC:README.md", declares="SVC:README.md"),
        ):
            exit_code, payload = self._invoke("commit-run")
        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        return payload

    def _mark_repair_session(self) -> None:
        """Make session 1 a review repair, which owns its own commit."""
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            review_repair.REVIEW_REPAIR_SESSION_MARKER
            + "\n"
            + review_repair.review_repair_session_version_marker()
            + "\n"
            + session_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
