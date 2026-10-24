"""Boundary tests for `agent_session_loop.run_outcome`.

The behavioural coverage of the outcome printer already exists and stays where
it is: `tests/test_asl_pause_ux.py` owns the paused text output and the
questions hint, `tests/test_asl_cli.py` owns the `--dry-run` preview lines, and
`tests/test_asl_control_signal.py` drives a real run through `_emit_run_payload`
while the lock is held open. None of them can cheaply state what this module actually
owns, which is *the last thing a run says on the one path where nothing else
can be said*, so that is what is stated here.

Four contracts:

* The extraction contract -- runner-free, no alias left behind, exactly the
  eight symbols the session moved, and no durable write anywhere.
* The reader contract -- every `payload[...]` key the printer indexes without a
  presence check is supplied by both payloads that reach it: the one
  `run_payloads._initial_payload` seeds and the one `_error_payload` builds
  here. Session 25's handoff nominated this as the pin for those reads; the key
  list is derived from the source by AST rather than curated, so a new bare
  index that nothing seeds fails here instead of raising `KeyError` mid-run.
* The stderr contract -- `_print_text_outcome` is the only function here that
  resolves `sys.stderr`, and it is allowed to only because it runs after the
  run has ended, when `run_reporting`'s coordinator no longer exists to race.
  `tests/test_asl_run_reporting.py` states the other half of that rule.
* The terminal-emission contract -- a dead pipe never kills the run, because
  the payload is already durable in `state.json` and `result.json` by the time
  this module is reached.

Every payload here is built for real against a plan on disk. Nothing reaches
Git or a harness, so nothing spawns a process, which is what keeps this module
in `PORTABLE_MODULES`; `test_printing_every_outcome_spawns_no_process` asserts
that rather than assuming it.
"""

from __future__ import annotations

import argparse
import ast
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    plans_files,
    plans_models,
    plans_snapshot,
    run_outcome,
    run_payloads,
    run_selection,
    runner,
    state,
)
from tests.support import assert_no_runner_import, called_names, write_plan
from tests.support_base import RaisingStream as _RaisingStream
from tests.support_base import parse_cli_args as _parse
from tests.support_base import stderr_owners as _stderr_owners

ASL_TEST_PORTABLE = True

RUN_ID = "20260903T000000Z-run-outcome"

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_FUNCTIONS = (
    "_state_error_reason",
    "_error_payload",
    "_print_model_effort_lines",
    "_commit_scope_label",
    "_print_commit_outcome_lines",
    "_last_commit",
    "_print_text_outcome",
    "_emit_run_payload",
)

#: Every call this boundary could make that outlives the process. Both halves
#: of this module are terminal -- `_error_payload` returns a dictionary its
#: caller persists, and the printers write only to the terminal -- so the
#: assertion below is a flat prohibition rather than the per-function map the
#: write-owning boundaries use.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "append_progress_entry",
        "mkdir",
        "rename",
        "rmdir",
        "touch",
        "unlink",
        "utime",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
    }
)

#: Keys the printer indexes on *every* payload it is handed, whatever the run
#: did. A payload missing one of these cannot be printed at all.
INDEXED_ALWAYS = frozenset({"plan_dir", "ok", "completed_sessions"})

#: Keys the printer indexes once it knows the run did not succeed. `reason` is
#: the failed spelling and `pause_reason`/`pause_message` the paused one; both
#: payloads carry all three because the printer picks the branch, not the
#: builder.
INDEXED_ON_FAILURE = frozenset({"reason", "pause_reason", "pause_message", "turns"})

#: Keys the printer indexes only after `payload["ok"]` proved true, which
#: `_error_payload` never does. Only the seeded payload has to carry these.
INDEXED_ON_SUCCESS = frozenset({"final_snapshot"})

#: Keys indexed only inside an `is not None` guard on the same key, so a
#: payload that omits them prints one line fewer rather than raising.
INDEXED_UNDER_GUARD = frozenset({"harness", "run_dir", "state_path", "events_path"})


def _bare_payload_reads(tree: ast.Module) -> set[str]:
    """Constant string keys ``_print_text_outcome`` reads off ``payload``.

    Only ``ast.Load`` subscripts count: a ``payload["x"] = ...`` in
    ``_error_payload`` is this module *writing* the shape, not depending on it.
    """
    printer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_print_text_outcome"
    )
    keys: set[str] = set()
    for node in ast.walk(printer):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "payload"
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


class _PlanFixture:
    """A plan on disk plus the resolved paths the two payload sources need."""

    def __init__(self, case: unittest.TestCase, statuses: tuple[str, ...]) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # Resolve the temp root: `plans_snapshot.inspect_plan` canonicalizes the macOS
        # `/var/folders/...` symlink and a later path comparison would differ.
        self.root = Path(tmp.name).resolve()
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, statuses)
        self.state_dir = self.root / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.run_dir = self.plan_state / "runs" / RUN_ID
        self.lock_info = state.LockInfo(
            path=self.plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans_files.canonicalize_path(self.plan_dir)),
            created_at="2026-08-10T10:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )

    def args(self, *tokens: str) -> argparse.Namespace:
        # `--harness claude` keeps the selection deterministic: the native model
        # payload for `codex`/`copilot` reads the developer's own config files.
        return _parse(
            [
                "run",
                str(self.plan_dir),
                "--state-dir",
                str(self.state_dir),
                "--harness",
                "claude",
                *tokens,
            ]
        )

    def snapshot(self) -> plans_models.PlanSnapshot:
        return plans_snapshot.inspect_plan(self.plan_dir)

    def seeded(self, *tokens: str) -> dict[str, Any]:
        """The payload a real run is born with, from `run_payloads`."""
        args = self.args(*tokens)
        return run_payloads._initial_payload(
            args=args,
            snapshot=self.snapshot(),
            state_dir=self.plan_state,
            run_dir=self.run_dir,
            event_path=self.plan_state / events.EVENTS_FILE_NAME,
            state_path=self.plan_state / events.STATE_FILE_NAME,
            lock_info=self.lock_info,
            cwd=self.root,
            run_id=RUN_ID,
            commit_mode=None,
            mailbox_activation=run_selection._mailbox_activation_payload(
                args, self.plan_dir
            ),
        )

    def error(
        self,
        *tokens: str,
        reason: str = "invalid-input",
        message: str = "boom",
        paused: bool = False,
        snapshot: plans_models.PlanSnapshot | None = None,
    ) -> dict[str, Any]:
        """The payload this module builds when no run was ever seeded."""
        return run_outcome._error_payload(
            self.args(*tokens),
            reason=reason,
            message=message,
            paused=paused,
            cwd=self.root,
            snapshot=snapshot,
        )


class RunOutcomeBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes nothing."""

    def setUp(self) -> None:
        self.source = Path(run_outcome.__file__ or "").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_outcome)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_FUNCTIONS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(run_outcome, name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        functions = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        classes = {
            node.name for node in self.tree.body if isinstance(node, ast.ClassDef)
        }
        constants = {
            target.id
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(set(MOVED_FUNCTIONS), functions)
        self.assertEqual(set(), classes)
        self.assertEqual(set(), constants)

    def test_this_boundary_makes_no_durable_write(self) -> None:
        # The run's durable record is written before this module is reached.
        # A printer that learned to persist its own output would be writing
        # after the run released its mutex.
        self.assertEqual(frozenset(), called_names(self.tree) & DURABLE_WRITE_NAMES)


class StderrOwnershipTests(unittest.TestCase):
    """One writer here, and it runs where no coordinator exists to race."""

    def test_only_the_text_printer_resolves_sys_stderr(self) -> None:
        source = Path(run_outcome.__file__ or "").read_text(encoding="utf-8")
        self.assertEqual({"_print_text_outcome"}, _stderr_owners(source))

    def test_the_failure_half_goes_to_stderr_and_the_result_half_to_stdout(
        self,
    ) -> None:
        plan = _PlanFixture(self, ("pending",))
        payload = plan.error(reason="invalid-input", message="bad flag")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            run_outcome._print_text_outcome(payload)

        self.assertIn("Run failed: invalid-input", err.getvalue())
        self.assertNotIn("Run failed", out.getvalue())
        self.assertIn(f"Plan: {payload['plan_dir']}", out.getvalue())


class ReaderContractTests(unittest.TestCase):
    """Every key the printer indexes bare is a key its two payloads supply."""

    def setUp(self) -> None:
        source = Path(run_outcome.__file__ or "").read_text(encoding="utf-8")
        self.reads = _bare_payload_reads(ast.parse(source))
        self.plan = _PlanFixture(self, ("pending", "pending"))

    def test_the_key_tables_below_cover_every_bare_read(self) -> None:
        # Derived from the source, so a new `payload["x"]` added to the printer
        # lands in no table and fails here rather than in a live run.
        self.assertEqual(
            INDEXED_ALWAYS
            | INDEXED_ON_FAILURE
            | INDEXED_ON_SUCCESS
            | INDEXED_UNDER_GUARD,
            self.reads,
        )

    def test_the_seeded_payload_supplies_every_key_the_printer_indexes(self) -> None:
        # The cross-boundary half of session 25's contract: `run_payloads`
        # seeds the shape, this module reads it without a presence check, and
        # nothing between them checks that the two agree. This does.
        payload = self.plan.seeded()
        for key in INDEXED_ALWAYS | INDEXED_ON_FAILURE | INDEXED_ON_SUCCESS:
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_the_error_payload_supplies_every_key_the_failure_path_reads(
        self,
    ) -> None:
        # `_error_payload` never reports `ok`, so the success-only keys are
        # legitimately absent; everything the failed and paused branches touch
        # is not.
        for paused in (False, True):
            payload = self.plan.error(paused=paused)
            self.assertFalse(payload["ok"])
            for key in INDEXED_ALWAYS | INDEXED_ON_FAILURE:
                with self.subTest(paused=paused, key=key):
                    self.assertIn(key, payload)

    def test_both_payloads_print_without_a_key_error(self) -> None:
        # The tables above say the keys are present; this says the printer
        # actually gets through both documents end to end.
        for label, payload in (
            ("seeded", self.plan.seeded()),
            ("failed", self.plan.error()),
            ("paused", self.plan.error(reason="runner-pause", paused=True)),
        ):
            with (
                self.subTest(payload=label),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                run_outcome._print_text_outcome(payload)


class ErrorPayloadTests(unittest.TestCase):
    """The payload of last resort, and the classifier in front of it."""

    def setUp(self) -> None:
        self.plan = _PlanFixture(self, ("pending", "pending"))

    def test_lock_contention_is_a_pause_and_anything_else_is_a_failure(self) -> None:
        for message, expected in (
            ("plan is locked by pid 4 on some-other-host", ("lock-contention", True)),
            ("could not acquire lock", ("lock-contention", True)),
            ("state.json is unreadable", ("state-error", False)),
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    expected,
                    run_outcome._state_error_reason(state.StateError(message)),
                )

    def test_a_paused_error_payload_carries_the_pause_classification(self) -> None:
        # `run_termination.set_pause` attaches the same single-source-of-truth
        # block, so a run that paused before it ever started is classified the
        # same way as one that paused mid-loop.
        paused = self.plan.error(reason="lock-contention", paused=True)
        self.assertTrue(paused["paused"])
        self.assertEqual("lock-contention", paused["pause_reason"])
        self.assertIsInstance(paused["pause_policy"], dict)

        failed = self.plan.error(reason="invalid-input")
        self.assertNotIn("pause_policy", failed)

    def test_a_snapshot_adds_the_state_paths_and_both_snapshot_blocks(self) -> None:
        without = self.plan.error()
        self.assertNotIn("state_path", without)
        self.assertNotIn("final_snapshot", without)

        with_snapshot = self.plan.error(snapshot=self.plan.snapshot())
        self.assertEqual(str(self.plan.plan_state), with_snapshot["state_dir"])
        self.assertEqual(
            str(self.plan.plan_state / events.EVENTS_FILE_NAME),
            with_snapshot["event_path"],
        )
        self.assertEqual(with_snapshot["event_path"], with_snapshot["events_path"])
        self.assertEqual(
            with_snapshot["initial_snapshot"], with_snapshot["final_snapshot"]
        )

    def test_a_review_run_names_the_output_path_and_the_skip_reason(self) -> None:
        plain = self.plan.error(reason="invalid-input")
        self.assertFalse(plain["review_requested"])
        self.assertIsNone(plain["review_output_path"])
        self.assertIsNone(plain["review_skipped_reason"])

        review = self.plan.error("--review-harness", "claude", reason="invalid-input")
        self.assertTrue(review["review_requested"])
        self.assertTrue(review["review_output_path"])
        self.assertEqual("invalid-input", review["review_skipped_reason"])

    def test_an_uncanonicalizable_plan_dir_degrades_to_the_raw_argument(self) -> None:
        args = self.plan.args()
        with mock.patch.object(
            plans_files, "canonicalize_path", side_effect=OSError("no")
        ):
            payload = run_outcome._error_payload(
                args, reason="invalid-input", message="boom", paused=False
            )
        self.assertEqual(str(args.plan_dir), payload["plan_dir"])


class CommitOutcomeTests(unittest.TestCase):
    """The commit lines, and the shapes the printer refuses to trust."""

    def test_the_scope_label_names_the_session_the_end_or_the_repository(
        self,
    ) -> None:
        for commit, expected in (
            ({"scope": "session", "session_id": "7"}, "session 7"),
            ({"scope": "session", "session": "7"}, "session 7"),
            ({"scope": "end"}, "end"),
            ({"scope": "session"}, "session"),
            ({}, "unknown"),
            (
                {"scope": "end", "repository_alias": "api"},
                "end, repository api",
            ),
            ({"scope": "end", "repo": "api"}, "end, repository api"),
        ):
            with self.subTest(commit=commit):
                self.assertEqual(expected, run_outcome._commit_scope_label(commit))

    def test_a_malformed_commits_block_prints_nothing_rather_than_raising(
        self,
    ) -> None:
        for commits in (None, "not-a-list", [None, 7, "row"]):
            with self.subTest(commits=commits):
                out = io.StringIO()
                with redirect_stdout(out):
                    run_outcome._print_commit_outcome_lines({"commits": commits})
                self.assertEqual("", out.getvalue())
                self.assertIsNone(run_outcome._last_commit({"commits": commits}))

    def test_the_success_and_failure_lines_name_the_sha_and_the_status(self) -> None:
        payload = {
            "commits": [
                {
                    "scope": "session",
                    "session_id": "1",
                    "commit_index": 1,
                    "success": True,
                    "commit_sha": "abc123",
                },
                {
                    "scope": "end",
                    "commit_index": 2,
                    "success": False,
                    "status_result": "dirty",
                },
            ]
        }
        out = io.StringIO()
        with redirect_stdout(out):
            run_outcome._print_commit_outcome_lines(payload)
        lines = out.getvalue().splitlines()
        self.assertEqual("Created commit abc123 (session 1)", lines[0])
        self.assertEqual("Commit 2 failed (end): dirty", lines[1])
        self.assertEqual(payload["commits"][-1], run_outcome._last_commit(payload))

    def test_the_model_and_effort_lines_omit_what_the_payload_omits(self) -> None:
        for block, expected in (
            ({}, []),
            ({"model": "opus"}, []),
            ({"model": "opus", "model_source": "flag"}, ["Model: opus (flag)"]),
            ({"effort": "high"}, ["Effort: high"]),
            (
                {"effort": "high", "effort_source": "config"},
                ["Effort: high (config)"],
            ),
        ):
            with self.subTest(block=block):
                out = io.StringIO()
                with redirect_stdout(out):
                    run_outcome._print_model_effort_lines(block)
                self.assertEqual(expected, out.getvalue().splitlines())


class TerminalEmissionTests(unittest.TestCase):
    """The last write of a run never becomes the reason the run failed."""

    def setUp(self) -> None:
        self.plan = _PlanFixture(self, ("pending",))

    def test_json_and_text_are_the_only_two_routes(self) -> None:
        payload = self.plan.error()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            run_outcome._emit_run_payload(self.plan.args("--json"), payload)
        self.assertIn('"reason": "invalid-input"', out.getvalue())

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            run_outcome._emit_run_payload(self.plan.args(), payload)
        self.assertNotIn('"reason"', out.getvalue())
        self.assertIn("Plan: ", out.getvalue())

    def test_a_dead_pipe_is_swallowed_on_both_routes(self) -> None:
        # A recovered run whose original reader exited must not crash on its
        # final emission; the same payload is already durable on disk.
        payload = self.plan.error()
        for error in (BrokenPipeError("gone"), OSError("closed")):
            for tokens in ((), ("--json",)):
                with (
                    self.subTest(error=type(error).__name__, tokens=tokens),
                    redirect_stdout(_RaisingStream(error)),
                    redirect_stderr(io.StringIO()),
                ):
                    run_outcome._emit_run_payload(self.plan.args(*tokens), payload)

    def test_an_unrelated_error_still_propagates(self) -> None:
        # The swallow is scoped to a dead sink, not to "printing may fail".
        payload = self.plan.error()
        with (
            self.assertRaises(ValueError),
            redirect_stdout(_RaisingStream(ValueError("nope"))),
            redirect_stderr(io.StringIO()),
        ):
            run_outcome._emit_run_payload(self.plan.args(), payload)


class PortabilityTests(unittest.TestCase):
    """Nothing at this boundary shells out, which is the PORTABLE_MODULES claim."""

    def test_printing_every_outcome_spawns_no_process(self) -> None:
        plan = _PlanFixture(self, ("pending", "pending"))

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("this boundary must not spawn a process")

        with (
            mock.patch.object(subprocess, "Popen", refuse),
            mock.patch.object(subprocess, "run", refuse),
            mock.patch.object(subprocess, "call", refuse),
            mock.patch.object(subprocess, "check_call", refuse),
            mock.patch.object(subprocess, "check_output", refuse),
        ):
            payload = plan.error(snapshot=plan.snapshot())
            self.assertTrue(payload)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                run_outcome._emit_run_payload(plan.args(), payload)
                run_outcome._emit_run_payload(plan.args("--json"), payload)


if __name__ == "__main__":
    unittest.main()
