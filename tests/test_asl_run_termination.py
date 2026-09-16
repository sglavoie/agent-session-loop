"""How a run ends, asserted at the seam rather than through a run.

The behavioural coverage of each ending already lives where it always has —
``tests/test_asl_assurance_lifecycle.py`` drives the terminal path and reads its
audit back, ``tests/test_asl_pause_ux.py`` owns what a pause tells an operator,
and ``tests/test_asl_control.py`` owns the stop and abort a supervisor asks for.
What is asserted here is the contract those modules cannot state, because they
exercise the run rather than the boundary:

* the module does not import the runner, and needs nothing from it;
* it opens no file of its own — every durable record it owns goes through the
  ``_RunPersistence`` handle the caller supplied, under that handle's lock;
* the four collaborators this boundary does not own enter as *required*
  parameters, so a terminal path cannot silently skip the readiness close, the
  status revert, the re-inspection, or the verdict projection by omitting an
  argument; and
* ``set_pause`` is the single writer of the pause block, structurally, so a
  second assembly site cannot appear without failing here.

The last one is why this file exists. Every failure mode in this boundary is
quiet: a readiness close that does not run leaves the plan advertising a state
the run no longer holds, an abort that reports its pre-revert snapshot tells a
supervisor the killed session still holds a completion, and a pause assembled by
hand reaches ``asl status`` with no policy and no remediation while still looking
exactly like a pause.
"""

from __future__ import annotations

import ast
import inspect
import shlex
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans,
    run_control,
    run_persistence,
    run_termination,
    runner,
)
from tests.support import assert_no_runner_import, read_jsonl, write_plan

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_UNAUDITED_TERMINAL_OUTCOMES",
    "_audited_repositories",
    "_terminal_outcome",
    "_attach_terminal_assurance",
    "_TerminalAdjudication",
    "_adjudicate_terminal_run",
    "_terminalize_run",
    "_final_readiness_state_from_payload",
    "_finalize_readiness_lifecycle",
    "_PAUSE_REMEDIATION_PATHS",
    "_optional_str",
    "_runtime_reason_remediation",
    "_pause_remediation",
    "_pause_run",
    "_detach_turn_controls",
    "_record_abort_satisfied_by_worker",
    "_honor_abort_turn",
    "_honor_stop_after_current",
    "_stop_before_review_work",
)

#: ``_set_pause`` is the one moved symbol that changed name: the session that
#: extracted this boundary published it, because every later module pauses
#: through it and a private name invites reaching around it.
RENAMED_SYMBOLS = {"_set_pause": "set_pause"}

#: Each seam and the function that must take it without a default.
REQUIRED_SEAMS = (
    ("_terminalize_run", "write_readiness_state"),
    ("_finalize_readiness_lifecycle", "write_readiness_state"),
    ("_honor_abort_turn", "revert_session_status"),
    ("_honor_abort_turn", "reinspect"),
    ("_stop_before_review_work", "record_existing_review_verdict"),
)


def _durable_write_receivers(source: str) -> set[str]:
    """Every receiver of a ``write_state`` or ``append_event`` call, as source."""
    tree = ast.parse(source)
    receivers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"write_state", "append_event"}:
            continue
        receivers.add(ast.unparse(node.func.value))
    return receivers


def _pause_block_writers(source: str) -> set[str]:
    """Functions that assign ``payload["pause_reason"]`` anywhere in their body."""
    tree = ast.parse(source)
    writers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for assignment in ast.walk(node):
            if not isinstance(assignment, ast.Assign):
                continue
            for target in assignment.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "pause_reason"
                ):
                    writers.add(node.name)
    return writers


class TerminationBoundaryTests(unittest.TestCase):
    """The structural contract of the boundary itself."""

    @property
    def source(self) -> str:
        return Path(run_termination.__file__).read_text(encoding="utf-8")

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_termination)

    def test_the_module_opens_no_file_of_its_own(self) -> None:
        # This boundary owns the pause block, the terminal verdict and the
        # readiness close -- three durable records written from paths that can
        # race a control thread. What makes that safe is that none of them are
        # written here: they all go through the run's single mutex.
        for reach in ("write_text(", "open(", "mkdir(", "unlink(", "durability."):
            self.assertNotIn(reach, self.source, msg=reach)

    def test_every_durable_write_goes_through_a_persistence_handle(self) -> None:
        self.assertEqual({"persistence"}, _durable_write_receivers(self.source))

    def test_set_pause_is_the_only_writer_of_the_pause_block(self) -> None:
        self.assertEqual({"set_pause"}, _pause_block_writers(self.source))
        # And the runner assembles none of its own: it pauses through the entry
        # this boundary published rather than writing the block beside it.
        self.assertEqual(set(), _pause_block_writers(runner_source()))

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(run_termination, name))

    def test_the_published_pause_entry_replaced_its_private_name(self) -> None:
        for old, new in RENAMED_SYMBOLS.items():
            with self.subTest(name=old):
                self.assertFalse(hasattr(runner, old))
                self.assertFalse(hasattr(run_termination, old))
                self.assertTrue(hasattr(run_termination, new))

    def test_every_seam_is_required_rather_than_defaulted(self) -> None:
        for function_name, parameter_name in REQUIRED_SEAMS:
            with self.subTest(function=function_name, parameter=parameter_name):
                signature = inspect.signature(getattr(run_termination, function_name))
                parameter = signature.parameters[parameter_name]
                self.assertIs(parameter.default, inspect.Parameter.empty)
                self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)


def runner_source() -> str:
    return Path(runner.__file__).read_text(encoding="utf-8")


class _TerminationCase(unittest.TestCase):
    """Shared scaffolding: a real persistence handle over a temp directory."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plan_dir = self.tmp_dir / "plan"
        write_plan(self.plan_dir, ("complete", "pending"))
        self.event_path = self.tmp_dir / events.EVENTS_FILE_NAME
        self.payload: dict[str, Any] = {
            "state_path": str(self.tmp_dir / events.STATE_FILE_NAME),
            "run_id": "run-1",
            "plan_dir": str(self.plan_dir),
        }
        self.persistence = run_persistence._RunPersistence()

    def _listener(self) -> run_control.RunControlListener:
        return run_control.RunControlListener(enabled=False)

    def _snapshot(self) -> plans.PlanSnapshot:
        return plans.inspect_plan(self.plan_dir)


class ReadinessCloseTests(_TerminationCase):
    """The readiness writer enters as a seam and its outcome is recorded."""

    def test_a_successful_write_records_the_state_the_run_left(self) -> None:
        calls: list[tuple[Path, str]] = []

        def writer(
            plan_dir: Path,
            new_state: plans.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            calls.append((plan_dir, new_state))
            return True, None

        payload = dict(self.payload, readiness_state_active=True, ok=True)
        run_termination._finalize_readiness_lifecycle(
            payload, write_readiness_state=writer
        )

        self.assertEqual([(self.plan_dir, "loop-ready")], calls)
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        self.assertEqual("loop-ready", payload["readiness_state_current"])

    def test_a_refused_write_records_the_error_and_no_final_state(self) -> None:
        def writer(
            plan_dir: Path,
            new_state: plans.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            return False, "readiness digest moved"

        payload = dict(self.payload, readiness_state_active=True, ok=True)
        run_termination._finalize_readiness_lifecycle(
            payload, write_readiness_state=writer
        )

        self.assertIsNone(payload["readiness_state_final"])
        self.assertEqual(
            "readiness digest moved", payload["readiness_state_write_error"]
        )

    def test_an_inactive_lifecycle_never_reaches_the_writer(self) -> None:
        def writer(
            plan_dir: Path,
            new_state: plans.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            raise AssertionError("the writer must not run for an inactive lifecycle")

        payload = dict(self.payload, ok=True)
        run_termination._finalize_readiness_lifecycle(
            payload, write_readiness_state=writer
        )

        self.assertNotIn("readiness_state_final", payload)

    def test_the_finalizer_factory_binds_the_writer_it_was_given(self) -> None:
        # `_finish_run` takes a one-argument callback, so the seam has to be
        # bound somewhere; this is the one place that binding is made.
        calls: list[str] = []

        def writer(
            plan_dir: Path,
            new_state: plans.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            calls.append(new_state)
            return True, None

        payload = dict(self.payload, readiness_state_active=True, ok=True)
        run_termination.readiness_finalizer(writer)(payload)

        self.assertEqual(["loop-ready"], calls)


class PauseWriteTests(_TerminationCase):
    """The pause block, its policy, and the event that publishes it."""

    def test_a_pause_run_writes_the_block_and_appends_the_event(self) -> None:
        run_termination._pause_run(
            self.event_path,
            self.payload,
            reason="verify-failed",
            message="checks failed",
            details={"repair_hints": [{"check_id": "suite"}]},
            persistence=self.persistence,
        )

        self.assertTrue(self.payload["paused"])
        self.assertFalse(self.payload["ok"])
        self.assertEqual("verify-failed", self.payload["pause_reason"])
        names = [record["name"] for record in read_jsonl(self.event_path)]
        self.assertEqual([events.RUN_PAUSED_EVENT], names)

    def test_the_pause_write_happens_under_the_run_s_mutex(self) -> None:
        # A pause races the control poller by construction, so the block and its
        # event must not be observable half-written from another thread.
        held: list[bool] = []

        def probe(event_path: Path, name: str, payload: dict[str, Any]) -> None:
            thread = threading.Thread(
                target=lambda: held.append(
                    not self.persistence.lock.acquire(blocking=False)
                )
            )
            thread.start()
            thread.join()

        original = self.persistence.append_event
        self.persistence.append_event = probe  # type: ignore[method-assign]
        try:
            run_termination._pause_run(
                self.event_path,
                self.payload,
                reason="blocked",
                message="needs an answer",
                persistence=self.persistence,
            )
        finally:
            self.persistence.append_event = original  # type: ignore[method-assign]

        self.assertEqual([True], held)
        self.assertEqual("blocked", self.payload["pause_reason"])

    def test_remediation_prefers_the_hints_the_pause_evidence_carries(self) -> None:
        remediation = run_termination._pause_remediation(
            {"readiness_gate": {"repair_hints": [{"check_id": "loop-ready"}]}},
            reason="not-loop-ready",
        )

        self.assertEqual([{"check_id": "loop-ready"}], remediation)

    def test_a_reason_with_no_hints_still_resolves_a_remediation(self) -> None:
        # Without this fallback a runner-side refusal tells an operator that
        # something is wrong without ever saying where to look.
        remediation = run_termination._pause_remediation({}, reason="no-contract")

        self.assertEqual(["runtime.no-contract"], [e["check_id"] for e in remediation])
        self.assertTrue(all(entry.get("message") for entry in remediation))

    def test_a_reason_that_resolves_to_nothing_records_no_guesswork(self) -> None:
        self.assertEqual([], run_termination._pause_remediation({}, reason="inspect"))

    def test_a_closed_question_names_the_explicit_reply_resume_command(self) -> None:
        self.payload["plan_dir"] = "/tmp/plan with 'quotes'"
        self.payload["state_dir"] = "/tmp/custom state/plan-hash"
        self.payload["run_config"] = {"state_dir_override": "/tmp/custom state"}
        closure = {"action": "pause", "question_id": "closed-question"}
        run_termination.set_pause(
            self.payload,
            reason="blocked",
            message="operator requested a pause",
            details={"mailbox_resolution": closure},
        )

        self.assertTrue(self.payload["paused"])
        self.assertFalse(self.payload["pause_policy"]["recoverable"])
        action = self.payload["action_required"]
        self.assertEqual("reply", action["user_action"])
        [hint] = action["remediation"]
        self.assertIn("closed the mailbox question", hint["message"])
        command = hint["message"].split("`")[1]
        self.assertEqual(
            [
                "asl",
                "run",
                self.payload["plan_dir"],
                "--resume",
                "--reply-stdin",
                "--state-dir",
                "/tmp/custom state",
            ],
            shlex.split(command),
        )
        self.assertEqual(closure, self.payload["pause_details"]["mailbox_resolution"])

    def test_a_closed_question_does_not_turn_a_guard_refusal_into_a_reply(self) -> None:
        run_termination.set_pause(
            self.payload,
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            message="guard refused writes",
            details={"mailbox_resolution": {"action": "pause"}},
        )
        self.assertEqual("inspect", self.payload["action_required"]["user_action"])
        self.assertNotIn(
            "runtime.mailbox-pause-resolution",
            [
                hint["check_id"]
                for hint in self.payload["action_required"]["remediation"]
            ],
        )

    def test_a_closed_question_replaces_even_a_generic_live_pause_signal(self) -> None:
        signal = self._live_signal("runner-pause")
        self.payload["action_required"] = signal
        run_termination.set_pause(
            self.payload,
            reason="runner-pause",
            message="paused",
            details={"mailbox_resolution": {"action": "pause"}},
        )
        self.assertEqual(
            signal, self.payload[events.ACTION_REQUIRED_SUPERSEDED_KEY]["record"]
        )
        [hint] = self.payload["action_required"]["remediation"]
        self.assertIn("--resume --reply-stdin", hint["message"])

    def test_a_pause_projection_refreshes_when_its_question_is_closed(self) -> None:
        run_termination.set_pause(self.payload, reason="blocked", message="needs input")
        self.assertEqual([], self.payload["action_required"]["remediation"])
        run_termination.set_pause(
            self.payload,
            reason="blocked",
            message="operator paused",
            details={"mailbox_resolution": {"action": "pause"}},
        )
        self.assertEqual("operator paused", self.payload["action_required"]["message"])
        [hint] = self.payload["action_required"]["remediation"]
        self.assertIn("--resume --reply-stdin", hint["message"])

    def test_an_unanswerable_pause_still_says_where_to_look(self) -> None:
        # A pause no reply can clear is exactly the one an operator most needs
        # directed: there is no question coming, so the remediation is the only
        # thing that tells them which pids to check.
        remediation = run_termination._pause_remediation(
            {}, reason=events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED
        )

        self.assertEqual(
            ["runtime.redirect-successor-blocked"],
            [entry["check_id"] for entry in remediation],
        )
        [entry] = remediation
        self.assertIn("ps -p", entry["message"])
        self.assertIn("No reply clears this", entry["message"])

    def _live_signal(self, reason: str = "runner-status-paused") -> dict[str, Any]:
        return {
            "reason": reason,
            "message": "the harness asked to be looked at",
            "source": "turn",
            "session_id": "session-1",
        }

    def test_a_runner_declared_pause_retires_the_live_ask_it_moved_past(self) -> None:
        # `clear_live_action_required` fires only on a new turn or an unpaused
        # finish, and a paused run reaches neither, so this is the only place
        # the generic harness ask is retired before the run ends on a verdict
        # of its own.
        signal = self._live_signal()
        self.payload["action_required"] = dict(signal)

        run_termination.set_pause(
            self.payload, reason="blocked", message="the runner refused"
        )

        superseded = self.payload[events.ACTION_REQUIRED_SUPERSEDED_KEY]
        self.assertEqual(events.SUPERSEDED_BY_RUNNER_PAUSE, superseded["superseded_by"])
        self.assertEqual("blocked", superseded["pause_reason"])
        self.assertEqual(signal, superseded["record"])
        # `blocked` requires a user, so the pause writes its own ask in place of
        # the one it displaced rather than leaving the run asking for nothing.
        self.assertEqual("blocked", self.payload["action_required"]["reason"])
        self.assertEqual("pause", self.payload["action_required"]["source"])

    def test_a_pause_that_is_itself_the_live_ask_keeps_the_turn_s_record(self) -> None:
        # The turn's own signal is the more specific record of the same ask.
        signal = self._live_signal("runner-pause")
        self.payload["action_required"] = dict(signal)

        run_termination.set_pause(
            self.payload, reason="runner-pause", message="the harness paused"
        )

        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, self.payload)
        self.assertEqual(signal, self.payload["action_required"])

    def test_a_signal_the_runner_declared_is_never_retired(self) -> None:
        # Review findings and their kin are not live signals: nothing about a
        # later pause answers them, so they stand.
        signal = self._live_signal("review-findings")
        self.payload["action_required"] = dict(signal)

        run_termination.set_pause(
            self.payload, reason="unsafe", message="the runner refused"
        )

        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, self.payload)
        self.assertEqual(signal, self.payload["action_required"])

    def test_a_pause_with_no_standing_ask_records_no_retirement(self) -> None:
        run_termination.set_pause(
            self.payload, reason="malformed-contract", message="unreadable"
        )

        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, self.payload)
        self.assertEqual(
            "malformed-contract", self.payload["action_required"]["reason"]
        )


class HonoredStopTests(_TerminationCase):
    """The stop and the abort a supervisor asks for, and their seams."""

    def test_the_stop_records_the_snapshot_it_was_handed(self) -> None:
        listener = self._listener()
        listener.state.request_stop_after_current()

        run_termination._honor_stop_after_current(
            listener,
            snapshot=self._snapshot(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload={},
            persistence=self.persistence,
        )

        self.assertEqual("stopped", self.payload["reason"])
        self.assertEqual(
            plans.snapshot_payload(self._snapshot()), self.payload["final_snapshot"]
        )
        self.assertIsNone(listener.state.consume_stop_after_current())

    def test_the_abort_reports_the_snapshot_taken_after_the_revert(self) -> None:
        # The whole point of the revert is that the killed session no longer
        # holds a completion; reporting the pre-revert snapshot would say it
        # still does.
        reverted_path = self.plan_dir / "session-2.md"
        seen: dict[str, Any] = {}

        def revert(
            snapshot: plans.PlanSnapshot,
            session_id: str,
            *,
            failure_note: str,
        ) -> Path | None:
            seen["session_id"] = session_id
            seen["failure_note"] = failure_note
            return reverted_path

        after = self._snapshot()

        def reinspect(
            plan_dir: Path,
            fallback: plans.PlanSnapshot,
            path: Path | None,
            plan_cache: plans.PlanSnapshotCache,
        ) -> plans.PlanSnapshot:
            seen["reverted_path"] = path
            return after

        run_termination._honor_abort_turn(
            self._listener(),
            snapshot=self._snapshot(),
            session_id="2",
            payload=self.payload,
            event_path=self.event_path,
            control_payload={},
            plan_cache=plans.PlanSnapshotCache(),
            revert_session_status=revert,
            reinspect=reinspect,
            persistence=self.persistence,
        )

        self.assertEqual("2", seen["session_id"])
        self.assertIn("aborted by supervisor request", seen["failure_note"])
        self.assertEqual(reverted_path, seen["reverted_path"])
        self.assertEqual("stopped", self.payload["reason"])
        self.assertEqual(plans.snapshot_payload(after), self.payload["final_snapshot"])

    def test_the_review_stop_records_its_verdict_before_the_stop(self) -> None:
        order: list[str] = []

        def record(payload: dict[str, Any], plan_dir: Path) -> None:
            order.append("verdict")
            payload["review_verdict"] = {"outcome": "all-clear"}

        original = self.persistence.write_state

        def write_state(payload: dict[str, Any]) -> None:
            order.append("stop")
            original(payload)

        self.persistence.write_state = write_state  # type: ignore[method-assign]
        try:
            run_termination._stop_before_review_work(
                self._listener(),
                snapshot=self._snapshot(),
                payload=self.payload,
                event_path=self.event_path,
                control_payload={},
                record_existing_review_verdict=record,
                persistence=self.persistence,
            )
        finally:
            self.persistence.write_state = original  # type: ignore[method-assign]

        self.assertEqual(["verdict", "stop"], order)
        self.assertEqual({"outcome": "all-clear"}, self.payload["review_verdict"])


class TerminalVerdictTests(_TerminationCase):
    """Adjudication, and the endings it is allowed to change."""

    def test_a_paused_run_keeps_the_blocker_it_arrived_with(self) -> None:
        payload = dict(self.payload, paused=True, ok=False, reason="blocked")
        adjudication = run_termination._adjudicate_terminal_run(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
        )

        self.assertFalse(adjudication.blocked)
        self.assertEqual("blocked", adjudication.claimed_reason)
        self.assertFalse(payload["terminal_adjudication"]["blocked"])

    def test_a_dry_run_is_not_adjudicated_because_it_claimed_nothing(self) -> None:
        payload = dict(self.payload, ok=True, reason="dry-run")
        adjudication = run_termination._adjudicate_terminal_run(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
        )

        self.assertFalse(adjudication.blocked)
        self.assertEqual("dry-run", run_termination._terminal_outcome(payload))
        self.assertIn("dry-run", run_termination._UNAUDITED_TERMINAL_OUTCOMES)


if __name__ == "__main__":
    unittest.main()
