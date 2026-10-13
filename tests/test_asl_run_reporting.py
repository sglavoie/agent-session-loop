"""The run's terminal surface: one stderr handle, one lock, one voice.

The behavioural coverage of what an operator is *told* lives where it always
has — ``tests/test_asl_harness_*.py`` drives whole runs and reads
their progress lines back, and ``tests/test_asl_turn_commit_*.py`` owns the
sentences a refused commit prints. What is asserted here is the contract those
modules cannot state, because they exercise the run rather than the seam:

* the module does not import the runner, and needs nothing from it;
* the stderr handle is *this* module's and no one else's — the coordinator is
  the only place in the run's live output path that resolves ``sys.stderr``,
  and every collaborator the loop wires is given the coordinator's proxy
  instead;
* the two commit-lifecycle collaborators enter at construction rather than by
  import, and the defaults this module falls back to still agree with
  ``commit_settlement``'s own constant and labeller; and
* output never ends a run: a sink that raises is disabled for the rest of the
  run rather than propagating.

The one sentence whose wording is asserted here is the paused-work settlement
line, and it is here because no whole-run driver reaches it: a settled pause
needs a recoverable pause *and* a dirty worktree at the moment the run gives
up, which the integration harness cannot stage without becoming a second
implementation of the settlement. The line is not decoration — it is the only
place the run tells the operator whether to relaunch or to look — so its
disposition clause is pinned per result rather than left to a substring.

The stderr group is the reason this file exists. A sink bug here does not fail
a gate: it tears one operator line across two threads, or it turns an exited
helper's abandoned pipe into an exception that ends a healthy run, and both
look like something else entirely from the outside.
"""

from __future__ import annotations

import ast
import io
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from agent_session_loop import (
    commit_settlement,
    run_loop,
    run_reporting,
    runner,
    turn_execution,
)
from tests import support_asl_run_loop_view as run_loop_view
from tests.support import assert_no_runner_import

#: The functions in the runner and its run loop that write ``sys.stderr``
#: directly: none, since session 28. All three writers of this kind have left
#: for boundaries that run where
#: no coordinator exists -- ``_print_text_outcome`` to ``run_outcome`` in
#: session 26, and ``_not_implemented`` and ``_print_doctor_error`` to
#: ``run_doctor`` in session 28 -- and each is pinned at its new owner by
#: ``tests/test_asl_run_outcome.py`` and ``tests/test_asl_run_doctor.py`` under
#: the same rule. Anything appearing here now is a second writer racing the
#: coordinator for the terminal while a run is live.
RUNNER_STDERR_WRITERS: frozenset[str] = frozenset()


class _RaisingStream(io.StringIO):
    """A sink that behaves like a pipe whose reader has gone away."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def write(self, text: str) -> int:
        raise self._error

    def flush(self) -> None:
        raise self._error


def _stderr_owners(source: str) -> set[str]:
    """The top-level definitions in ``source`` that name ``sys.stderr``."""
    tree = ast.parse(source)
    owners: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "stderr"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "sys"
            ):
                owners.add(node.name)
                break
    return owners


class RunReportingBoundaryTests(unittest.TestCase):
    """``run_reporting`` is runner-free and writes nothing durable."""

    def setUp(self) -> None:
        self.source = Path(run_reporting.__file__ or "").read_text(encoding="utf-8")

    def test_run_reporting_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_reporting)

    def test_the_module_writes_no_durable_record(self) -> None:
        # Every other surface in a run leaves a trace behind. This one's whole
        # output is ephemeral, which is why losing the terminal can be made a
        # non-event: there is nothing here for a reader to miss later.
        for reach in ("write_text(", "open(", "mkdir(", "unlink(", "durability."):
            self.assertNotIn(reach, self.source, msg=reach)

    def test_the_reporter_satisfies_the_turn_boundary_protocol(self) -> None:
        # Structural, not nominal: the turn boundary reports through this
        # object without either module importing the other.
        progress: turn_execution.TurnProgress = run_reporting._ProgressReporter(
            enabled=False
        )
        self.assertIsNotNone(progress)


class StderrOwnershipTests(unittest.TestCase):
    """One handle on ``sys.stderr``, and every collaborator writes through it."""

    def test_only_the_coordinator_resolves_sys_stderr(self) -> None:
        source = Path(run_reporting.__file__ or "").read_text(encoding="utf-8")
        self.assertEqual({"_StderrOutputCoordinator"}, _stderr_owners(source))

    def test_the_run_keeps_no_second_live_output_writer(self) -> None:
        for module in (runner, run_loop):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__ or "").read_text(encoding="utf-8")
                self.assertEqual(RUNNER_STDERR_WRITERS, _stderr_owners(source))

    def test_the_control_listener_and_tail_are_handed_the_proxy(self) -> None:
        # `run_control.RunControlListener` and `tail.TailFollower` both default
        # to the real `sys.stderr`; the run never lets them, because a second
        # unserialized writer is exactly what tears a line.
        source = run_loop_view.assembled_source()
        self.assertIn("stderr=output.stream", source)
        reporting_source = Path(run_reporting.__file__ or "").read_text(
            encoding="utf-8"
        )
        self.assertIn("out=self._output.stream", reporting_source)

    def test_the_proxy_writes_through_the_coordinator_lock(self) -> None:
        stream = io.StringIO()
        coordinator = run_reporting._StderrOutputCoordinator(
            enabled=True, stream=stream
        )
        held: list[bool] = []

        original = stream.write

        def observe(text: str) -> int:
            # Probed from a thread that does not own the lock: the owner would
            # re-acquire a reentrant mutex unconditionally and prove nothing.
            probe = threading.Thread(
                target=lambda: held.append(
                    not coordinator._lock.acquire(blocking=False)
                )
            )
            probe.start()
            probe.join()
            return original(text)

        stream.write = observe  # type: ignore[method-assign]
        coordinator.stream.write("one line\n")

        self.assertEqual(["one line\n"], [stream.getvalue()])
        self.assertEqual([True], held)


class LineBufferingTests(unittest.TestCase):
    """A partial line waits for its newline, per writing thread."""

    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.coordinator = run_reporting._StderrOutputCoordinator(
            enabled=True, stream=self.stream
        )

    def test_a_partial_line_is_withheld_until_its_newline_arrives(self) -> None:
        self.coordinator.write_unprefixed("half")
        self.assertEqual("", self.stream.getvalue())

        self.coordinator.write_unprefixed(" a line\n")
        self.assertEqual("half a line\n", self.stream.getvalue())

    def test_two_threads_never_split_each_other_s_line(self) -> None:
        def writer(word: str) -> None:
            for chunk in (word[:3], word[3:], "\n"):
                self.coordinator.write_unprefixed(chunk)

        threads = [
            threading.Thread(target=writer, args=(word,))
            for word in ("alphabet", "bicycle-")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(
            {"alphabet", "bicycle-"},
            set(self.stream.getvalue().splitlines()),
        )

    def test_flush_thread_releases_this_thread_s_partial_line(self) -> None:
        self.coordinator.write_unprefixed("no newline")
        self.coordinator.flush_thread()
        self.assertEqual("no newline", self.stream.getvalue())


class SinkFailureTests(unittest.TestCase):
    """An abandoned pipe costs a run its terminal, never its run."""

    def _coordinator(
        self, error: BaseException
    ) -> run_reporting._StderrOutputCoordinator:
        return run_reporting._StderrOutputCoordinator(
            enabled=True, stream=_RaisingStream(error)
        )

    def test_a_broken_pipe_disables_the_sink_instead_of_raising(self) -> None:
        coordinator = self._coordinator(BrokenPipeError())
        coordinator.emit_line("progress")
        self.assertFalse(coordinator.enabled)

    def test_an_os_error_on_a_batch_disables_the_sink(self) -> None:
        coordinator = self._coordinator(OSError("gone"))
        coordinator.emit_lines(["one", "two"])
        self.assertFalse(coordinator.enabled)

    def test_an_unprefixed_write_disables_the_sink(self) -> None:
        coordinator = self._coordinator(BrokenPipeError())
        self.assertEqual(len("line\n"), coordinator.write_unprefixed("line\n"))
        self.assertFalse(coordinator.enabled)

    def test_a_flush_failure_disables_the_sink(self) -> None:
        coordinator = self._coordinator(OSError("gone"))
        coordinator.write_unprefixed("partial")
        coordinator.flush_thread()
        self.assertFalse(coordinator.enabled)

    def test_isatty_degrades_to_false_rather_than_raising(self) -> None:
        class _Rude(io.StringIO):
            def isatty(self) -> bool:
                raise ValueError("detached")

        coordinator = run_reporting._StderrOutputCoordinator(
            enabled=True, stream=_Rude()
        )
        self.assertFalse(coordinator.isatty())


class CommitVocabularySeamTests(unittest.TestCase):
    """The commit lifecycle enters at construction, not by import."""

    def _emitted(self, reporter: run_reporting._ProgressReporter, call: Any) -> str:
        stream = io.StringIO()
        with redirect_stderr(stream):
            call(reporter)
        return stream.getvalue()

    def test_the_default_key_is_the_key_the_owner_writes(self) -> None:
        self.assertEqual(
            commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY,
            run_reporting.DEFAULT_UNRESOLVED_COMMIT_GROUPS_KEY,
        )

    def test_the_default_labeller_agrees_with_the_owner_s(self) -> None:
        # `commit_settlement` owns the commit lifecycle's vocabulary and this
        # module may not import it — `commit_settlement` imports this one for
        # the reporter type — so the defaults here are a second implementation
        # of one sentence, pinned equal rather than trusted to stay so.
        summaries: list[dict[str, Any]] = [
            {"session_id": "3", "failed_alias": "OAK"},
            {"session_id": "3", "failed_alias": None},
            {"session_id": None, "failed_alias": "OAK"},
            {"session_id": None, "failed_alias": None},
            {},
        ]
        # Every scope as well, since the scope is now part of the sentence and
        # the two implementations must agree on which value changes it.
        summaries.extend(
            {"scope": scope, "session_id": session_id, "failed_alias": alias}
            for scope in ("session", "session-paused", "end", "invented", None)
            for session_id in ("3", None)
            for alias in ("OAK", None)
        )
        for summary in summaries:
            with self.subTest(summary=summary):
                self.assertEqual(
                    commit_settlement._unresolved_commit_group_label(summary),
                    run_reporting.default_commit_group_label(summary),
                )

    def test_an_injected_labeller_is_what_the_reporter_prints(self) -> None:
        reporter = run_reporting._ProgressReporter(
            enabled=True,
            commit_group_label=lambda summary: f"group {summary.get('session_id')}",
        )
        text = self._emitted(
            reporter,
            lambda progress: progress.commit_failed(
                {"session_id": "7", "error": "refused", "retrying": False}
            ),
        )
        self.assertIn("commit failed: group 7: refused", text)

    def test_an_injected_key_is_where_the_reporter_reads_owed_groups(self) -> None:
        reporter = run_reporting._ProgressReporter(
            enabled=True, unresolved_groups_key="owed"
        )
        text = self._emitted(
            reporter,
            lambda progress: progress.run_finished(
                {
                    "ok": True,
                    "reason": "all-complete",
                    "run_id": "run-1",
                    "duration_seconds": 1.0,
                    "owed": [{"session_id": "1", "failed_alias": "SVC"}],
                }
            ),
        )
        self.assertIn("1 commit group(s) were never completed: session 1 (SVC)", text)
        # The key the reporter was not given is not a key it reads.
        other = run_reporting._ProgressReporter(enabled=True)
        quiet = self._emitted(
            other,
            lambda progress: progress.run_finished(
                {
                    "ok": True,
                    "reason": "all-complete",
                    "run_id": "run-1",
                    "duration_seconds": 1.0,
                    "owed": [{"session_id": "1", "failed_alias": "SVC"}],
                }
            ),
        )
        self.assertNotIn("never completed", quiet)


class PausedWorkSettlementLineTests(unittest.TestCase):
    """What the settlement line owes the operator holding the relaunch."""

    def _emitted(self, record: dict[str, Any], **kwargs: Any) -> str:
        reporter = run_reporting._ProgressReporter(enabled=True, **kwargs)
        stream = io.StringIO()
        with redirect_stderr(stream):
            reporter.paused_work_committed(record)
        return stream.getvalue()

    def test_every_result_says_what_the_relaunch_will_find(self) -> None:
        # The outcome word is the run's vocabulary, not the operator's
        # question. Each line is pinned to its disposition clause because the
        # clause is the part they act on.
        for result, disposition in run_reporting.PAUSED_WORK_DISPOSITIONS.items():
            with self.subTest(result=result):
                text = self._emitted(
                    {"result": result, "pause_reason": "verify-failed"}
                )
                self.assertIn(f"paused work {result}", text)
                self.assertIn("after the verify-failed pause", text)
                self.assertIn(disposition, text)

    def test_an_unknown_result_still_accounts_for_the_worktree(self) -> None:
        text = self._emitted({"result": "invented", "pause_reason": "timeout"})
        self.assertIn("paused work invented after the timeout pause", text)
        self.assertIn("leaving the retained changes in the worktree", text)

    def test_a_record_with_no_result_or_reason_names_both_unknown(self) -> None:
        text = self._emitted({})
        self.assertIn("paused work unknown after the unknown pause", text)

    def test_the_group_is_named_only_when_the_settlement_built_one(self) -> None:
        record: dict[str, Any] = {
            "result": "committed",
            "pause_reason": "verify-failed",
            "commit_group": {"session_id": "8", "failed_alias": None},
        }
        self.assertIn("for session 8", self._emitted(record))
        # `no-changes` and the guard refusals settle without a group, and the
        # record is the only thing that knows which session this was.
        self.assertNotIn("for session", self._emitted({**record, "commit_group": None}))

    def test_the_group_label_is_the_injected_one(self) -> None:
        text = self._emitted(
            {
                "result": "committed",
                "pause_reason": "verify-failed",
                "commit_group": {"session_id": "8"},
            },
            commit_group_label=lambda summary: f"group {summary.get('session_id')}",
        )
        self.assertIn("for group 8", text)

    def test_a_refused_commit_reads_its_own_verdict_as_the_cause(self) -> None:
        # A commit that refuses raises nothing, so `error` is empty exactly
        # when the commit's status is the fact worth printing.
        text = self._emitted(
            {
                "result": "failed",
                "pause_reason": "verify-failed",
                "commit": {"status_result": "protected path changed"},
            }
        )
        self.assertIn("paused work failed after the verify-failed pause: ", text)
        self.assertIn("protected path changed", text)

    def test_a_raised_error_outranks_the_commit_status(self) -> None:
        text = self._emitted(
            {
                "result": "failed",
                "pause_reason": "verify-failed",
                "error": "asl commit exited 1",
                "commit": {"status_result": "refused"},
            }
        )
        self.assertIn("asl commit exited 1", text)
        self.assertNotIn("refused", text)

    def test_a_settlement_that_worked_states_no_cause(self) -> None:
        text = self._emitted(
            {
                "result": "committed",
                "pause_reason": "verify-failed",
                "commit": {"status_result": None},
            }
        )
        self.assertNotIn(":", text.split("paused work", 1)[1].split(";", 1)[0])


class CompactStatusProjectionTests(unittest.TestCase):
    """The status key reads the run; it never writes it."""

    def test_the_projection_leaves_the_payload_it_read_untouched(self) -> None:
        payload: dict[str, Any] = {
            "run_started_at": "2026-01-01T00:00:00+00:00",
            "harness": "claude",
            "turns": [{"session_id": "1", "stdout_path": "/tmp/out"}],
        }
        control: dict[str, Any] = {"stop_after_current": False, "tail_enabled": True}
        before = repr(payload), repr(control)

        lines = run_reporting._compact_status_lines(
            payload=payload, control=control, snapshot=None
        )

        self.assertEqual(before, (repr(payload), repr(control)))
        self.assertTrue(all(line.startswith("status") for line in lines))

    def test_a_plan_that_could_not_be_read_is_reported_not_raised(self) -> None:
        lines = run_reporting._compact_status_lines(
            payload={},
            control={},
            snapshot=None,
            plan_error="plan directory is missing",
        )
        self.assertIn("status: plan inspection error: plan directory is missing", lines)
        self.assertIn("status: no active turn; sessions unavailable", lines[0])


if __name__ == "__main__":
    unittest.main()
