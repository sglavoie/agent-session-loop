"""The verbatim retained-work commit message and how a recovered group selects it."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_session_loop import (
    commit_groups,
    commit_message,
    commit_settlement,
    pause_policy,
    plans_payloads,
    run_commit,
    run_commit_paused,
    run_selection,
    runner,
)


class PausedWorkCommitMessageTests(unittest.TestCase):
    """The retained-work commit is verbatim text, and its argv says so.

    Settling the work a recoverable pause retained must not spend another
    bounded harness turn -- the pause may be the harness failing -- so the
    commit goes out through ``--message-file`` with a message this module
    renders. Both halves are asserted here: the exact argv that selects the
    mode, and the exact text it commits.
    """

    #: The recoverable pauses that carry no repair budget, derived from the
    #: policy rather than restated, so a fifth recoverable reason is covered
    #: here the moment it is added.
    NO_BUDGET_REASONS = tuple(
        reason
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS
        if reason != "verify-failed"
    )

    @staticmethod
    def _flat(message: str) -> str:
        """The message as one line, so a substring assertion sees no wrap."""
        return message.replace("\n", " ")

    def _message(self, **overrides: Any) -> str:
        kwargs: dict[str, Any] = {
            "session_label": plans_payloads.session_label("3"),
            "pause_reason": "verify-failed",
            "commit_mode": run_selection.COMMIT_MODE_PER_SESSION,
            "repair_attempt": 1,
            "repair_max_attempts": 2,
            "run_id": "run-20260904-abc123",
        }
        kwargs.update(overrides)
        return run_commit_paused._paused_work_commit_message(**kwargs)

    def test_the_default_generation_still_asks_for_a_generated_message(self) -> None:
        # The mode is opt-in: every commit a run makes on its accepted path
        # must keep generating its message.
        self.assertIsNone(run_commit._CommitGeneration().message_file)
        self.assertEqual(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                *run_commit.COMMIT_SUBCOMMAND_ARGV,
                "--timeout",
                str(commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS),
            ],
            run_commit._asl_commit_argv(run_commit._CommitGeneration()),
        )

    def test_the_message_file_argv_composes_with_the_bound_and_pathspec(self) -> None:
        # `--timeout` is unconditional and `--pathspec` still scopes the
        # staging, so the message-file argv is the default argv plus one flag
        # pair -- which is what makes the retained-work commit legal: `asl
        # commit` refuses `--message-file` only next to `--from-id` and
        # `--prepare-only`, neither of which a run passes.
        self.assertEqual(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                *run_commit.COMMIT_SUBCOMMAND_ARGV,
                "--timeout",
                str(commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS),
                "--message-file",
                "/tmp/paused-work.txt",
                "--pathspec",
                "worked.txt",
            ],
            run_commit._asl_commit_argv(
                run_commit._CommitGeneration(message_file=Path("/tmp/paused-work.txt")),
                ["SVC:worked.txt"],
            ),
        )

    def test_the_flag_travels_with_the_harness_selection_it_overrides(self) -> None:
        # A run that named a harness still forwards that selection; the
        # message-file mode just means the nested command never uses it.
        self.assertEqual(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                *run_commit.COMMIT_SUBCOMMAND_ARGV,
                "--timeout",
                "60",
                "--harness",
                "codex",
                "--model",
                "gpt-5",
                "--effort",
                "high",
                "--message-file",
                "/tmp/paused-work.txt",
            ],
            run_commit._asl_commit_argv(
                run_commit._CommitGeneration(
                    harness="codex",
                    model="gpt-5",
                    effort="high",
                    timeout=60,
                    message_file=Path("/tmp/paused-work.txt"),
                )
            ),
        )

    def test_the_per_session_message_is_the_one_it_renders(self) -> None:
        self.assertEqual(
            "chore: retain unverified work from session 3\n"
            "\n"
            "The run paused with reason `verify-failed` before this work was "
            "verified.\n"
            "Session 3 is back to `pending`; this commit records what the turn "
            "left behind so\n"
            "the next attempt starts from a clean worktree. Repair attempt 1 of "
            "2. Run:\n"
            "run-20260904-abc123.",
            self._message(),
        )

    def test_a_repair_child_reads_as_its_own_session(self) -> None:
        # A repair child owns its commit with `commit_mode is None`; its
        # staging is scoped like a per-session commit, so its subject is too.
        message = self._message(
            session_label=plans_payloads.session_label("3.1"), commit_mode=None
        )
        self.assertEqual(
            "chore: retain unverified work from session 3.1", message.splitlines()[0]
        )
        self.assertIn("Session 3.1 is back to `pending`", self._flat(message))

    def test_the_at_end_message_never_claims_the_commit_is_one_turns(self) -> None:
        # Legacy at-end staging is a repository-wide `git add -A`, so the
        # commit genuinely spans earlier sessions. Both the subject and the
        # sentence describing what was recorded have to say so.
        message = self._message(
            commit_mode=run_selection.COMMIT_MODE_END, pause_reason="no-status-change"
        )
        self.assertEqual(
            "chore: retain unverified work accumulated through session 3",
            message.splitlines()[0],
        )
        flat = self._flat(message)
        self.assertIn("spans everything the run has not committed yet", flat)
        self.assertIn("whatever earlier sessions left in the tree", flat)
        self.assertNotIn("what the turn left behind", flat)

    def test_every_message_names_its_pause_reason_and_its_run(self) -> None:
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS:
            for mode in (
                run_selection.COMMIT_MODE_PER_SESSION,
                run_selection.COMMIT_MODE_END,
                None,
            ):
                with self.subTest(reason=reason, commit_mode=mode):
                    flat = self._flat(
                        self._message(pause_reason=reason, commit_mode=mode)
                    )
                    self.assertIn(f"reason `{reason}`", flat)
                    self.assertIn("before this work was verified", flat)
                    self.assertIn("Run: run-20260904-abc123.", flat)
                    self.assertIn("is back to `pending`", flat)

    def test_only_the_verification_pause_numbers_a_repair_attempt(self) -> None:
        # The other three recoverable reasons relaunch the same session
        # unchanged, so the numbers are omitted even when a caller supplies
        # them -- the reason is what decides, not the arguments.
        self.assertIn("Repair attempt 1 of 2.", self._flat(self._message()))
        for reason in self.NO_BUDGET_REASONS:
            with self.subTest(reason=reason):
                self.assertNotIn(
                    "Repair attempt", self._flat(self._message(pause_reason=reason))
                )

    def test_an_unnumbered_verification_pause_says_nothing_about_attempts(self) -> None:
        self.assertNotIn(
            "Repair attempt",
            self._flat(self._message(repair_attempt=None, repair_max_attempts=None)),
        )

    def test_the_rendered_message_is_committable_as_written(self) -> None:
        # Asserted mechanically rather than by reading it: the subject must
        # satisfy the title pattern `asl commit` enforces, and the whole
        # message must already be in the form `format_commit_message` would
        # normalize it to, so what lands in the message file is what git keeps.
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS:
            for mode in (
                run_selection.COMMIT_MODE_PER_SESSION,
                run_selection.COMMIT_MODE_END,
                None,
            ):
                with self.subTest(reason=reason, commit_mode=mode):
                    message = self._message(pause_reason=reason, commit_mode=mode)
                    self.assertRegex(
                        message.splitlines()[0], commit_message.COMMIT_TITLE_PATTERN
                    )
                    self.assertEqual(
                        message, commit_message.format_commit_message(message)
                    )


class RecoveredCommitGenerationTests(unittest.TestCase):
    """How a group another run left owed gets its message the second time.

    A settlement can fail like any commit, and what recovers it is a *later*
    process that never rendered its message. Both reasons the settlement does
    not generate one outlive that first attempt, so the selection is the
    group's to make rather than the recovering run's: a ``session-paused``
    group is committed with the text it carries, and every other scope keeps
    the run's message harness.
    """

    #: The run's own generation, which is what recovery must keep for every
    #: scope but one -- and whose harness selection the paused scope keeps too,
    #: since ``--message-file`` only means the nested command never uses it.
    RUN_GENERATION = run_commit._CommitGeneration(
        harness="codex", model="gpt-5", effort="high", timeout=60
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "run"

    def _group(self, scope: str, **overrides: Any) -> dict[str, Any]:
        group = commit_groups.create_group(
            scope=scope,
            session_id=None if scope == commit_groups.GROUP_SCOPE_END else "3",
            targets=[
                commit_groups.create_target(
                    alias=None, root=Path(self._tmp.name), repository_path="."
                )
            ],
            initial_heads={commit_groups.LEGACY_REPOSITORY_KEY: "abc123"},
            verbatim_message=overrides.pop(
                "verbatim_message",
                "chore: retain unverified work from session 3\n\nMarked.",
            )
            if scope == commit_groups.GROUP_SCOPE_SESSION_PAUSED
            else None,
        )
        group.update(overrides)
        return group

    def _select(self, group: Mapping[str, Any]) -> run_commit._CommitGeneration:
        return commit_settlement._recovered_commit_generation(
            group, generation=self.RUN_GENERATION, run_dir=self.run_dir
        )

    def test_a_paused_group_is_recovered_through_its_own_message(self) -> None:
        group = self._group(commit_groups.GROUP_SCOPE_SESSION_PAUSED)
        generation = self._select(group)
        assert generation.message_file is not None
        self.assertEqual(
            group["verbatim_message"],
            generation.message_file.read_text(encoding="utf-8"),
        )
        # The file is written under the recovering run's directory: the
        # original lives beside a turn in a run this process may never have
        # seen, and the group id is what keeps two recovered groups apart.
        self.assertEqual(
            self.run_dir
            / run_commit_paused.RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME
            / group["group_id"]
            / run_commit_paused.PAUSED_WORK_MESSAGE_FILE_NAME,
            generation.message_file,
        )
        # Everything else about the run's selection survives, so the argv the
        # recovery builds is the run's plus the one flag pair.
        self.assertEqual(
            self.RUN_GENERATION,
            dataclasses.replace(generation, message_file=None),
        )
        self.assertIn("--message-file", run_commit._asl_commit_argv(generation))

    def test_the_accepted_and_end_scopes_still_generate_their_messages(self) -> None:
        for scope in (
            commit_groups.GROUP_SCOPE_SESSION,
            commit_groups.GROUP_SCOPE_END,
        ):
            with self.subTest(scope=scope):
                generation = self._select(self._group(scope))
                self.assertIs(self.RUN_GENERATION, generation)
                self.assertNotIn(
                    "--message-file", run_commit._asl_commit_argv(generation)
                )

    def test_a_paused_group_with_no_message_refuses_rather_than_generates(self) -> None:
        # Falling back to the run's harness here would be the defect in a
        # narrower form: the one scope that must never ask a model for its
        # message would ask one whenever its durable record was incomplete.
        group = self._group(commit_groups.GROUP_SCOPE_SESSION_PAUSED)
        for missing in (None, "", "   ", 17):
            with (
                self.subTest(verbatim_message=missing),
                self.assertRaises(runner.RunError),
            ):
                self._select({**group, "verbatim_message": missing})
        # And a group written before the field existed, which carries no key.
        without_key = {
            key: value for key, value in group.items() if key != "verbatim_message"
        }
        with self.assertRaises(runner.RunError):
            self._select(without_key)

    def test_the_message_a_group_carries_is_outside_its_identity(self) -> None:
        # Recovery finds the group by the id its own failure recorded, so the
        # text the group commits must not be able to change that id.
        carried = self._group(commit_groups.GROUP_SCOPE_SESSION_PAUSED)
        reworded = self._group(
            commit_groups.GROUP_SCOPE_SESSION_PAUSED, verbatim_message="chore: other\n"
        )
        self.assertNotEqual(carried["verbatim_message"], reworded["verbatim_message"])
        self.assertEqual(carried["group_id"], reworded["group_id"])
        self.assertEqual(carried["identity"], reworded["identity"])


if __name__ == "__main__":
    unittest.main()
