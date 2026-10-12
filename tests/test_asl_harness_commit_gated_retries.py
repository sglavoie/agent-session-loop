"""Bounded commit gates and verification repair attempts."""

from __future__ import annotations

import json
from pathlib import Path

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    config,
    run_commit,
    run_selection,
    verification_transitions,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    git_stdout,
    init_git_repo,
    install_fake_codex,
    read_jsonl,
    write_plan,
)


class GatedCommitRetryTests(FakeHarnessIntegrationBase):
    def test_a_paused_group_refused_every_time_stops_at_a_bounded_gate(self) -> None:
        """The other half of ASL-FINAL-002: bounded, not a commit loop.

        Spending the retry at the startup boundary must not turn that boundary
        into a retry loop. A repository that refuses every commit is an
        environment fault, and the run's job is to stop for a human rather than
        keep asking: the group spends exactly its budget --
        ``MAX_COMMIT_GROUP_RETRIES`` retries after the first attempt -- and then
        the startup commit gate refuses the dirt that is still there, which is
        the correct ending for dirt no commit can clear.
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
        # Far beyond any budget this run could spend, so the hook is "always
        # refuses" without the test having to know the bound it is measuring.
        refusals = self.refuse_first_commits_hook(repo_dir, tmp_dir, count=99)

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
        before = refusals.read_text(encoding="utf-8").count("refused")

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        spent = refusals.read_text(encoding="utf-8").count("refused") - before
        self.assertEqual(
            commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1, spent, second_completed
        )
        self.assertFalse(second_payload["commit_group_recovery"]["success"])
        self.assertEqual("commit-preflight", second_payload["pause_reason"])
        self.assertEqual([], second_payload["turns"])
        self.assertEqual(1, second_completed.returncode, second_completed.stderr)

    def install_gated_commit_harness(
        self, tmp_dir: Path, config_home: Path, *, enable_marker: Path
    ) -> Path:
        """Point the run's harness at one that answers no commit turn yet.

        A run resolves the same executable for its session turns and for its
        commit-message turns, so "the message harness is unavailable" cannot be
        staged by breaking the executable outright -- that would break the
        session turn with it. This wraps the fixture instead and refuses only
        the commit-message branch, recognized by the prompt prefix the fixture
        itself matches on, until ``enable_marker`` exists.

        Every refused *and* every forwarded commit turn is appended to the
        returned log, so a test can assert that the harness was not merely
        unable to answer but never asked.
        """
        # The opening words of `commit_message.build_commit_prompt`, which is
        # also how the fixture this wraps tells a commit turn from a session
        # one (`COMMIT_PROMPT_PREFIX` in tests/fixtures/fake_codex.py).
        commit_prompt_prefix = "You are a commit message generator."
        attempts = tmp_dir / "commit-harness-attempts.log"
        gate_dir = tmp_dir / "gated-harness"
        gate_dir.mkdir(parents=True, exist_ok=True)
        # The basename is what a harness executable is normally called, and the
        # directory is what keeps it from colliding with the fixture it wraps.
        wrapper = gate_dir / "codex"
        wrapper.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    'for arg in "$@"; do',
                    '  case "$arg" in',
                    f'    "{commit_prompt_prefix}"*)',
                    f'      echo "commit-message turn" >> "{attempts}"',
                    f'      if [ ! -f "{enable_marker}" ]; then',
                    '        echo "commit-message harness unavailable" >&2',
                    "        exit 127",
                    "      fi",
                    "      ;;",
                    "  esac",
                    "done",
                    f'exec "{install_fake_codex(tmp_dir)}" "$@"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.write_text(
            json.dumps({"codex": {"executable": str(wrapper)}}),
            encoding="utf-8",
        )
        return attempts

    def test_recovered_paused_group_reuses_the_deterministic_message_file(
        self,
    ) -> None:
        """Defect ASL-FINAL-001, now fixed: recovery does not re-generate.

        The settlement commits with ``--message-file`` for two reasons that
        both outlive the process that made it: the pause it marks may *be* the
        message harness failing, and a marking that says the work is unverified
        must not be rewordable by a model. Recovery of that same group used to
        honour neither -- it passed the run's own generation, whose
        ``message_file`` is unset, so the second process launched the
        commit-message harness for a group whose whole point was not needing
        one. With the harness unavailable, the recovery failed, the retained
        work stayed in the tree, and the startup commit preflight ended the
        relaunch with zero turns.

        The lever is a harness that refuses only the commit-message branch, and
        only until the session's own verification passes. So the harness is
        genuinely unavailable across the whole of the recovery, and genuinely
        available by the time the accepted session commit asks it for a message
        -- which is what keeps this a regression about the recovered
        ``session-paused`` group rather than about commit generation at large.
        """
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        tmp_dir = plan_dir.parent
        repo_dir = tmp_dir / "repo"
        init_git_repo(repo_dir)
        commit_log, commit_env = self.setup_commit_harness(tmp_dir)

        # The check fails its first run and passes every one after it, and its
        # passing run is also what makes the message harness available again.
        # Both markers live outside the repository, so neither is dirt the
        # settlement or the startup gate could trip over.
        attempted_marker = tmp_dir / "verification-attempted"
        enable_marker = tmp_dir / "commit-harness-enabled"
        check_script = tmp_dir / "verify-once-then-enable.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    f'if [ -f "{attempted_marker}" ]; then',
                    f'  : > "{enable_marker}"',
                    "  exit 0",
                    "fi",
                    f': > "{attempted_marker}"',
                    "exit 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        harness_attempts = self.install_gated_commit_harness(
            tmp_dir, config_home, enable_marker=enable_marker
        )
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={"1": self.settlement_contract(check_script)},
        )

        # Refuse the first commit the run attempts -- the settlement's -- so a
        # `session-paused` group is left owed for the second process to
        # recover, and step aside for every commit after it.
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
        owed_group = record["commit_group"]
        self.assertEqual(commit_groups.GROUP_SCOPE_SESSION_PAUSED, owed_group["scope"])
        # The message survives the process on the group itself, which is the
        # only route the next run has back to the exact text.
        expected_message = run_commit._paused_work_commit_message(
            session_label="session 1",
            pause_reason="verify-failed",
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            repair_attempt=record["verification_repair_attempt"],
            repair_max_attempts=record["verification_repair_max_attempts"],
            run_id=str(first_payload["run_id"]),
        )
        self.assertEqual(expected_message, owed_group["verbatim_message"])
        # Nothing has asked the harness for a message yet, and the retained
        # work is still in the tree for the relaunch to trip over.
        self.assertFalse(harness_attempts.exists())
        self.assertFalse(enable_marker.exists())
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

        # The recovery commit and the accepted one, in the order they ran. The
        # first carries `--message-file`; the second does not, so the scope the
        # settlement owns is the only one whose message stopped being
        # generated.
        recovered_commit, session_commit = second_payload["commits"]
        self.assertEqual(owed_group["group_id"], recovered_commit["group_id"])
        self.assertIn("--message-file", recovered_commit["argv"])
        self.assertNotIn("--message-file", session_commit["argv"])
        argv = recovered_commit["argv"]
        message_path = Path(argv[argv.index("--message-file") + 1])
        self.assertEqual(expected_message, message_path.read_text(encoding="utf-8"))

        # One commit-message turn across both processes, and it is the accepted
        # session's. The recovery never reached the harness at all -- not "was
        # refused by it", which is what the log distinguishes.
        self.assertEqual(
            ["commit-message turn"],
            harness_attempts.read_text(encoding="utf-8").splitlines(),
        )
        self.assertEqual(1, len(read_jsonl(commit_log)))
        self.assertEqual(
            [
                "feat: add the fake codex commit branch",
                "chore: retain unverified work from session 1",
                "Initial commit",
            ],
            git_stdout(repo_dir, "log", "--format=%s").splitlines(),
        )
        self.assertEqual(
            expected_message.strip(),
            git_stdout(repo_dir, "log", "-1", "--pretty=%B", "HEAD~1").strip(),
        )

    def test_the_at_end_commit_mode_also_survives_a_recoverable_pause(self) -> None:
        """``--commit-at-end`` retains and marks the same way, and says so.

        The at-end commit defers until after the last session and stages the
        whole worktree, so what a pause retains under it is genuinely mixed in
        with whatever earlier sessions left behind. The marking has to say that
        rather than claim the work is this turn's alone -- and the deferred
        end-of-run commit still has to happen on the run that finishes.
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

        first_completed, first_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_END,
            extra_env=commit_env,
        )

        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        record = first_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]
        self.assertEqual("committed", record["result"], record)
        self.assertEqual(run_selection.COMMIT_MODE_END, record["commit_mode"])
        self.assertEqual("", git_stdout(repo_dir, "status", "--porcelain").strip())
        # The wording the mode changes, pinned against git rather than against
        # the renderer alone.
        self.assertEqual(
            "chore: retain unverified work accumulated through session 1",
            git_stdout(repo_dir, "log", "-1", "--pretty=%s").strip(),
        )
        self.assertEqual(
            run_commit._paused_work_commit_message(
                session_label="session 1",
                pause_reason="verify-failed",
                commit_mode=run_selection.COMMIT_MODE_END,
                repair_attempt=record["verification_repair_attempt"],
                repair_max_attempts=record["verification_repair_max_attempts"],
                run_id=str(first_payload["run_id"]),
            ).strip(),
            git_stdout(repo_dir, "log", "-1", "--pretty=%B").strip(),
        )

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_END,
            extra_env=commit_env,
        )

        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertFalse(second_payload["paused"], second_payload)
        self.assertNotEqual([], second_payload["turns"])
        # Settling the pause does not consume the mode's own commit: the run
        # that finishes the plan still makes the deferred end-of-run one.
        self.assertEqual(1, len(second_payload["commits"]), second_payload["commits"])
        end_commit = second_payload["commits"][0]
        self.assertEqual("end", end_commit["scope"])
        self.assertEqual(
            [
                "feat: add the fake codex commit branch",
                "chore: retain unverified work accumulated through session 1",
                "Initial commit",
            ],
            git_stdout(repo_dir, "log", "--format=%s").splitlines(),
        )

    def test_verification_repair_attempts_are_exhausted_rather_than_dead_ended(
        self,
    ) -> None:
        """The budget is spent on repairs now, not on the commit gate.

        A session that cannot be repaired still has to stop, and the honest way
        for it to stop is to run out of verification repair attempts -- the
        budget the repair machinery declares -- and say so. Before the
        settlement it stopped one process earlier and for the wrong reason: the
        relaunch tripped the startup commit preflight, so a run that had never
        been given a second attempt was reported as an environment problem for
        a human to inspect.

        The check here fails the same way every time, so the second process's
        failure fingerprint repeats the first's and the budget is exhausted at
        its last attempt. What that produces is still ``verify-failed``, with
        the exhaustion stated in the pause details -- never ``commit-preflight``.
        """
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        tmp_dir = plan_dir.parent
        repo_dir = tmp_dir / "repo"
        init_git_repo(repo_dir)
        _commit_log, commit_env = self.setup_commit_harness(tmp_dir)
        check_script = self.settlement_check_script(tmp_dir, heals=False)
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={"1": self.settlement_contract(check_script)},
        )

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
        first_details = first_payload["pause_details"]
        self.assertEqual(1, first_details["verification_repair_attempt"])
        self.assertFalse(first_details["verification_repair_exhausted"])
        self.assertEqual(
            "committed",
            first_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]["result"],
        )
        self.assertEqual("", git_stdout(repo_dir, "status", "--porcelain").strip())

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=repo_dir,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            extra_env=commit_env,
        )

        self.assertEqual(1, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["paused"], second_payload)
        # The reason a supervisor reads, and the reason it no longer reads.
        self.assertEqual("verify-failed", second_payload["pause_reason"])
        self.assertNotEqual("commit-preflight", second_payload["pause_reason"])
        # The relaunch really did get its attempt: it ran a turn, and it spent
        # the last one the budget allows.
        self.assertNotEqual([], second_payload["turns"])
        second_details = second_payload["pause_details"]
        self.assertTrue(second_details["verification_repair_exhausted"])
        self.assertEqual(
            verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS,
            second_details["verification_repair_attempt"],
        )
        self.assertEqual(
            verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS,
            second_details["verification_repair_max_attempts"],
        )
        self.assertTrue(
            second_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY][
                "verification_repair_exhausted"
            ]
        )
