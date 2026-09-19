"""One canonical commit path publishes an accepted turn exactly once.

Covers the seam where a turn stops being an execution record and becomes an
accepted, immutable statement: which function may emit ``turn_finished``, what
must already have happened when it does, and what the event stream says instead
when a post-execution guard refuses the turn.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import inspect
import io
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_groups,
    commit_message,
    commit_settlement,
    events,
    harnesses,
    pause_policy,
    plans,
    review_guard,
    review_repair,
    run_commit,
    run_loop,
    run_persistence,
    run_reporting,
    run_selection,
    runner,
    session_verification,
    state,
    supervisor_audit,
    turn_execution,
    turn_postflight,
    turn_preflight,
    turn_preparation,
    turn_settlement,
    verification,
)
from agent_session_loop import verification as verification_module
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    GENERATION_TIMEOUT_PROBE_SECONDS,
    REPO_ROOT,
    _contract_block,
    assert_no_runner_import,
    fake_commit_harness_env,
    init_git_repo,
    init_multi_repo_workspace,
    read_jsonl,
    wait_for_process_exit,
    write_multi_repo_plan,
)

SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

#: The symbols `run_commit` took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_LiveTurn",
    "_commit_paths",
    "_build_invocation_payload",
    "_resolved_model_record",
    "_merged_model_projection",
    "_commit_status_result",
    "_write_commit_artifacts",
    "_CommitGeneration",
    "_commit_generation",
    "_asl_commit_argv",
    "_asl_commit_env",
    "_snapshot_commit_descendants",
    "_sweep_commit_descendants",
    "_run_bounded_asl_commit",
    "_run_asl_commit",
    "_commit_started_payload",
    "COMMIT_SUBPROCESS_GRACE_SECONDS",
    "_COMMIT_DRAIN_SECONDS",
    "CHECKOUT_ROOT",
    "COMMIT_SUBCOMMAND_ARGV",
)

#: The three files a commit leaves behind, and the only paths this boundary is
#: allowed to write. Everything else it produces is a payload it returns for
#: the loop to append under the run's own lock.
COMMIT_ARTIFACT_KEYS = ("stdout", "stderr", "result")

#: Every module the turn path has been sliced into. The commit-ordering
#: contract below is an ordering one, and raw source offsets stop being
#: comparable the moment two ordered steps live in different files -- so each
#: guard is located by search and ordered by where the loop reaches its owner,
#: which keeps the contract intact as the extraction keeps carving the loop up.
TURN_PATH_MODULES = (
    run_loop,
    turn_preparation,
    turn_preflight,
    turn_postflight,
    turn_settlement,
)


def _module_tree(module: Any) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _call_lines(node: ast.AST, name: str) -> list[int]:
    """Lines in ``node`` that call ``name``, bare or through its module.

    A call site keeps its meaning when the callee is extracted into a package
    module, so `name(...)` and `module.name(...)` both count -- otherwise an
    ordering contract silently stops seeing a step the moment it moves.
    """
    return sorted(
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id == name)
            or (isinstance(call.func, ast.Attribute) and call.func.attr == name)
        )
    )


def _turn_finished_emitters() -> set[str]:
    """Names of the functions that mention the turn_finished event.

    Both the run loop and the turn-execution boundary are scanned: the emitter
    moved behind the boundary, and the invariant is that it stayed singular
    across the split rather than gaining a second copy in the run loop.
    """
    emitters: set[str] = set()
    for scanned in (run_loop, turn_execution):
        source = Path(inspect.getsourcefile(scanned) or "").read_text("utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(node):
                names_event = (
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "TURN_FINISHED_EVENT"
                ) or (isinstance(inner, ast.Name) and inner.id == "TURN_FINISHED_EVENT")
                names_literal = (
                    isinstance(inner, ast.Constant) and inner.value == "turn_finished"
                )
                if names_event or names_literal:
                    emitters.add(node.name)
                    break
    return emitters


def _filesystem_writers(source: str) -> dict[str, set[str]]:
    """Map each function in ``source`` to the write calls it makes.

    A write is either a reach into :mod:`agent_session_loop.durability` -- the
    atomic writers this boundary is allowed to use -- or one of the raw
    filesystem verbs a module could reach for instead. Both are collected so
    the assertion can say *which* functions write and *how*, rather than only
    counting.
    """
    raw_verbs = {"write_text", "write_bytes", "mkdir", "unlink", "touch", "rename"}
    writers: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Name) and func.id == "open":
                calls.add("open")
            elif isinstance(func, ast.Attribute) and (
                func.attr in raw_verbs or ast.unparse(func).startswith("durability.")
            ):
                calls.add(ast.unparse(func))
        if calls:
            writers[node.name] = calls
    return writers


class RunCommitBoundaryTests(unittest.TestCase):
    """``run_commit`` is runner-free and writes only the commit artifacts."""

    def setUp(self) -> None:
        self.source = Path(run_commit.__file__ or "").read_text(encoding="utf-8")

    def test_run_commit_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_commit)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(run_commit, name))

    def test_only_the_artifact_writers_touch_the_filesystem(self) -> None:
        # The subprocess this module runs can take minutes, so the run's write
        # mutex is deliberately not held across it: the commit-started and
        # commit-finished payloads are returned for the loop to persist. The
        # exceptions are the commit's own artifact directory and the message
        # file a retained-work commit is made from -- the nested command's own
        # input, written where its argv is built rather than in the ledger that
        # asked for it, which owns no file writes at all. Both go through the
        # atomic writers rather than through `Path.write_text`.
        self.assertEqual(
            {
                "_write_commit_artifacts": {
                    "durability.write_text",
                    "durability.write_json_file",
                },
                "_write_paused_work_message": {"durability.write_text"},
            },
            _filesystem_writers(self.source),
        )

    def test_the_artifact_layout_is_the_one_the_writer_writes(self) -> None:
        paths = run_commit._commit_paths(
            Path("/run"), commit_index=7, session_id="3", repository_alias="app"
        )
        self.assertEqual(
            {"artifact_dir", *COMMIT_ARTIFACT_KEYS}, set(paths), msg=str(paths)
        )
        self.assertEqual("commit-0007-session-3-app", paths["artifact_dir"].name)
        for key in COMMIT_ARTIFACT_KEYS:
            with self.subTest(key=key):
                self.assertEqual(paths["artifact_dir"], paths[key].parent)

    def test_the_checkout_root_resolves_to_this_repository(self) -> None:
        # `Path(__file__).resolve().parent.parent` only answers the checkout
        # while this module sits directly under the package directory. A module
        # one directory deeper would resolve one level short and silently
        # commit with whatever `asl` another install put on PYTHONPATH.
        self.assertEqual(REPO_ROOT, run_commit.CHECKOUT_ROOT)
        self.assertEqual(
            Path(run_commit.__file__ or "").parent.name, "agent_session_loop"
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
            "session_label": plans.session_label("3"),
            "pause_reason": "verify-failed",
            "commit_mode": run_selection.COMMIT_MODE_PER_SESSION,
            "repair_attempt": 1,
            "repair_max_attempts": 2,
            "run_id": "run-20260904-abc123",
        }
        kwargs.update(overrides)
        return run_commit._paused_work_commit_message(**kwargs)

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
            session_label=plans.session_label("3.1"), commit_mode=None
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
            / run_commit.RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME
            / group["group_id"]
            / run_commit.PAUSED_WORK_MESSAGE_FILE_NAME,
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


class PausedRetainedWorkTests(unittest.TestCase):
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
            session_label=plans.session_label("1"),
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
        return self.artifact_dir / run_commit.PAUSED_WORK_MESSAGE_FILE_NAME

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

    # -- the four results -------------------------------------------------

    def test_a_registered_settlement_stages_the_guarded_paths_only(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(["SVC"], [call["repository_key"] for call in self.launched])
        # The staging is the write guard's evidence and nothing else: the
        # settlement reuses the accepted path's resolver rather than sweeping
        # the repository the way an end-of-run commit would.
        self.assertEqual(["SVC:worked.txt"], self.launched[0]["staging_paths"])
        self.assertEqual(
            self.repositories["SVC"].resolve(), Path(self.launched[0]["cwd"])
        )
        self.assertEqual([], self._dirty(self.repositories["SVC"]))
        self.assertEqual(record, turn_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY])
        self.assertEqual(
            commit_groups.GROUP_SCOPE_SESSION_PAUSED, record["commit_group"]["scope"]
        )
        self.assertEqual({"repositories": []}, record["residue"])
        self.assertIsNone(record["error"])

    def test_the_legacy_settlement_covers_its_one_repository(self) -> None:
        self._write(self.legacy_root, "worked.txt")
        self._write(self.legacy_root, "also.txt")
        record, _turn_payload = self._settle(self.legacy, guard=None)

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            [commit_groups.LEGACY_REPOSITORY_KEY],
            [call["repository_key"] for call in self.launched],
        )
        # No pathspec means `asl commit` stays on its `git add -A` default, so
        # the file the guard never named is committed too.
        self.assertEqual([], self.launched[0]["staging_paths"])
        self.assertEqual([], self._dirty(self.legacy_root))

    def test_the_at_end_settlement_covers_the_whole_workspace(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=run_selection.COMMIT_MODE_END,
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        # The guard authorized SVC alone; the at-end sweep commits every dirty
        # repository regardless, because that is what the mode already claims.
        self.assertEqual(
            ["SVC", "OAK"], [call["repository_key"] for call in self.launched]
        )
        self.assertEqual([[], []], [call["staging_paths"] for call in self.launched])
        self.assertEqual([], self._dirty(self.repositories["OAK"]))

    def test_nothing_to_commit_records_no_changes_without_a_commit(self) -> None:
        record, _turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        self.assertEqual("no-changes", record["result"])
        self.assertEqual([], self.launched)
        self.assertIsNone(record["commit_group"])
        self.assertIsNone(record["commit"])
        self.assertIsNone(record["error"])
        # No group was created and no message was rendered for a commit that
        # never had a reason to run.
        self.assertNotIn("commit_groups", self.payload)
        self.assertFalse(self._message_file().exists())
        self.assertEqual([], self.soft_failures)

    def test_dirt_that_survives_the_commit_is_named_in_the_record(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["SVC"], "stray.txt")
        record, _turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        self.assertEqual("committed-with-residue", record["result"])
        self.assertEqual(
            {
                "repositories": [
                    {
                        "alias": "SVC",
                        "clean": False,
                        "dirty_paths": ["SVC:stray.txt"],
                        "error": None,
                    }
                ]
            },
            record["residue"],
        )
        # The commit is real; what it left behind is what the relaunch's
        # startup gate will still refuse, which is why it is reported.
        self.assertIsNone(record["error"])
        self.assertEqual(["stray.txt"], self._dirty(self.repositories["SVC"]))

    def test_a_failed_settlement_soft_fails_and_leaves_the_group_owed(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit=self._refused_commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual(1, record["commit"]["returncode"])
        self.assertEqual(
            commit_groups.GROUP_STATUS_FAILED, record["commit_group"]["status"]
        )
        # The durable row is what a later run recovers, and the run's own
        # end-of-run report names it as a loss.
        self.assertEqual(
            [commit_groups.GROUP_STATUS_FAILED],
            [group["status"] for group in self.payload["commit_groups"]],
        )
        self.assertEqual(
            [commit_groups.GROUP_SCOPE_SESSION_PAUSED],
            [
                summary["scope"]
                for summary in commit_settlement._unresolved_commit_groups(self.payload)
            ],
        )
        self.assertEqual(1, len(self.soft_failures))
        failure = self.soft_failures[0]
        self.assertEqual(commit_groups.GROUP_SCOPE_SESSION_PAUSED, failure["scope"])
        self.assertEqual("1", failure["session_id"])
        self.assertEqual(record["commit_group"], failure["group"])
        self.assertEqual(record["commit"], failure["commit"])
        # A commit that exited nonzero moved no HEAD, so nothing reached the
        # history and the sentence says exactly that. The landed-commit wording
        # is the other branch's, and it must not leak into this one.
        self.assertEqual(
            "the work retained by the verify-failed pause of session 1 "
            "could not be committed",
            failure["message"],
        )

    def test_a_refused_commit_names_the_retained_residue(self) -> None:
        # A commit that exits nonzero took nothing, so everything the turn
        # retained is still in the tree -- and those are exactly the paths the
        # relaunch's startup gate refuses over. Until the probe became
        # unconditional the record reported an empty residue here, which reads
        # as a clean tree the run never confirmed.
        self._write(self.repositories["SVC"], "worked.txt")
        head_before = self._head(self.repositories["SVC"])
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit=self._refused_commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual("nonzero-exit", record["commit"]["status_result"])
        # The ordinary refusal branch: HEAD never moved, so this is not the
        # commit-landed-on-a-dirty-tree case that was already probed.
        self.assertEqual(head_before, self._head(self.repositories["SVC"]))
        self.assertEqual(
            [
                {
                    "alias": "SVC",
                    "clean": False,
                    "dirty_paths": ["SVC:worked.txt"],
                    "error": None,
                }
            ],
            record["residue"]["repositories"],
        )
        self.assertEqual(["worked.txt"], self._dirty(self.repositories["SVC"]))

    def test_a_partial_failure_names_only_the_repository_left_dirty(self) -> None:
        # The group's verdict is one word for the whole set, and on a partial
        # failure that word is wrong about the repositories that committed.
        # The residue is per target, so it separates them.
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")

        def commit(**kwargs: Any) -> dict[str, Any]:
            if kwargs["repository_key"] == "OAK":
                return self._refused_commit(**kwargs)
            return self._fake_commit(**kwargs)

        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=run_selection.COMMIT_MODE_END,
            commit=commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual("OAK", record["commit_group"]["failed_alias"])
        self.assertEqual(
            [
                {
                    "alias": "OAK",
                    "clean": False,
                    "dirty_paths": ["OAK:elsewhere.txt"],
                    "error": None,
                }
            ],
            record["residue"]["repositories"],
        )
        # SVC committed before OAK refused, and the record does not accuse it
        # of residue it does not have.
        self.assertEqual([], self._dirty(self.repositories["SVC"]))
        self.assertEqual(["elsewhere.txt"], self._dirty(self.repositories["OAK"]))

    def test_a_partial_failure_message_reports_both_committed_and_retained_work(
        self,
    ) -> None:
        # The failing commit landed nothing, but the group had already
        # committed SVC before OAK refused. The all-or-nothing sentence sent
        # the operator looking for work that is already in `git log`, so the
        # message now names what committed and what did not.
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")

        def commit(**kwargs: Any) -> dict[str, Any]:
            if kwargs["repository_key"] == "OAK":
                return self._refused_commit(**kwargs)
            return self._fake_commit(**kwargs)

        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=run_selection.COMMIT_MODE_END,
            commit=commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        # The commit the message reads landed nothing; the correction comes
        # from the group, not from a second reading of the commit.
        self.assertEqual("nonzero-exit", record["commit"]["status_result"])
        committed = record["commit_group"]["successful_repositories"]
        self.assertEqual(["SVC"], [row["alias"] for row in committed])
        self.assertEqual(1, len(self.soft_failures))
        self.assertEqual(
            "the verify-failed pause of session 1 committed the work it "
            "retained in SVC, but the work it retained in OAK could not be "
            "committed; the worktree still holds OAK:elsewhere.txt",
            self.soft_failures[0]["message"],
        )

    def test_a_partial_failure_message_survives_an_unreadable_residue_probe(
        self,
    ) -> None:
        # With no residue path to name, the sentence still must not deny SVC's
        # commit -- the correction lives on the group, and the probe's silence
        # only drops the trailing clause.
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")
        real_run_git = review_guard._run_git

        def commit(**kwargs: Any) -> dict[str, Any]:
            if kwargs["repository_key"] == "OAK":
                return self._refused_commit(**kwargs)
            return self._fake_commit(**kwargs)

        def blinded(cwd: Path, args: list[str]) -> Any:
            if len(self.launched) == 2 and args[:1] == ["status"]:
                raise runner.RunError("could not inspect git status: fatal: unreadable")
            return real_run_git(cwd, args)

        with mock.patch.object(review_guard, "_run_git", side_effect=blinded):
            record, _turn_payload = self._settle(
                self.registered,
                guard=self._guard(added_paths=("worked.txt",)),
                commit_mode=run_selection.COMMIT_MODE_END,
                commit=commit,
            )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual(
            [None, None],
            [row["clean"] for row in record["residue"]["repositories"]],
        )
        self.assertEqual(
            "the verify-failed pause of session 1 committed the work it "
            "retained in SVC, but the work it retained in OAK could not be "
            "committed",
            self.soft_failures[0]["message"],
        )

    def test_a_target_whose_status_is_unreadable_records_undetermined(self) -> None:
        # The commit may have failed *because* the repository became
        # unreadable, and the settlement is on its way to a pause it must not
        # replace with a crash. So the probe answers `clean: None` with the
        # failure text rather than raising or reading the tree as clean.
        self._write(self.repositories["SVC"], "worked.txt")
        real_run_git = review_guard._run_git

        def blinded(cwd: Path, args: list[str]) -> Any:
            # Only after the commit ran: the target resolution and the group's
            # own skip-clean check need real answers to reach the commit at all.
            if self.launched and args[:1] == ["status"]:
                raise runner.RunError("could not inspect git status: fatal: unreadable")
            return real_run_git(cwd, args)

        with mock.patch.object(review_guard, "_run_git", side_effect=blinded):
            record, _turn_payload = self._settle(
                self.registered,
                guard=self._guard(added_paths=("worked.txt",)),
                commit=self._refused_commit,
            )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual(
            [
                {
                    "alias": "SVC",
                    "clean": None,
                    "dirty_paths": [],
                    "error": "could not inspect git status: fatal: unreadable",
                }
            ],
            record["residue"]["repositories"],
        )
        # And the settlement still recorded, published and soft-failed.
        self.assertEqual(1, len(self._events()))
        self.assertEqual(1, len(self.soft_failures))

    def test_a_guard_that_did_not_pass_fails_with_no_group_to_retry(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",), passed=False),
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertIn("workspace write guard did not pass", record["error"])
        self.assertEqual([], self.launched)
        # Session 1's gap 1: the resolver raises before `create_group`, so the
        # failure leaves no durable row and nothing for the loop to re-arm.
        self.assertIsNone(record["commit_group"])
        self.assertNotIn("commit_groups", self.payload)
        self.assertEqual(1, len(self.soft_failures))
        self.assertIsNone(self.soft_failures[0]["group"])
        self.assertIsNone(self.soft_failures[0]["commit"])
        # The run still says it lost a commit, from the failure record alone.
        self.assertEqual(["worked.txt"], self._dirty(self.repositories["SVC"]))

    # -- what the record and the message carry ----------------------------

    def test_the_message_file_is_the_deterministic_marking(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        self._settle(self.registered, guard=self._guard(added_paths=("worked.txt",)))

        expected = run_commit._paused_work_commit_message(
            session_label="session 1",
            pause_reason="verify-failed",
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            repair_attempt=1,
            repair_max_attempts=2,
            run_id="run-paused",
        )
        self.assertEqual(
            self._message_file(), self.launched[0]["generation"].message_file
        )
        self.assertEqual(expected, self._message_file().read_text(encoding="utf-8"))
        # The message the settlement wrote is the message git kept.
        committed = review_guard._run_git(
            self.repositories["SVC"], ["log", "-1", "--pretty=%B"]
        ).stdout
        self.assertEqual(expected.strip(), committed.strip())

    def test_the_event_carries_the_record_the_turn_payload_keeps(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        published = self._events()
        self.assertEqual(1, len(published))
        self.assertEqual(
            {"run_id": "run-paused", "session_id": "1", **record}, published[0]
        )
        self.assertEqual(
            commit_settlement.PAUSED_WORK_COMMIT_GUARD,
            turn_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]["guard"],
        )

    def test_the_repair_numbers_are_read_from_the_pause_details(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            pause_details={
                "verification_repair_attempt": 2,
                "verification_repair_max_attempts": 2,
                "verification_repair_exhausted": True,
            },
        )

        assert record is not None
        self.assertEqual(2, record["verification_repair_attempt"])
        self.assertEqual(2, record["verification_repair_max_attempts"])
        self.assertIs(True, record["verification_repair_exhausted"])
        # Settling an exhausted repair is deliberate: the work stays
        # inspectable through `git show` and the flag says the budget is spent.
        self.assertEqual("committed", record["result"])

    def test_details_that_carry_no_numbers_invent_none(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            pause_reason="timeout",
            pause_details={},
        )

        assert record is not None
        self.assertIsNone(record["verification_repair_attempt"])
        self.assertIsNone(record["verification_repair_max_attempts"])
        self.assertIsNone(record["verification_repair_exhausted"])
        self.assertEqual("timeout", record["pause_reason"])
        # And the message says nothing about an attempt it was never told.
        self.assertNotIn("Repair attempt", self._message_file().read_text("utf-8"))

    # -- which pauses owe a commit at all ---------------------------------

    def test_a_pause_that_owes_no_commit_settles_nothing(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        guard = self._guard(added_paths=("worked.txt",))
        for label, kwargs in (
            ("review session", {"is_review_session": True}),
            ("no commit mode", {"commit_mode": None}),
            ("non-recoverable pause", {"pause_reason": "commit-preflight"}),
        ):
            with self.subTest(case=label):
                record, turn_payload = self._settle(
                    self.registered, guard=guard, **kwargs
                )
                self.assertIsNone(record)
                self.assertNotIn(commit_settlement.PAUSED_WORK_COMMIT_KEY, turn_payload)
        self.assertEqual([], self.launched)
        self.assertEqual([], self._events())

    def test_a_repair_child_owns_its_own_retained_work_commit(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=None,
            is_repair_session=True,
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertIsNone(record["commit_mode"])


class TurnCommitSourceTests(unittest.TestCase):
    def test_only_the_commit_function_names_the_turn_finished_event(self) -> None:
        self.assertEqual({"commit_turn"}, _turn_finished_emitters())

    def test_commit_persists_the_result_and_state_before_the_event(self) -> None:
        source = inspect.getsource(turn_execution.commit_turn)
        written = source.index('durability.write_json_file(paths["result"]')
        state_written = source.index("persistence.write_state(payload)")
        appended = source.index("TURN_FINISHED_EVENT")
        self.assertLess(written, state_written)
        self.assertLess(state_written, appended)

    def test_commit_notifies_consumers_after_the_durable_event(self) -> None:
        source = inspect.getsource(turn_execution.commit_turn)
        appended = source.index("TURN_FINISHED_EVENT")
        reported = source.index("progress.turn_finished(")
        notified = source.index("notify_turn_finished(")
        self.assertLess(appended, reported)
        self.assertLess(appended, notified)


class RunLoopGuardOrderTests(unittest.TestCase):
    """Nothing is committed before every guard that could refuse it has run.

    The guards no longer all live in ``run_locked``: the post-execution
    sequence moved into ``turn_postflight``, and the turn-start captures into
    ``turn_preflight``. So each guard is ordered by the pair of the line in
    ``run_locked`` where the loop reaches its owner and the line inside that
    owner, with a guard the loop still performs itself sorting as ``(its own
    line, -1)``. Owners are discovered rather than listed, so a guard that moves
    into a new turn-path module rejoins this contract by being called from the
    loop.
    """

    def setUp(self) -> None:
        self.trees = {
            module.__name__.rsplit(".", 1)[-1]: _module_tree(module)
            for module in TURN_PATH_MODULES
        }
        self.run_locked = self._function("run_locked", self.trees["run_loop"])
        self.entry_points = self._entry_points()

    def _function(self, name: str, tree: ast.AST) -> ast.FunctionDef:
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ]
        self.assertEqual(1, len(matches), f"expected one {name} definition")
        return matches[0]

    def _entry_points(self) -> dict[str, tuple[int, ast.FunctionDef]]:
        reached: dict[str, tuple[int, ast.FunctionDef]] = {}
        for module_name, tree in self.trees.items():
            if module_name == "run_loop":
                continue
            for node in ast.iter_child_nodes(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                calls = _call_lines(self.run_locked, node.name)
                if calls:
                    reached[node.name] = (min(calls), node)
        return reached

    def _positions(self, name: str) -> list[tuple[int, int]]:
        positions = [(line, -1) for line in _call_lines(self.run_locked, name)]
        for loop_line, owner in self.entry_points.values():
            positions.extend((loop_line, line) for line in _call_lines(owner, name))
        return sorted(positions)

    def test_the_run_loop_commits_after_every_guard(self) -> None:
        commit = self._positions("commit")
        self.assertTrue(commit, "expected the loop to commit a turn")
        for earlier in (
            "_enforce_workspace_write_guard",
            "_enforce_review_mutation_guard",
            "_enforce_mailbox_completion_guard",
            "_enforce_session_verification",
            "_append_progress_entry",
            "sync_derived_status_ledgers",
        ):
            with self.subTest(guard=earlier):
                reached = self._positions(earlier)
                self.assertTrue(reached, f"the turn path never reaches {earlier}")
                # First reach, as the string-offset form this replaced read it:
                # the crash handler's own copy of a write guard sits after the
                # commit by construction and is not what this orders.
                self.assertLess(min(reached), min(commit), earlier)


class CommittedTurnImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        write_multi_repo_plan(self.plan_dir, ("complete",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def _commit(self, turn_payload: dict[str, Any]) -> Any:
        payload: dict[str, Any] = {"run_id": "run-1", "turns": [turn_payload]}
        commit = turn_execution.commit_turn(
            event_path=self.tmp_path / "events.jsonl",
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            progress=run_reporting._ProgressReporter(enabled=False),
            turn_payload=turn_payload,
            live_turn=dict(turn_payload),
            accepted=self.snapshot,
            plan_cache=plans.PlanSnapshotCache(),
            paths={"result": self.tmp_path / "result.json"},
        )
        return commit.record, payload

    def test_commit_returns_the_published_snapshot_it_recorded(self) -> None:
        turn_payload = {"turn_index": 1, "session": "1"}
        payload: dict[str, Any] = {"run_id": "run-1", "turns": [turn_payload]}
        commit = turn_execution.commit_turn(
            event_path=self.tmp_path / "events.jsonl",
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            progress=run_reporting._ProgressReporter(enabled=False),
            turn_payload=turn_payload,
            live_turn=dict(turn_payload),
            accepted=self.snapshot,
            plan_cache=plans.PlanSnapshotCache(),
            paths={"result": self.tmp_path / "result.json"},
        )
        self.assertIsInstance(commit, turn_execution.TurnCommit)
        self.assertEqual(
            plans.snapshot_payload(commit.published), commit.record["after"]
        )

    def test_committed_turn_cannot_be_rewritten(self) -> None:
        committed, _payload = self._commit({"turn_index": 1, "session": "1"})
        self.assertIsInstance(committed, MappingProxyType)
        with self.assertRaises(TypeError):
            committed["session"] = "2"  # type: ignore[index]

    def test_committed_turn_replaces_the_in_flight_state_record(self) -> None:
        turn_payload = {"turn_index": 1, "session": "1"}
        committed, payload = self._commit(turn_payload)
        self.assertEqual(1, len(payload["turns"]))
        self.assertTrue(payload["turns"][0]["committed"])
        self.assertNotIn("committed", turn_payload)
        self.assertEqual(dict(committed), payload["turns"][0])

    def test_committed_turn_carries_the_accepted_snapshot(self) -> None:
        committed, _payload = self._commit(
            {"turn_index": 1, "session": "1", "after": {"stale": True}}
        )
        self.assertEqual(plans.snapshot_payload(self.snapshot), committed["after"])


class TurnCommitRunTests(unittest.TestCase):
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
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )
        # Real runs carry readiness metadata, so every turn rewrites manifest.md.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
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
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
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
            state.release_lock(self.plan_dir, self.state_dir)

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
                if line.startswith(plans.STATUS_PREFIX):
                    lines[index] = f"{plans.STATUS_PREFIX} complete\n"
                elif line.startswith(plans.HANDOFF_FILES_PREFIX):
                    lines[index] = f"{plans.HANDOFF_FILES_PREFIX} {declares}\n"
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

    def test_acceptance_follows_execution_and_every_guard(self) -> None:
        self._run_accepted()
        names = self._names()
        self.assertEqual(1, names.count(events.TURN_FINISHED_EVENT))
        accepted = names.index(events.TURN_FINISHED_EVENT)
        for earlier in (
            events.TURN_EXECUTION_FINISHED_EVENT,
            events.VERIFICATION_STARTED_EVENT,
            events.VERIFICATION_FINISHED_EVENT,
        ):
            self.assertLess(names.index(earlier), accepted, earlier)
        self.assertLess(accepted, names.index("run_finished"))
        self.assertEqual([], self._payloads(events.TURN_REJECTED_EVENT))

    def test_event_result_and_state_describe_the_same_committed_turn(self) -> None:
        payload = self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertTrue(finished["committed"])
        result_path = Path(str(finished["result_path"]))
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), finished)
        self.assertEqual([finished], payload["turns"])
        state_payload = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(
            finished["turn_index"], state_payload["turns"][0]["turn_index"]
        )
        self.assertTrue(state_payload["turns"][0]["committed"])

    def test_committed_turn_reports_the_guarded_snapshot(self) -> None:
        self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertEqual(
            plans.snapshot_payload(plans.inspect_plan(self.plan_dir)),
            finished["after"],
        )

    def test_authored_session_remains_uncommitted_under_null_mode(self) -> None:
        payload = self._run_accepted()

        self.assertIsNone(payload["commit_mode"])
        self.assertEqual([], payload["commits"])
        self.assertIn(
            "README.md",
            review_guard._run_git(
                self.repositories["SVC"], ["status", "--porcelain"]
            ).stdout,
        )

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

    def test_registered_repair_commits_only_changed_writable_aliases(self) -> None:
        self._mark_repair_session()
        (self.repositories["OAK"] / "preexisting.txt").write_text(
            "read-only dirty state\n", encoding="utf-8"
        )
        oak_head = review_guard._read_git_head(self.repositories["OAK"])["head"]
        commit_log = self.tmp_path / "commit-turns.jsonl"
        env = fake_commit_harness_env(self.tmp_path, log_path=commit_log)

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:repair.txt", declares="SVC:repair.txt"
                ),
            ),
        ):
            exit_code, payload = self._invoke("registered-repair-commit")

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload["commit_mode"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertEqual("SVC", payload["commits"][0]["repository_alias"])
        # Only the writable alias ran a message-generation turn, and it ran
        # with that repository as its cwd.
        self.assertEqual(
            [str(self.repositories["SVC"].resolve())],
            [record["cwd"] for record in read_jsonl(commit_log)],
        )
        self.assertEqual(
            "",
            review_guard._run_git(
                self.repositories["SVC"], ["status", "--porcelain"]
            ).stdout.strip(),
        )
        self.assertEqual(
            oak_head,
            review_guard._read_git_head(self.repositories["OAK"])["head"],
        )
        self.assertTrue(
            review_guard._run_git(
                self.repositories["OAK"], ["status", "--porcelain"]
            ).stdout.strip()
        )

    def test_pathspec_commit_carries_only_the_paths_it_names(self) -> None:
        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("somebody else's edit\n", encoding="utf-8")
        env = fake_commit_harness_env(self.tmp_path)

        with mock.patch.dict(os.environ, env):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "pathspec-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=run_commit._CommitGeneration(harness="codex"),
            )

        self.assertEqual(0, commit["returncode"])
        self.assertEqual(["--pathspec", "declared.txt"], commit["argv"][-2:])
        self.assertEqual(
            ["declared.txt"],
            review_guard._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        # A pathspec commit only promises the paths it names, so the file the
        # turn never touched survives the commit as it was — which is also why
        # the probe reports `dirty-after-commit` rather than success.
        self.assertEqual(
            "?? unrelated.txt",
            review_guard._run_git(repo, ["status", "--porcelain"]).stdout.strip(),
        )
        self.assertEqual("dirty-after-commit", commit["status_result"])
        self.assertFalse(commit["success"])

    def test_pathspec_commit_leaves_a_preexisting_index_entry_staged(self) -> None:
        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("somebody else's edit\n", encoding="utf-8")
        # Somebody staged their own work before the run's commit boundary.
        review_guard._run_git(repo, ["add", "--", "unrelated.txt"])
        env = fake_commit_harness_env(self.tmp_path)

        with mock.patch.dict(os.environ, env):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "prestaged-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=run_commit._CommitGeneration(harness="codex"),
            )

        self.assertEqual(0, commit["returncode"])
        self.assertEqual(
            ["declared.txt"],
            review_guard._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        # The pre-staged path was neither committed nor unstaged by the scoped
        # commit; it is still staged, now against the new HEAD.
        self.assertEqual(
            "A  unrelated.txt",
            review_guard._run_git(repo, ["status", "--porcelain"]).stdout.strip(),
        )

    def test_runner_commit_generation_is_bounded(self) -> None:
        # Every run-owned commit carries a bound, including the default
        # selection a run that named no harness produces: an unbounded nested
        # commit is what turns one stalled message harness into a stalled loop.
        default_argv = run_commit._asl_commit_argv(run_commit._CommitGeneration())
        self.assertIn("--timeout", default_argv)
        self.assertEqual(
            str(commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS),
            default_argv[default_argv.index("--timeout") + 1],
        )
        # The run's own `--timeout` bounds a session turn and is routinely
        # unset, so it must not be what the commit bound depends on.
        unset = argparse.Namespace(harness="codex", timeout=None, model=None)
        self.assertEqual(
            commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS,
            run_commit._commit_generation(unset).timeout,
        )

        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        pid_path = self.tmp_path / "hang-child.pid"
        env = fake_commit_harness_env(self.tmp_path)
        env["FAKE_CODEX_COMMIT_MODE"] = "hang"
        env["FAKE_CODEX_COMMIT_HANG_CHILD_PID"] = str(pid_path)

        started = time.monotonic()
        with mock.patch.dict(os.environ, env):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "bounded-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=run_commit._CommitGeneration(
                    harness="codex", timeout=GENERATION_TIMEOUT_PROBE_SECONDS
                ),
            )
        elapsed = time.monotonic() - started

        # The nested command enforced its own bound and exited with a
        # classified failure, so the run never reached its outer backstop.
        self.assertLess(elapsed, 120, "the nested commit outlived its bound")
        argv = list(commit["argv"])
        self.assertEqual(
            str(GENERATION_TIMEOUT_PROBE_SECONDS),
            argv[argv.index("--timeout") + 1],
        )
        self.assertEqual(
            GENERATION_TIMEOUT_PROBE_SECONDS, commit["generation_timeout_seconds"]
        )
        self.assertFalse(commit["timed_out"])
        self.assertNotEqual(0, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])
        stderr = Path(str(commit["stderr_path"])).read_text(encoding="utf-8")
        self.assertIn(
            "did not produce a commit message within "
            f"{GENERATION_TIMEOUT_PROBE_SECONDS}s",
            stderr,
        )
        # Nothing was committed, and the stalled harness's whole process tree
        # went down with the bound rather than outliving the run.
        self.assertEqual(commit["head_before"], commit["head_after"])
        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a harness descendant outlived the nested commit bound",
        )

    def test_a_wedged_commit_subprocess_is_killed_by_the_outer_backstop(self) -> None:
        # The nested bound cannot cover a commit process that never reaches its
        # own timeout logic, so the run bounds the subprocess as well. The stub
        # stands in for that: it spawns a helper into the same process group and
        # then sleeps past anything the test would wait for.
        repo = self.repositories["SVC"]
        pid_path = self.tmp_path / "wedged-child.pid"
        stub = [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(600)']);"
            f"open({str(pid_path)!r}, 'w').write(str(child.pid));"
            "time.sleep(600)",
        ]

        started = time.monotonic()
        with (
            mock.patch.object(run_commit, "_asl_commit_argv", return_value=stub),
            mock.patch.object(run_commit, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
        ):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "wedged-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=run_commit._CommitGeneration(harness="codex", timeout=1),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 120, "the commit subprocess outlived its bound")
        self.assertTrue(commit["timed_out"])
        # 124 is `run_harness`'s timeout code, so a bounded-out commit reads as
        # an ordinary nonzero commit exit everywhere downstream.
        self.assertEqual(124, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])
        self.assertEqual(2, commit["subprocess_timeout_seconds"])
        stderr = Path(str(commit["stderr_path"])).read_text(encoding="utf-8")
        self.assertIn("exceeded its 2s bound", stderr)
        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a commit-subprocess descendant outlived the outer backstop",
        )

    def test_outer_commit_timeout_kills_detached_descendants(self) -> None:
        # The real nested harness starts a session of its own, so a group kill
        # aimed at the commit subprocess never reaches it. The stub models that
        # exactly: the helper spawns with `start_new_session=True`, leaving the
        # group the backstop signals while still holding the captured pipes —
        # which is also what makes the drain time out rather than return.
        repo = self.repositories["SVC"]
        pid_path = self.tmp_path / "detached-child.pid"
        stub = [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(600)'], start_new_session=True);"
            f"open({str(pid_path)!r}, 'w').write(str(child.pid));"
            "time.sleep(600)",
        ]

        started = time.monotonic()
        with (
            mock.patch.object(run_commit, "_asl_commit_argv", return_value=stub),
            mock.patch.object(run_commit, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
        ):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "detached-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=run_commit._CommitGeneration(harness="codex", timeout=1),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 120, "the commit subprocess outlived its bound")
        # The escaped descendant changes nothing about how the expiry reports:
        # still exit 124, still the ordinary soft-failure path.
        self.assertTrue(commit["timed_out"])
        self.assertEqual(124, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])
        stderr = Path(str(commit["stderr_path"])).read_text(encoding="utf-8")
        self.assertIn("exceeded its 2s bound", stderr)

        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a detached commit descendant outlived the outer backstop",
        )
        # The group kill could not have reached it, so the sweep is what did:
        # assert on the report rather than only on the process being gone.
        sweep = commit["process_sweep"]
        self.assertIsNotNone(sweep, "the backstop recorded no descendant sweep")
        self.assertIn(child_pid, sweep["killed"])
        # The child is reaped before the sweep, so the group route is available
        # rather than skipped as a stranger's — and it still does not list the
        # detached descendant. The pre-kill snapshot is the only route to it.
        self.assertNotIn("pgid_skipped", sweep["enumeration"])
        self.assertNotIn(child_pid, sweep["enumeration"]["pgid_members"])

    def test_outer_commit_timeout_kills_descendants_forked_after_the_snapshot(
        self,
    ) -> None:
        # The test above forks its detached helper before the bound expires, so
        # the pre-kill snapshot names it. A real harness keeps spawning, and a
        # child born after that snapshot is in no snapshot and — being detached
        # — in no process group either. Releasing the fork from inside the
        # snapshot call is what makes the ordering a fact rather than a race:
        # the late child provably cannot exist until the snapshot is taken.
        repo = self.repositories["SVC"]
        early_pid_path = self.tmp_path / "early-detached.pid"
        late_pid_path = self.tmp_path / "late-detached.pid"
        release_path = self.tmp_path / "fork-now"
        forker = self.tmp_path / "late_forker.py"
        forker.write_text(
            "import os, subprocess, sys, time\n"
            "release, late_pid_path = sys.argv[1], sys.argv[2]\n"
            "while not os.path.exists(release):\n"
            "    time.sleep(0.01)\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(600)'],\n"
            "    start_new_session=True,\n"
            ")\n"
            "open(late_pid_path, 'w').write(str(child.pid))\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        stub = [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen("
            "[sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],"
            " start_new_session=True);"
            "open(sys.argv[4], 'w').write(str(child.pid));"
            "time.sleep(600)",
            str(forker),
            str(release_path),
            str(late_pid_path),
            str(early_pid_path),
        ]
        self.addCleanup(self._reap_recorded_pids, early_pid_path, late_pid_path)

        take_snapshot = run_commit._snapshot_commit_descendants
        captured: dict[str, Any] = {}

        def fork_after_snapshot(root_pid: int) -> tuple[Any, ...]:
            recorded = take_snapshot(root_pid)
            captured["snapshot_pids"] = [entry["pid"] for entry in recorded]
            release_path.write_text("go", encoding="utf-8")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not late_pid_path.exists():
                time.sleep(0.01)
            return recorded

        started = time.monotonic()
        with (
            mock.patch.object(run_commit, "_asl_commit_argv", return_value=stub),
            mock.patch.object(run_commit, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
            mock.patch.object(
                run_commit, "_snapshot_commit_descendants", fork_after_snapshot
            ),
        ):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "late-fork-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=run_commit._CommitGeneration(harness="codex", timeout=1),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 120, "the commit subprocess outlived its bound")
        # The late fork changes nothing about how the expiry reports: still
        # exit 124, still the ordinary soft-failure path.
        self.assertTrue(commit["timed_out"])
        self.assertEqual(124, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])

        self.assertTrue(late_pid_path.exists(), "the late child never forked")
        early_pid = int(early_pid_path.read_text(encoding="utf-8").strip())
        late_pid = int(late_pid_path.read_text(encoding="utf-8").strip())
        # The premise of the test, asserted rather than assumed: the snapshot
        # named the parent and could not have named the child.
        self.assertIn(early_pid, captured["snapshot_pids"])
        self.assertNotIn(late_pid, captured["snapshot_pids"])

        sweep = commit["process_sweep"]
        self.assertIsNotNone(sweep, "the backstop recorded no descendant sweep")
        # Neither of the two pre-existing routes could reach the late child, so
        # walking the live ancestry of a snapshot-verified process is what did.
        self.assertNotIn(late_pid, sweep["enumeration"]["pgid_members"])
        self.assertNotIn(late_pid, sweep["enumeration"]["snapshot_verified"])
        self.assertIn(late_pid, sweep["enumeration"]["late_descendants"])
        self.assertIn(late_pid, sweep["killed"])
        # `survivors_after` is re-sampled inside the sweep, so an empty entry
        # for the late child proves it was gone before the call returned.
        self.assertNotIn(late_pid, sweep["survivors_after"])
        self.assertFalse(
            wait_for_process_exit(late_pid),
            "a descendant forked after the snapshot outlived the outer backstop",
        )
        self.assertFalse(
            wait_for_process_exit(early_pid),
            "a detached commit descendant outlived the outer backstop",
        )

    def _reap_recorded_pids(self, *pid_paths: Path) -> None:
        """Kill whatever a sweep regression left behind when it fails.

        The stubs sleep for ten minutes, so a failing assertion would otherwise
        leak processes into the rest of the suite rather than just reporting.
        """
        for pid_path in pid_paths:
            if not pid_path.exists():
                continue
            try:
                pid = int(pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                continue
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    def test_ignored_evidence_is_recorded_but_never_staged(self) -> None:
        self._mark_repair_session()
        repo = self.repositories["SVC"]
        # A `.gitignore` naming itself is invisible to the untracked scan, so
        # rewriting it leaves `worktree_ignore` as its only evidence; the same
        # goes for `.git/info/exclude`, which no worktree scan reaches at all.
        ignore_path = repo / ".gitignore"
        ignore_path.write_text(".gitignore\ncache/\n", encoding="utf-8")
        exclude_path = repo / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("# baseline\n", encoding="utf-8")
        self.assertEqual(
            "", review_guard._run_git(repo, ["status", "--porcelain"]).stdout.strip()
        )
        commit_log = self.tmp_path / "commit-turns.jsonl"
        env = fake_commit_harness_env(self.tmp_path, log_path=commit_log)
        complete_session = self._harness(
            writes="SVC:repair.txt", declares="SVC:repair.txt"
        )

        def run(*args: object, **kwargs: object) -> harnesses.HarnessResult:
            ignore_path.write_text(".gitignore\ncache/\nbuild/\n", encoding="utf-8")
            exclude_path.write_text("# baseline\n*.tmp\n", encoding="utf-8")
            return complete_session(*args, **kwargs)

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(harnesses, "run_harness", side_effect=run),
        ):
            exit_code, payload = self._invoke("ignored-evidence-commit")

        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(payload["commits"]))
        commit = payload["commits"][0]
        # The guard still reports both ignore sources — dropping them from the
        # evidence would let a turn hide behind the rules it writes.
        self.assertEqual(
            ["SVC:repair.txt", "SVC:.gitignore", "SVC:.git/info/exclude"],
            commit["qualified_paths"],
        )
        self.assertEqual(["SVC:repair.txt"], commit["staging_paths"])
        self.assertEqual(["--pathspec", "repair.txt"], commit["argv"][-2:])
        self.assertEqual(
            ["repair.txt"],
            review_guard._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        self.assertEqual("success", commit["status_result"])
        self.assertEqual(
            ".gitignore\ncache/\nbuild/\n", ignore_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            [str(self.repositories["SVC"].resolve())],
            [record["cwd"] for record in read_jsonl(commit_log)],
        )

    def test_turn_finished_digest_matches_stored_result_json(self) -> None:
        payload = self._run_accepted()
        self.assertTrue(payload["readiness_state_active"])
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        stored = json.loads(
            Path(str(finished["result_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["after"], finished["after"])
        self.assertEqual(
            plans.plan_content_sha256(self.plan_dir),
            finished["after"]["content_sha256"],
        )
        self.assertEqual(finished["after"], payload["final_snapshot"])

    def test_scope_violation_is_rejected_and_reopened_without_acceptance(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness(writes="OAK:README.md", declares="(none)"),
        ):
            exit_code, payload = self._invoke("rejected-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        names = self._names()
        self.assertIn(events.TURN_EXECUTION_FINISHED_EVENT, names)
        self.assertNotIn(events.TURN_FINISHED_EVENT, names)

        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("workspace-write-guard", rejections[0]["guard"])
        self.assertEqual(payload["pause_reason"], rejections[0]["reason"])
        self.assertTrue(rejections[0]["reopened"])

        reopened = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopened))
        self.assertEqual("complete", reopened[0]["from_status"])
        self.assertEqual("pending", reopened[0]["to_status"])
        self.assertEqual("workspace-write-guard", reopened[0]["guard"])
        self.assertLess(
            names.index(events.TURN_REJECTED_EVENT),
            names.index(events.SESSION_REOPENED_EVENT),
        )
        self.assertEqual(
            "pending", plans.inspect_plan(self.plan_dir).sessions[0].status
        )

    def test_unverifiable_turn_is_rejected_and_reopened(self) -> None:
        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:README.md", declares="SVC:README.md"
                ),
            ),
            mock.patch.object(
                verification,
                "run_verification",
                side_effect=OSError("verifier unavailable"),
            ),
        ):
            exit_code, payload = self._invoke("verify-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("session-verification", rejections[0]["guard"])
        self.assertEqual("verify-failed", rejections[0]["reason"])
        self.assertIn("verifier unavailable", rejections[0]["detail"]["error"])
        self.assertTrue(rejections[0]["reopened"])
        self.assertEqual(1, len(self._payloads(events.SESSION_REOPENED_EVENT)))

    def test_publication_drift_pauses_instead_of_crashing_the_loop(self) -> None:
        rejection = turn_execution.TurnCommitRejected(
            reason="unexpected-status-change",
            message="session statuses moved",
            detail={"status_changes": []},
        )
        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:README.md", declares="SVC:README.md"
                ),
            ),
            mock.patch.object(
                turn_execution, "publication_snapshot", side_effect=rejection
            ),
        ):
            exit_code, payload = self._invoke("drift-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("ledger-sync", rejections[0]["guard"])
        self.assertEqual("unexpected-status-change", rejections[0]["reason"])

    def test_untouched_session_is_rejected_without_a_reopen(self) -> None:
        def idle(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            return harnesses.HarnessResult(
                name="codex", argv=["fake"], returncode=0, stdout="", stderr=""
            )

        with mock.patch.object(harnesses, "run_harness", side_effect=idle):
            exit_code, payload = self._invoke("idle-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("status-change", rejections[0]["guard"])
        self.assertEqual("no-status-change", rejections[0]["reason"])
        self.assertFalse(rejections[0]["reopened"])
        self.assertEqual([], self._payloads(events.SESSION_REOPENED_EVENT))


class CommitFailureProgressTests(unittest.TestCase):
    """What an operator watching the run is told when a commit is refused.

    The failure used to announce itself by ending the run. Now the progress
    journal is where it is announced, so these are the sentences that carry it.
    """

    def _emitted(self, call: Any) -> str:
        stream = io.StringIO()
        progress = run_reporting._ProgressReporter(enabled=True)
        with redirect_stderr(stream):
            call(progress)
        return stream.getvalue()

    def _emitted_by_the_run(self, call: Any) -> str:
        """Emit through the labeller ``run_loop`` actually injects.

        The reporter's own default is a second implementation of the same
        sentence, pinned equal in ``tests/test_asl_run_reporting.py``. A test
        about which group a line names has to read the one the run installs,
        or it would pass on the copy while the run printed the other.
        """
        stream = io.StringIO()
        progress = run_reporting._ProgressReporter(
            enabled=True,
            commit_group_label=commit_settlement._unresolved_commit_group_label,
            unresolved_groups_key=commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY,
        )
        with redirect_stderr(stream):
            call(progress)
        return stream.getvalue()

    def test_a_retried_failure_says_the_run_continues(self) -> None:
        text = self._emitted(
            lambda progress: progress.commit_failed(
                {
                    "session_id": "2",
                    "failed_alias": "SVC",
                    "error": "spec commit refused",
                    "retrying": True,
                }
            )
        )

        self.assertIn("commit failed: session 2 (SVC): spec commit refused", text)
        self.assertIn("retrying this group at the next commit boundary", text)

    def test_an_abandoned_failure_says_the_changes_are_uncommitted(self) -> None:
        text = self._emitted(
            lambda progress: progress.commit_failed(
                {
                    "session_id": None,
                    "failed_alias": None,
                    "message": "final auto-commit failed",
                    "retrying": False,
                }
            )
        )

        self.assertIn("commit failed: end-of-run: final auto-commit failed", text)
        self.assertIn("leaving these changes uncommitted", text)

    def test_the_final_summary_warns_without_failing_the_run(self) -> None:
        text = self._emitted(
            lambda progress: progress.run_finished(
                {
                    "ok": True,
                    "reason": "all-complete",
                    "run_id": "run-1",
                    "duration_seconds": 1.0,
                    commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY: [
                        {"session_id": "1", "failed_alias": "SVC"},
                        {"session_id": None, "failed_alias": None},
                    ],
                }
            )
        )

        self.assertIn(
            "warning: 2 commit group(s) were never completed: "
            "session 1 (SVC), end-of-run",
            text,
        )
        # The warning is non-fatal: the run still reports that it finished.
        self.assertIn("run finished: run run-1, reason all-complete", text)

    def test_unresolved_paused_group_label_names_paused_scope(self) -> None:
        # All three group scopes in one summary, because the point is that the
        # operator can tell them apart: the commit a session's verification
        # earned, the commit a recoverable pause settled for the work that
        # session's failed turn retained, and the end-of-run sweep. Naming the
        # middle one "session 2" hid whether the lost commit covered verified
        # work or work no verification ever passed.
        text = self._emitted_by_the_run(
            lambda progress: progress.run_finished(
                {
                    "ok": False,
                    "pause_reason": "verify-failed",
                    "run_id": "run-2",
                    "duration_seconds": 1.0,
                    commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY: [
                        {
                            "scope": commit_groups.GROUP_SCOPE_SESSION,
                            "session_id": "2",
                            "failed_alias": "SVC",
                        },
                        {
                            "scope": commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                            "session_id": "2",
                            "failed_alias": "SVC",
                        },
                        {
                            "scope": commit_groups.GROUP_SCOPE_END,
                            "session_id": None,
                            "failed_alias": None,
                        },
                    ],
                }
            )
        )

        self.assertIn(
            "warning: 3 commit group(s) were never completed: "
            "session 2 (SVC), session 2 retained work (SVC), end-of-run",
            text,
        )

    def test_a_paused_group_failure_names_the_retained_work(self) -> None:
        # The same distinction on the failure line, which is where an operator
        # meets the paused-work group first: a refused settlement says it was
        # the retained work that stayed uncommitted.
        text = self._emitted_by_the_run(
            lambda progress: progress.commit_failed(
                commit_settlement._commit_failure_record(
                    scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                    session_id="2",
                    group=None,
                    commit=None,
                    error="paused-work commit refused",
                    message="paused-work commit failed",
                )
            )
        )

        self.assertIn(
            "commit failed: session 2 retained work: paused-work commit refused",
            text,
        )


class CommitFailureSoftFailTests(unittest.TestCase):
    """Commit recovery precedes the required-tree check for the next worker.

    A transient failure recovers and continues. A persistent failure leaving a
    needed tree dirty pauses before another worker can launch.
    """

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
            ("pending", "pending"),
            specs=SPECS,
            contracts_by_id={
                session: _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session=session,
                )
                for session in ("1", "2")
            },
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self.context = verification_module.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        self.commit_log = self.tmp_path / "commit-turns.jsonl"

    def _harness(self, *_args: object, **_kwargs: object) -> harnesses.HarnessResult:
        """Complete whichever session is pending with a change of its own."""
        pending = next(
            session
            for session in plans.inspect_plan(self.plan_dir).sessions
            if session.status == "pending"
        )
        relative = f"session-{pending.session_id}.txt"
        (self.repositories["SVC"] / relative).write_text(
            f"work for session {pending.session_id}\n", encoding="utf-8"
        )
        session_path = self.plan_dir / f"session-{pending.session_id}.md"
        lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith(plans.STATUS_PREFIX):
                lines[index] = f"{plans.STATUS_PREFIX} complete\n"
            elif line.startswith(plans.HANDOFF_FILES_PREFIX):
                lines[index] = f"{plans.HANDOFF_FILES_PREFIX} SVC:{relative}\n"
        session_path.write_text("".join(lines), encoding="utf-8")
        return harnesses.HarnessResult(
            name="codex", argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _run(
        self,
        run_id: str,
        *,
        fail_limit: int | None = None,
        max_sessions: int | None = None,
    ) -> dict[str, Any]:
        env = fake_commit_harness_env(self.tmp_path, log_path=self.commit_log)
        env["FAKE_CODEX_COMMIT_MODE"] = "failure"
        if fail_limit is not None:
            env["FAKE_CODEX_COMMIT_FAIL_LIMIT"] = str(fail_limit)
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=max_sessions,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            model=None,
            effort=None,
            note=None,
            max_pending_sessions=30,
            allow_not_loop_ready=True,
        )
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(harnesses, "run_harness", side_effect=self._harness),
            ):
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.workspace_root,
                    timeout=None,
                    max_sessions=max_sessions,
                    commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                    lock_info=lock_info,
                    run_id=run_id,
                    verification_context=self.context,
                )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)
        self.exit_code = exit_code
        return payload

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def test_a_refused_commit_blocks_the_next_needed_dirty_session(self) -> None:
        """The run stops, and it stops as the commit failure it actually was.

        This repository refuses every commit, so the group spends its whole
        retry budget and the run ends before a second worker launches. The
        reason is ``commit-failed``: the dirt the next session's tree check
        would object to is the run's own uncommitted work, and naming the tree
        instead would report a bounded commit failure as an environment for a
        human to clean up. Both reasons are environment-blocked, so what this
        pins is which of the two the supervisor is told about.
        """
        payload = self._run("soft-fail-run")

        self.assertEqual(1, self.exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("commit-failed", payload["reason"])
        # Stopping with a retry unspent is the failure mode this guards: the
        # tree check the next session brings sees the failed group's dirt, so it
        # used to refuse before the last attempt was ever made.
        self.assertEqual(
            commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1,
            len(payload["commit_failures"]),
        )
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(
            ["complete", "pending"],
            [session.status for session in plans.inspect_plan(self.plan_dir).sessions],
        )
        names = [record["name"] for record in self._records()]
        self.assertEqual(1, names.count(events.TURN_FINISHED_EVENT))
        self.assertIn("run_paused", names)
        # The turn executed, verified, and completed its session; only the
        # commit of its changes failed, so no guard rejected it.
        self.assertNotIn(events.TURN_REJECTED_EVENT, names)

    def test_the_failure_is_recorded_on_the_turn_and_on_the_run(self) -> None:
        payload = self._run("soft-fail-record-run")

        failures = payload["commit_failures"]
        self.assertEqual({"session"}, {record["scope"] for record in failures})
        self.assertEqual({"1"}, {record["session_id"] for record in failures})
        first = failures[0]
        self.assertIn("auto-commit failed after session 1", first["message"])
        self.assertFalse(first["commit"]["success"])
        self.assertEqual(
            "session-commit", payload["turns"][0]["session_commit"]["guard"]
        )
        self.assertEqual(
            "commit-failed", payload["turns"][0]["session_commit"]["reason"]
        )

    def test_an_unresolved_group_is_reported_by_the_run_that_lost_it(self) -> None:
        payload = self._run("soft-fail-report-run")

        summaries = payload["unresolved_commit_groups"]
        self.assertEqual({"1"}, {str(row["session_id"]) for row in summaries})
        self.assertEqual({"session"}, {row["scope"] for row in summaries})
        self.assertEqual({"SVC"}, {row["failed_alias"] for row in summaries})
        # The same record reaches the state file the supervisor audit reads.
        persisted = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(summaries, persisted["unresolved_commit_groups"])
        self.assertEqual(
            "SVC",
            commit_settlement._unresolved_commit_groups(persisted)[0]["failed_alias"],
        )

    def test_a_transient_failure_heals_at_the_next_commit_boundary(self) -> None:
        payload = self._run("soft-fail-retry-run", fail_limit=1)

        self.assertEqual(0, self.exit_code)
        self.assertEqual([1, 2], payload["completed_sessions"])
        # One failure, retried before the next session ran, and nothing owed.
        self.assertEqual(1, len(payload["commit_failures"]))
        self.assertEqual([], payload["unresolved_commit_groups"])
        self.assertTrue(payload["commit_group_recovery"]["success"])
        self.assertEqual(
            "complete", payload["commit_group_recovery"]["group"]["status"]
        )
        records = self._records()
        recovered = [
            index
            for index, record in enumerate(records)
            if record["name"] == events.COMMIT_FINISHED_EVENT
            and record["payload"].get("success")
        ][0]
        session_2_started = [
            index
            for index, record in enumerate(records)
            if record["name"] == "turn_started"
            and str(record["payload"].get("session")) == "2"
        ][0]
        self.assertLess(recovered, session_2_started)
        self.assertEqual(
            "",
            review_guard._run_git(
                self.repositories["SVC"], ["status", "--porcelain"]
            ).stdout.strip(),
        )

    def test_a_run_stops_retrying_one_group_after_its_budget(self) -> None:
        payload = self._run("soft-fail-budget-run")

        # A repository that refuses every time must not turn the session loop
        # into a commit loop: each group is attempted its budget and no more.
        by_group: dict[str, int] = {}
        for record in payload["commit_failures"]:
            group_id = str(record["group_id"])
            by_group[group_id] = by_group.get(group_id, 0) + 1
        self.assertTrue(by_group)
        for attempts in by_group.values():
            self.assertLessEqual(
                attempts, commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1
            )

    def test_unused_dirty_repository_does_not_block_worker_launch(self):
        unused = next(alias for alias in self.repositories if alias != "SVC")
        for session in plans.inspect_plan(self.plan_dir).sessions:
            text = session.path.read_text()
            registry = plans.read_repository_registry(self.plan_dir)
            scope = plans.parse_session_scope(text, registry.aliases)
            text = text.replace(
                plans.render_session_scope(scope).strip(),
                plans.render_session_scope(
                    plans.SessionScope(reads=("SVC",), writes=("SVC",))
                ).strip(),
            )
            session.path.write_text(text)
        path = self.repositories[unused] / "independent.txt"
        path.write_text("pre-existing independent work\n")
        payload = self._run("unused-dirt", fail_limit=0)
        self.assertEqual(0, self.exit_code, payload.get("message"))
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertEqual("pre-existing independent work\n", path.read_text())

    def test_seventeen_session_budget_stops_before_session_twenty_one(self):
        write_multi_repo_plan(
            self.plan_dir, ("complete",) * 3 + ("pending",) * 18, specs=SPECS
        )
        payload = self._run("seventeen-session-budget", fail_limit=0, max_sessions=17)
        self.assertEqual(0, self.exit_code, payload.get("message"))
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual(list(range(4, 21)), payload["completed_sessions"])
        self.assertEqual("21", plans.inspect_plan(self.plan_dir).next_pending.id)

    def test_later_scope_blocks_preexisting_dirty_repository(self):
        unused = next(alias for alias in self.repositories if alias != "SVC")
        registry = plans.read_repository_registry(self.plan_dir)
        first = self.plan_dir / "session-1.md"
        text = first.read_text()
        scope = plans.parse_session_scope(text, registry.aliases)
        first.write_text(
            text.replace(
                plans.render_session_scope(scope).strip(),
                plans.render_session_scope(
                    plans.SessionScope(reads=("SVC",), writes=("SVC",))
                ).strip(),
            )
        )
        (self.repositories[unused] / "independent.txt").write_text("existing dirt\n")
        payload = self._run("later-needed-dirt", fail_limit=0)
        self.assertEqual(1, self.exit_code)
        self.assertEqual("commit-preflight", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual("2", plans.inspect_plan(self.plan_dir).next_pending.id)

    def test_new_unused_repository_mutation_still_rejects_worker(self):
        unused = next(alias for alias in self.repositories if alias != "SVC")
        registry = plans.read_repository_registry(self.plan_dir)
        first = self.plan_dir / "session-1.md"
        text = first.read_text()
        scope = plans.parse_session_scope(text, registry.aliases)
        first.write_text(
            text.replace(
                plans.render_session_scope(scope).strip(),
                plans.render_session_scope(
                    plans.SessionScope(reads=("SVC",), writes=("SVC",))
                ).strip(),
            )
        )
        original = self._harness

        def mutating(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.repositories[unused] / "new-dirt.txt").write_text("new mutation\n")
            return result

        with mock.patch.object(self, "_harness", side_effect=mutating):
            payload = self._run("new-unused-mutation", fail_limit=0)
        self.assertEqual(1, self.exit_code)
        self.assertEqual("workspace-write-scope-violation", payload["reason"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual("1", plans.inspect_plan(self.plan_dir).next_pending.id)
        self.assertTrue((self.repositories[unused] / "new-dirt.txt").exists())


class UnregisteredCommitTargetTests(unittest.TestCase):
    """ASI-19: a clean unregistered repository is never handed to a commit.

    The registered path has always dropped a clean repository before the group
    was built, but the single target an unregistered workspace resolves to was
    handed over unexamined. A session that wrote nothing into the bound
    repository -- one that repaired an external audit store, say -- therefore
    launched ``asl commit`` with empty staging and qualified paths, and the
    refusal to commit nothing was recorded as that repository's failure. The
    next run read the same still-clean tree at the same HEAD, recorded
    ``skipped-clean``, and finalized the failed group as complete.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name) / "repo"
        self.repo_root.mkdir(parents=True)
        init_git_repo(self.repo_root)
        self.context = verification_module.VerificationContext.legacy(self.repo_root)

    def _dirty(self) -> None:
        (self.repo_root / "work.txt").write_text("work\n", encoding="utf-8")

    def _coordinate(
        self,
        targets: list[dict[str, Any]],
        *,
        group: dict[str, Any] | None = None,
        skip_clean: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
        """Drive one group, recording every commit it launches."""
        launched: list[dict[str, Any]] = []
        if group is None:
            group = commit_groups.create_group(
                scope="session",
                session_id="11.1",
                targets=targets,
                initial_heads={
                    str(target["key"]): commit_settlement._commit_target_head(target)
                    for target in targets
                },
            )

        def run_commit(target: Mapping[str, Any], group_id: str) -> Mapping[str, Any]:
            launched.append(dict(target))
            return {
                "success": False,
                "returncode": 1,
                "status_result": "nonzero-exit",
                "repository_key": target["key"],
                "group_id": group_id,
            }

        final, failed = commit_groups.coordinate_group(
            group,
            is_clean=commit_settlement._commit_target_is_clean,
            read_head=commit_settlement._commit_target_head,
            run_commit=run_commit,
            persist=lambda *_args: None,
            skip_clean=skip_clean,
        )
        return launched, final, failed

    def test_a_clean_repository_yields_no_commit_target(self) -> None:
        self.assertEqual([], commit_settlement._dirty_commit_targets(self.context))

    def test_a_dirty_repository_still_yields_its_target(self) -> None:
        self._dirty()
        targets = commit_settlement._dirty_commit_targets(self.context)
        self.assertEqual(
            [commit_groups.LEGACY_REPOSITORY_KEY],
            [target["key"] for target in targets],
        )
        self.assertEqual(self.repo_root.resolve(), Path(targets[0]["root"]))

    def test_the_end_scope_reads_the_same_rule(self) -> None:
        self.assertEqual(
            [], commit_settlement._registered_end_commit_targets(self.context)
        )
        self._dirty()
        self.assertEqual(
            1, len(commit_settlement._registered_end_commit_targets(self.context))
        )

    def test_a_clean_repository_records_no_failed_commit_attempt(self) -> None:
        launched, group, failed = self._coordinate(
            commit_settlement._dirty_commit_targets(self.context)
        )
        self.assertEqual([], launched)
        self.assertIsNone(failed)
        self.assertEqual(commit_groups.GROUP_STATUS_COMPLETE, group["status"])
        self.assertEqual([], group["successful_repositories"])
        self.assertEqual([], group["skipped_repositories"])

    def test_the_unfiltered_target_is_what_failed_its_group(self) -> None:
        launched, group, failed = self._coordinate(
            commit_settlement._commit_targets(self.context)
        )
        self.assertEqual(
            [commit_groups.LEGACY_REPOSITORY_KEY],
            [target["key"] for target in launched],
        )
        self.assertEqual([], launched[0]["staging_paths"])
        self.assertEqual([], launched[0]["qualified_paths"])
        self.assertEqual(commit_groups.GROUP_STATUS_FAILED, group["status"])
        assert failed is not None
        self.assertEqual(1, failed["returncode"])

    def test_the_resumed_group_no_longer_has_a_failure_to_paper_over(self) -> None:
        _launched, failed_group, _failed = self._coordinate(
            commit_settlement._commit_targets(self.context)
        )
        resumed_launches, resumed, refused = self._coordinate(
            [dict(target) for target in failed_group["repositories"]],
            group=failed_group,
            skip_clean=True,
        )
        # This is the second half of the finding, still reachable from a group
        # a failed attempt left behind: the clean tree at the unchanged HEAD
        # reads back as skipped, and the group that failed completes.
        self.assertEqual([], resumed_launches)
        self.assertIsNone(refused)
        self.assertEqual(commit_groups.GROUP_STATUS_COMPLETE, resumed["status"])
        self.assertEqual("clean", resumed["skipped_repositories"][0]["reason"])
        # Nothing reaches that path now: the selection never names the target
        # whose refusal put the group into failure.
        self.assertEqual([], commit_settlement._dirty_commit_targets(self.context))

    def test_the_unregistered_session_scope_reads_the_filtered_selection(self) -> None:
        # Matched structurally rather than by substring: qualifying the callee
        # with its owning module made the line long enough for the formatter to
        # wrap the argument, and a text match reads that as a behaviour change.
        # The per-session commit moved to `turn_settlement` in Session 36; the
        # selection it reads is the claim, not the file it is read in.
        tree = ast.parse(inspect.getsource(turn_settlement.settle_turn))

        def names_the_context(node: ast.expr) -> bool:
            # The verification context is reached through the run context now,
            # so the argument is an attribute rather than a bare local.
            if isinstance(node, ast.Name):
                return node.id == "verification_context"
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "verification_context"
                and isinstance(node.value, ast.Name)
                and node.value.id == "ctx"
            )

        selectors = {
            call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute | ast.Name)
            and any(names_the_context(arg) for arg in call.args)
        }
        self.assertIn("_dirty_commit_targets", selectors)
        self.assertNotIn("_commit_targets", selectors)


class PublicationSnapshotTests(unittest.TestCase):
    """The snapshot a turn is published against is re-read, and must still agree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def publish(self) -> plans.PlanSnapshot:
        return turn_execution.publication_snapshot(
            plans.PlanSnapshotCache(), self.snapshot
        )

    def test_readiness_bookkeeping_is_republished_without_moving_identity(self) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="running",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        published = self.publish()
        self.assertEqual(
            plans.plan_content_sha256(self.plan_dir), published.content_sha256
        )
        self.assertEqual(self.snapshot.content_sha256, published.content_sha256)

    def test_status_drift_refuses_the_turn(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(turn_execution.TurnCommitRejected) as caught:
            self.publish()
        self.assertEqual("unexpected-status-change", caught.exception.reason)
        self.assertEqual(
            [{"number": 1, "before": "pending", "after": "complete"}],
            caught.exception.detail["status_changes"],
        )

    def test_unreadable_plan_refuses_the_turn_rather_than_raising_plan_error(
        self,
    ) -> None:
        (self.plan_dir / "session-1.md").write_text("no handoff\n", encoding="utf-8")
        with self.assertRaises(turn_execution.TurnCommitRejected) as caught:
            self.publish()
        self.assertEqual("malformed-plan", caught.exception.reason)
        self.assertTrue(caught.exception.detail["error"])


class PlanMutationReinspectionTests(unittest.TestCase):
    """A runner-owned plan rewrite is re-read before it is reported."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def test_reinspection_reports_the_rewritten_plan(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        refreshed = session_verification._reinspect_after_plan_mutation(
            plans.PlanSnapshotCache(), self.snapshot
        )
        self.assertEqual("pending", self.snapshot.sessions[0].status)
        self.assertEqual("complete", refreshed.sessions[0].status)

    def test_unparseable_rewrite_keeps_the_pre_mutation_snapshot(self) -> None:
        (self.plan_dir / "session-1.md").write_text("no handoff\n", encoding="utf-8")
        refreshed = session_verification._reinspect_after_plan_mutation(
            plans.PlanSnapshotCache(), self.snapshot
        )
        self.assertIs(self.snapshot, refreshed)


class TurnAnomalyClassificationTests(unittest.TestCase):
    """A clean exit code is not on its own evidence that a turn succeeded."""

    def test_unreadable_handoff_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "plan_error": "session-1.md: no handoff"}
            )
        )

    def test_failure_status_result_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "status_result": "no-status-change"}
            )
        )

    def test_accepted_turn_is_not_anomalous(self) -> None:
        self.assertFalse(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "status_result": "complete", "plan_error": None}
            )
        )

    def test_turn_without_a_status_result_is_judged_on_the_generic_signals(
        self,
    ) -> None:
        self.assertFalse(supervisor_audit._turn_shows_failure({"returncode": 0}))
        self.assertTrue(supervisor_audit._turn_shows_failure({"returncode": 1}))


if __name__ == "__main__":
    unittest.main()
