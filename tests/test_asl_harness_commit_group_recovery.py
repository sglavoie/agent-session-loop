"""Per-session commit repair and paused-group recovery."""

from __future__ import annotations

import json
from pathlib import Path

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    plans,
    run_commit,
    run_selection,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    git_stdout,
    init_git_repo,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
    write_plan,
)


class PausedGroupCommitRecoveryTests(FakeHarnessIntegrationBase):
    #: The registered workspace the settlement regressions run in. Two
    #: repositories are enough to prove the startup commit gate reports on all
    #: of them: the turn only ever writes into ``SVC``, so ``OAK`` is the one
    #: the settlement must leave alone and the gate must still account for.
    SETTLEMENT_SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def test_per_session_commit_repairs_a_verify_failed_session_across_two_processes(
        self,
    ) -> None:
        """Defect ASL-PAUSED-RETAINED-WORK-COMMIT-PREFLIGHT, now fixed.

        A recoverable pause under a commit mode used to be a dead end for a
        supervisor. The first process failed verification, deliberately
        retained the turn's changes and paused ``verify-failed`` -- recoverable,
        so the supervisor relaunched. The second process then found the
        retained dirt at its startup commit preflight and paused
        ``commit-preflight``, non-recoverable with ``user_action: inspect``, so
        the repair attempt the first pause had earned never ran.

        What this pins is the autonomy the fix buys, end to end and in the
        shape a supervisor really sees it: one session whose check fails its
        first run and passes every one after it, two ``asl run`` *processes*
        over the same plan and the same state, and zero human input between
        them. The first process commits what it retained before it exits; the
        second meets a clean tree, passes the startup gate on every registered
        repository, spends the repair attempt the first pause earned, and
        finishes the session.

        Session 6 covers the same handover in-process against the gate itself.
        This runs it through real subprocesses, so what is asserted is the
        durable evidence a relaunch actually inherits -- ``state.json``, the
        gate's own per-repository record, and ``git log``.
        """
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        tmp_dir = plan_dir.parent
        workspace = tmp_dir / "workspace"
        repositories = init_multi_repo_workspace(workspace, self.SETTLEMENT_SPECS)
        commit_log, commit_env = self.setup_commit_harness(
            tmp_dir, mutation_path=str(repositories["SVC"] / "worked.txt")
        )
        check_script = self.settlement_check_script(tmp_dir)
        write_multi_repo_plan(
            plan_dir,
            ("pending",),
            specs=self.SETTLEMENT_SPECS,
            contracts_by_id={"1": self.settlement_contract(check_script, repo="SVC")},
        )
        registry = plans.read_repository_registry(plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(plan_dir, workspace, registry.digest)

        first_completed, first_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=workspace,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"], first_payload)
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        # A supervisor relaunches on this classification alone, so the second
        # process below is the one it would really start.
        self.assertTrue(first_payload["pause_policy"]["recoverable"])
        self.assertIsNone(first_payload["pause_policy"]["user_action"])
        # The evidence the settlement commits from is already on the turn when
        # the pause is written: the write guard passed before verification ran,
        # so the retained paths are named and authorized.
        first_turn = first_payload["turns"][-1]
        self.assertTrue(first_turn["workspace_write_guard"]["passed"])
        # Verification reverted the session but kept the work, and the pause
        # then committed it. The revert has to come first, or the commit would
        # record the session as `complete` and the revert would re-dirty the
        # plan behind it.
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        # The settlement's own verdict, read from the state file rather than
        # the process's stdout, because the state file is what the relaunch and
        # its supervisor inherit.
        state_payload = json.loads(
            Path(str(first_payload["state_path"])).read_text(encoding="utf-8")
        )
        record = state_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]
        self.assertEqual("committed", record["result"], record)
        self.assertEqual(commit_settlement.PAUSED_WORK_COMMIT_GUARD, record["guard"])
        self.assertEqual("verify-failed", record["pause_reason"])
        self.assertEqual([], record["residue"]["repositories"])
        self.assertEqual(
            commit_groups.GROUP_SCOPE_SESSION_PAUSED,
            record["commit_group"]["scope"],
        )
        self.assertEqual(1, len(first_payload["commits"]), first_payload["commits"])
        # And it committed without a generation turn. `commit_log` records one
        # line per commit-message harness call, and the settlement makes none:
        # it passes `--message-file` with a deterministic marking, so a pause
        # caused by a sick harness never has to ask that harness for a message.
        self.assertFalse(commit_log.exists())
        # Nothing retained survives the process, which is the whole point: the
        # relaunch below meets a clean tree instead of unexplained dirt.
        for alias, repository in repositories.items():
            self.assertEqual(
                "", git_stdout(repository, "status", "--porcelain").strip(), alias
            )

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=workspace,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertFalse(second_payload["paused"], second_payload)
        # The gate that used to end the run now records every registered
        # repository as clean -- including the one the settlement never touched,
        # which is what makes this a report on the workspace and not on the
        # single repository the turn happened to write into.
        preflight = second_payload["commit_preflight"]
        self.assertEqual(
            ["SVC", "OAK"], [row["alias"] for row in preflight["repositories"]]
        )
        for row in preflight["repositories"]:
            self.assertTrue(row["clean"], row)
        # The autonomy. The relaunch spends the repair attempt the first pause
        # earned, and the check that failed once now passes.
        self.assertNotEqual([], second_payload["turns"])
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

        # Two commits, in the order the repair happened: the marked retained
        # work first, then the session commit the passing verification earned.
        subjects = git_stdout(repositories["SVC"], "log", "--format=%s").splitlines()
        self.assertEqual(
            [
                "feat: add the fake codex commit branch",
                "chore: retain unverified work from session 1",
                "Initial commit",
            ],
            subjects,
        )
        # The retained-work commit carries the whole deterministic marking, not
        # just a matching subject.
        self.assertEqual(
            run_commit._paused_work_commit_message(
                session_label="session 1",
                pause_reason="verify-failed",
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                repair_attempt=record["verification_repair_attempt"],
                repair_max_attempts=record["verification_repair_max_attempts"],
                run_id=str(first_payload["run_id"]),
            ).strip(),
            git_stdout(
                repositories["SVC"], "log", "-1", "--pretty=%B", "HEAD~1"
            ).strip(),
        )
        # The repository the session declared but never wrote is untouched by
        # both commits.
        self.assertEqual(
            ["Initial commit"],
            git_stdout(repositories["OAK"], "log", "--format=%s").splitlines(),
        )
        # One generation turn across both processes: the accepted commit asked
        # the harness for a message, the retained-work commit did not.
        self.assertEqual(1, len(read_jsonl(commit_log)))

    def test_a_paused_work_commit_that_fails_is_completed_before_the_next_runs_gate_a(
        self,
    ) -> None:
        """A settlement that could not commit is owed, not lost.

        The settlement is a commit, so it can fail the way any commit can. When
        it does it soft-fails and leaves an unresolved ``session-paused`` group
        behind, and the retained work is still in the worktree -- exactly the
        state the startup commit preflight refuses. The relaunch is only
        autonomous if the owed group is settled *before* that gate runs, so
        that is what this pins: the second process finds the same group by id,
        completes it, and reaches a turn instead of pausing ``commit-preflight``
        with nothing done.

        The failure lever is a one-shot ``prepare-commit-msg`` hook rather than
        the fake harness's commit-failure mode. ``FAKE_CODEX_COMMIT_FAIL_LIMIT``
        only reaches a commit-message *generation* turn, and the settlement
        deliberately makes none -- it commits with ``--message-file``. A git
        hook fails the commit itself, which is the layer the settlement is
        actually exposed at, and it heals on its second invocation without
        touching the generation turn the recovery below does make.
        """
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        tmp_dir = plan_dir.parent
        repo_dir = tmp_dir / "repo"
        init_git_repo(repo_dir)
        _commit_log, commit_env = self.setup_commit_harness(tmp_dir)
        check_script = self.settlement_check_script(tmp_dir)
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={"1": self.settlement_contract(check_script)},
        )

        # `git commit --no-verify` still runs `prepare-commit-msg`, so this
        # refuses the first commit the run attempts -- the settlement's -- and
        # steps aside for every one after it. The marker lives outside the
        # repository so refusing does not itself leave dirt behind.
        hook_marker = tmp_dir / "hook-refused"
        hook = repo_dir / ".git" / "hooks" / "prepare-commit-msg"
        hook.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    f'if [ -f "{hook_marker}" ]; then exit 0; fi',
                    f': > "{hook_marker}"',
                    'echo "prepare-commit-msg refused this commit" >&2',
                    "exit 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        hook.chmod(0o755)

        first_completed, first_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        record = first_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]
        self.assertEqual("failed", record["result"], record)
        # The commit itself is what failed -- the hook refused it -- so the
        # settlement's verdict rests on the commit's own status, not on an
        # exception raised around it.
        self.assertFalse(record["commit"]["success"])
        self.assertEqual("nonzero-exit", record["commit"]["status_result"])
        owed_group = record["commit_group"]
        self.assertEqual(commit_groups.GROUP_SCOPE_SESSION_PAUSED, owed_group["scope"])
        self.assertEqual("failed", owed_group["status"])
        # The work is still there, and the pause is still the recoverable one:
        # a failed settlement does not turn a repairable pause into an
        # environment refusal, it just leaves a debt.
        self.assertTrue(first_payload["pause_policy"]["recoverable"])
        self.assertNotEqual("", git_stdout(repo_dir, "status", "--porcelain").strip())

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        # The ordering proof. This process started against a dirty worktree, so
        # had the startup preflight run first it would have paused
        # `commit-preflight` before any turn -- the outcome the neighbouring
        # dirty-worktree test pins. Instead the owed group, identified by the
        # id the first process recorded, is settled and the run carries on.
        recovery = second_payload["commit_group_recovery"]
        self.assertEqual(owed_group["group_id"], recovery["group_id"])
        self.assertTrue(recovery["success"], recovery)
        self.assertNotEqual("commit-preflight", second_payload.get("pause_reason"))
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertFalse(second_payload["paused"], second_payload)
        self.assertNotEqual([], second_payload["turns"])
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertEqual("", git_stdout(repo_dir, "status", "--porcelain").strip())

    def test_two_failed_paused_group_attempts_do_not_fall_into_gate_a(self) -> None:
        """Defect ASL-FINAL-002, now fixed: the re-armed retry is spent in time.

        One transient refusal already healed: the relaunch recovered the owed
        group before its startup commit gate ran. A *second* refusal did not.
        The failed recovery re-armed the group -- correctly, it has a retry
        budget -- but the boundary that consumes a re-armed retry is the top of
        the run loop, and the startup commit preflight stands between the two.
        So the relaunch met its own unresolved dirt at a gate whose pause is
        non-recoverable, and a supervised run stopped with zero turns and a
        retry it never got to spend.

        The lever is the same hook as the single-refusal test, refusing twice
        instead of once: refusal one is the settlement in the first process,
        refusal two is the recovery at the start of the second. What this pins
        is that the second process spends the retry that refusal armed, lands
        the commit, and reaches the pending session's turn -- and that the
        refusals it took to get there were really two, not one.
        """
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        tmp_dir = plan_dir.parent
        repo_dir = tmp_dir / "repo"
        init_git_repo(repo_dir)
        _commit_log, commit_env = self.setup_commit_harness(tmp_dir)
        check_script = self.settlement_check_script(tmp_dir)
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={"1": self.settlement_contract(check_script)},
        )
        refusals = self.refuse_first_commits_hook(repo_dir, tmp_dir, count=2)

        first_completed, first_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        record = first_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]
        self.assertEqual("failed", record["result"], record)
        owed_group = record["commit_group"]
        self.assertEqual(commit_groups.GROUP_SCOPE_SESSION_PAUSED, owed_group["scope"])
        self.assertTrue(first_payload["pause_policy"]["recoverable"])
        self.assertNotEqual("", git_stdout(repo_dir, "status", "--porcelain").strip())

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        # Both refusals landed, so this really is the two-failure case and not
        # the single-refusal one passing under a different name.
        self.assertEqual(
            2, refusals.read_text(encoding="utf-8").count("refused"), second_completed
        )
        # The first attempt of the second process failed and said so, and it
        # said it was going to try again.
        startup_failures = [
            failure
            for failure in second_payload[commit_settlement.COMMIT_FAILURES_KEY]
            if failure.get("group_id") == owed_group["group_id"]
        ]
        self.assertNotEqual([], startup_failures, second_payload)
        self.assertTrue(startup_failures[0]["retrying"], startup_failures[0])
        # And the retry it armed was spent before Gate A rather than behind it:
        # the group is resolved, the gate never fired, and the run reached the
        # pending session's turn.
        recovery = second_payload["commit_group_recovery"]
        self.assertEqual(owed_group["group_id"], recovery["group_id"])
        self.assertTrue(recovery["success"], recovery)
        self.assertNotEqual("commit-preflight", second_payload.get("pause_reason"))
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertFalse(second_payload["paused"], second_payload)
        self.assertNotEqual([], second_payload["turns"])
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertEqual("", git_stdout(repo_dir, "status", "--porcelain").strip())
