"""Settled pause commit gate tests."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_groups,
    events,
    harnesses,
    plans_metadata,
    plans_registry,
    run_loop,
    run_selection,
    state,
    state_locks,
    verification_models,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    fake_commit_harness_env,
    git_stdout,
    init_multi_repo_workspace,
    patched_env,
    read_jsonl,
    write_multi_repo_plan,
)


class SettledPauseCommitGateTests(unittest.TestCase):
    """The startup commit gate on the run a settled pause hands over to.

    The arrangement is the one the settlement exists for: one session whose
    verification fails its first attempt and passes every one after it, two
    ``run_locked`` passes over the same plan and the same state, and no human
    between them. What the first pass leaves in the worktree is the only thing
    that decides whether the second can start at all, so both outcomes are
    pinned here -- the clean handover the settlement is meant to produce, and
    the honest refusal that still happens when something the commit could not
    stage stayed behind.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, self.SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.commit_env = fake_commit_harness_env(self.tmp_path)
        self.marker = self.tmp_path / "verification-attempted"
        self.turns = 0

    def _write_plan(self, *, dirt: Path | None = None) -> None:
        script = self.tmp_path / "verify-once.sh"
        lines = ["#!/bin/sh"]
        if dirt is not None:
            # Verification runs after the post-turn write guard, so a check
            # that writes into a registered repository leaves a path no guard
            # evidence names -- and therefore one no staging derived from that
            # evidence can take with it.
            lines.append(f': > "{dirt}"')
        lines.extend(
            [
                # Fails its first run, passes every one after it: the pause the
                # first pass records is real, and the second pass is stopped by
                # the worktree or by nothing.
                f'if [ -f "{self.marker}" ]; then exit 0; fi',
                f': > "{self.marker}"',
                "exit 1",
                "",
            ]
        )
        script.write_text("\n".join(lines), encoding="utf-8")
        check = json.dumps(
            {
                "type": "command",
                "name": "verify-once",
                "repo": "SVC",
                "argv": ["sh", str(script)],
                "timeout_seconds": 60,
            }
        )
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=self.SPECS,
            contracts_by_id={"1": _contract_block(check, session="1")},
        )
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification_models.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

    def _work(self, name: str, **_kwargs: Any) -> harnesses.HarnessResult:
        self.turns += 1
        (self.repositories["SVC"] / "worked.txt").write_text(
            f"attempt {self.turns}\n", encoding="utf-8"
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name=name, argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _refuse(self, name: str, **_kwargs: Any) -> harnesses.HarnessResult:
        raise AssertionError(f"the harness must not be reached; got {name}")

    def _invoke(self, run_id: str, harness: Any) -> tuple[int, dict[str, Any]]:
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
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            with (
                patched_env(**self.commit_env),
                mock.patch.object(harnesses, "run_harness", side_effect=harness),
            ):
                return run_loop.run_locked(
                    args=args,
                    cwd=self.workspace_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                    lock_info=lock_info,
                    run_id=run_id,
                    verification_context=self.context,
                )
        finally:
            state_locks.release_lock(self.plan_dir, self.state_dir)

    def _git(self, alias: str, *args: str) -> str:
        return git_stdout(self.repositories[alias], *args)

    def _session_status(self) -> str:
        for line in (
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8").splitlines()
        ):
            if line.startswith("- **Status**:"):
                return line.split(":", 1)[1].strip()
        self.fail("session-1.md declares no status")

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

    def test_commit_preflight_accepts_a_relaunch_after_the_prior_run_settled(
        self,
    ) -> None:
        self._write_plan()

        exit_code, first = self._invoke("settled-run-1", self._work)

        self.assertEqual(1, exit_code)
        self.assertEqual("verify-failed", first["pause_reason"])
        self.assertTrue(first["pause_policy"]["recoverable"])
        self.assertEqual("pending", self._session_status())
        # The settlement took everything the turn retained, in the one
        # repository the turn wrote to, and admitted no residue.
        record = first["paused_work_commit"]
        self.assertEqual("committed", record["result"])
        self.assertEqual([], record["residue"]["repositories"])
        for alias in ("SVC", "OAK"):
            self.assertEqual("", self._git(alias, "status", "--porcelain"))

        exit_code, second = self._invoke("settled-run-2", self._work)

        # Gate A ran, passed, and recorded every registered repository as
        # clean -- the record is what a supervisor reads to know the handover
        # was clean rather than merely unblocked.
        preflight = second["commit_preflight"]
        self.assertEqual(
            ["SVC", "OAK"], [row["alias"] for row in preflight["repositories"]]
        )
        self.assertTrue(all(row["clean"] for row in preflight["repositories"]))
        self.assertEqual(0, exit_code)
        self.assertFalse(second["paused"])
        self.assertNotIn("commit-preflight", self._pause_reasons())
        # And the relaunch spent the repair attempt the first pause earned.
        self.assertEqual("1", second["turns"][0]["session_id"])
        self.assertEqual("complete", self._session_status())

    def test_a_settled_pause_that_left_residue_still_pauses_preflight_and_names_it(
        self,
    ) -> None:
        residue_path = self.repositories["SVC"] / "check-artifact.log"
        self._write_plan(dirt=residue_path)

        exit_code, first = self._invoke("residue-run-1", self._work)

        self.assertEqual(1, exit_code)
        self.assertEqual("verify-failed", first["pause_reason"])
        # The commit landed -- the turn's own work is in the history -- but a
        # worktree still dirty afterwards is what ``run_commit`` calls an
        # unsuccessful commit, so the settlement fails and the group stays
        # owed. That verdict is not softened here; what the record adds is the
        # residue, re-probed rather than assumed away by the failure.
        record = first["paused_work_commit"]
        self.assertEqual("failed", record["result"])
        self.assertEqual("dirty-after-commit", record["commit"]["status_result"])
        self.assertEqual(
            [
                {
                    "alias": "SVC",
                    "clean": False,
                    "dirty_paths": ["SVC:check-artifact.log"],
                    "error": None,
                }
            ],
            record["residue"]["repositories"],
        )
        self.assertEqual(
            "worked.txt", self._git("SVC", "show", "--name-only", "--format=")
        )
        self.assertEqual(
            "?? check-artifact.log", self._git("SVC", "status", "--porcelain")
        )
        # The failure was absorbed rather than paused on, and it left the run
        # ending with the paused-work group still unresolved.
        self.assertEqual(
            [commit_groups.GROUP_SCOPE_SESSION_PAUSED],
            [row["scope"] for row in first["commit_failures"]],
        )
        self.assertEqual(
            [(commit_groups.GROUP_SCOPE_SESSION_PAUSED, "failed")],
            [
                (row["scope"], row["status"])
                for row in first["unresolved_commit_groups"]
            ],
        )

        exit_code, second = self._invoke("residue-run-2", self._refuse)

        # The gate is not taught to tolerate what the settlement could not
        # take: dirt it did not author is still dirt, and the run stops before
        # a turn. What the fix buys here is the naming, not the acceptance --
        # the prior run's record already says which paths these are.
        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", second["pause_reason"])
        self.assertEqual([], second["turns"])
        self.assertNotIn("commit_preflight", second)
        self.assertIn("SVC", second["pause_message"])
        self.assertEqual(
            ["SVC:check-artifact.log"],
            [
                path
                for row in record["residue"]["repositories"]
                for path in row["dirty_paths"]
            ],
        )
        self.assertEqual(
            "?? check-artifact.log", self._git("SVC", "status", "--porcelain")
        )

    def test_dirty_after_commit_failure_message_says_the_commit_landed(self) -> None:
        # `failed` is one word for two opposite outcomes. Here the commit
        # exited zero and moved HEAD -- the turn's own work is in the history
        # -- and only the check's artifact stayed behind. A message that said
        # the retained work could not be committed would be the one part of
        # the evidence that disagreed with `git log`, and would say nothing
        # about the path the relaunch's gate is about to refuse over.
        residue_path = self.repositories["SVC"] / "check-artifact.log"
        self._write_plan(dirt=residue_path)

        exit_code, first = self._invoke("landed-message-run", self._work)

        self.assertEqual(1, exit_code)
        record = first["paused_work_commit"]
        self.assertEqual("failed", record["result"])
        self.assertEqual("dirty-after-commit", record["commit"]["status_result"])
        # The commit really did land: HEAD advanced onto the turn's file.
        self.assertNotEqual(
            record["commit"]["head_before"], record["commit"]["head_after"]
        )
        self.assertEqual(
            "worked.txt", self._git("SVC", "show", "--name-only", "--format=")
        )
        # The durable soft-failure message -- the same text the progress line
        # prints and the supervisor audit bundle carries -- names the landing
        # and the residue path, and drops the could-not-be-committed claim.
        self.assertEqual(
            [
                "the verify-failed pause of session 1 committed the work it "
                "retained, but the worktree still holds SVC:check-artifact.log"
            ],
            [row["message"] for row in first["commit_failures"]],
        )
        self.assertEqual(
            ["SVC:check-artifact.log"],
            [
                path
                for row in record["residue"]["repositories"]
                for path in row["dirty_paths"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
