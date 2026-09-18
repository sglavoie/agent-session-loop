"""One path for every run control and every correction, whoever asked.

Session 7's claim is that "both application modes expose the existing controls
through the same validation and execution paths, including interrupted and
ambiguous redirects". The browser and the AgentMux bridge had each grown a full
copy of this -- two journal reads, two replay orderings, two readings of what an
abort is about to mean -- and they agreed only because someone kept them
agreeing. Four properties add up to the claim, and each has its own class here.

**The rules are portable and there is one set of them.**
:class:`PortableControlRulesTests` holds :mod:`asl_workbench.controls`: the
control-to-queue-command table, which controls can be held to a live turn at
all, what an abort is going to mean, and the narrowing every client-chosen field
goes through. It imports nothing from ASL, so the Pi half stays loadable and the
two surfaces cannot be given two spellings of one rule.

**One service checks, queues, and wakes.** :class:`ControlDispatchTests` drives
:mod:`agent_session_loop.workbench_controls` directly. The interesting part is
what is deliberately *not* unified: an open caller may queue a control for a
plan whose runner has not started, and a strict caller may not, because those
are two correct answers for two callers rather than two implementations.

**A correction is judged against the journal, in one order.**
:class:`CorrectionTests` holds the replay-before-freshness ordering that is the
only reason a correction may be retried at all, the conflict that refuses an
edited retry, and the two states that have no clean answer: a correction
persisted and unanswered, and one delivered to an attempt that never receipted
it. Neither is resolved by guessing, on either surface.

**Both surfaces are the same path.** :class:`SurfaceParityTests` drives the HTTP
route and the bridge method over one corpus and asserts they land in the same
queue and the same journal: a correction submitted through the browser replays
for the connector under the id the client already holds, and an interrupted one
is reported at the same stage by both reads.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    bridge,
    control_cli,
    control_queue,
    intervention,
    plans,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store,
    workbench_controls,
    workbench_server,
)
from asl_workbench import controls

from .support import (
    _http_request,
    _write_lock_file,
    _write_state_payload,
    assert_no_runner_import,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "20260619T140000Z-1"
SESSION_ID = "9"
OPERATION_ID = "wbc-op-3d17aa"
FEEDBACK = "stop and re-read the session contract before touching the journal"
EDITED_FEEDBACK = "stop, re-read the session contract, and then revert the journal"
#: A pid this host is not running, so the journal's owner reads as gone and the
#: interrupted statuses become reachable without killing anything real.
DEAD_PID = 4_242_424
#: A path no filesystem can be asked about, which is how a read of the plan
#: itself is made to fail without depending on permissions or on a corpus the
#: readers deliberately tolerate.
UNRESOLVABLE_PLAN_DIR = "plans/\x00/plan-alpha"
#: The pause that hands a person a one-use authorization to start over.
RECOVERY_PAUSE_REASON = "conversation-recovery-required"


class PortableControlRulesTests(unittest.TestCase):
    """What a control means, decided without importing ASL."""

    def test_the_rules_module_never_reaches_the_worker_runtime(self) -> None:
        assert_no_runner_import(self, controls)

    def test_every_control_names_exactly_one_queue_command(self) -> None:
        commands = [controls.queue_command_for(kind) for kind in controls.CONTROL_KINDS]

        self.assertEqual(len(controls.CONTROL_KINDS), len(set(commands)))
        for kind, command in zip(controls.CONTROL_KINDS, commands, strict=True):
            with self.subTest(kind=kind):
                self.assertEqual(controls.QUEUE_COMMAND_FOR_CONTROL[kind], command)

    def test_and_every_one_of_them_is_a_command_asls_queue_accepts(self) -> None:
        """The mapping is portable because it has to be readable on a Pi, which
        is exactly why it is checked against the consumer's own closed set: a
        portable name that drifted would otherwise reach the queue as a line
        nothing consumes.
        """
        for command in controls.QUEUE_COMMAND_FOR_CONTROL.values():
            with self.subTest(command=command):
                self.assertIn(command, control_queue.COMMAND_NAMES)

    def test_an_unknown_control_is_refused_before_anything_is_read(self) -> None:
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.queue_command_for("run.detonate")

        self.assertEqual(controls.REASON_INVALID_CONTROL, caught.exception.reason)

    def test_only_the_controls_a_live_turn_owns_have_an_asl_action(self) -> None:
        """Harness selection is about the *next* run, so it has no turn to be
        fresh about. Giving it one would invent an authorization ASL does not
        define; leaving it out is what makes the strict path refuse it by name.
        """
        self.assertIsNone(controls.asl_action_for(controls.CONTROL_SELECT_HARNESS))
        self.assertEqual(
            intervention.ACTION_STOP_AFTER_CURRENT,
            controls.asl_action_for(controls.CONTROL_STOP_AFTER_CURRENT),
        )
        self.assertEqual(
            intervention.ACTION_ABORT, controls.asl_action_for(controls.CONTROL_ABORT)
        )

    def test_an_abort_note_is_decided_by_the_two_facts_that_decide_it(self) -> None:
        self.assertEqual(
            controls.ABORT_NOTE_NO_RUNNER,
            controls.abort_note_code(runner_present=False, turn_active=False),
        )
        self.assertEqual(
            controls.ABORT_NOTE_WILL_APPLY,
            controls.abort_note_code(runner_present=True, turn_active=True),
        )
        self.assertEqual(
            controls.ABORT_NOTE_DEGRADES,
            controls.abort_note_code(runner_present=True, turn_active=False),
        )

    def test_every_note_code_has_asl_s_own_sentence_behind_it(self) -> None:
        """The codes are portable and the wording is ASL's, so an operator who
        aborts from a browser is told what the terminal would have told them.
        """
        for code in controls.ABORT_NOTE_CODES:
            with self.subTest(code=code):
                self.assertIn(code, workbench_controls.ABORT_NOTES)
        self.assertEqual(
            control_cli.ABORT_WILL_APPLY_NOTE,
            workbench_controls.ABORT_NOTES[controls.ABORT_NOTE_WILL_APPLY],
        )

    def test_an_authorization_policy_outside_the_closed_set_is_refused(self) -> None:
        for policy in controls.AUTHORIZATION_POLICIES:
            with self.subTest(policy=policy):
                controls.require_authorization(policy)
        with self.assertRaises(controls.ControlRequestError):
            controls.require_authorization("lenient")

    def test_whitespace_is_not_a_correction(self) -> None:
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.require_correction_text("   \n  ")

        self.assertEqual(controls.REASON_EMPTY_TEXT, caught.exception.reason)
        self.assertEqual("stay put", controls.require_correction_text("stay put"))

    def test_a_named_turn_is_checked_for_shape_and_nothing_more(self) -> None:
        fields = controls.expected_target_fields(
            {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "harness": "claude",
            },
            label="expected_target",
        )

        self.assertEqual(sorted(controls.EXPECTED_TARGET_FIELDS), sorted(fields))
        self.assertEqual(1, fields["turn_index"])

    def test_a_boolean_turn_index_is_not_an_integer(self) -> None:
        """``True`` is an ``int`` in Python and must not be read as one here."""
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.expected_target_fields(
                {"run_id": RUN_ID, "session_id": SESSION_ID, "turn_index": True},
                label="params.expected_target",
            )

        self.assertIn("params.expected_target.turn_index", caught.exception.message)

    def test_a_named_turn_that_is_not_an_object_is_refused(self) -> None:
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.expected_target_fields("session-9", label="expected_target")

        self.assertEqual(controls.REASON_INVALID_REQUEST, caught.exception.reason)

    def test_a_wait_is_bounded_by_the_ceiling_it_is_given(self) -> None:
        self.assertEqual(
            2.5,
            controls.bounded_wait_seconds(
                2.5, default=1.0, maximum=30.0, label="wait_seconds"
            ),
        )
        self.assertEqual(
            1.0,
            controls.bounded_wait_seconds(
                None, default=1.0, maximum=30.0, label="wait_seconds"
            ),
        )
        for raw in (True, "5", -1.0, 31.0):
            with (
                self.subTest(raw=raw),
                self.assertRaises(controls.ControlRequestError),
            ):
                controls.bounded_wait_seconds(
                    raw, default=1.0, maximum=30.0, label="wait_seconds"
                )

    def test_a_projection_window_truncates_the_oldest(self) -> None:
        """An operator asking where a correction got to is asking about a recent
        one, so the window keeps the tail rather than the head.
        """
        window = controls.newest_operations(
            list(range(controls.MAX_REDIRECT_OPERATIONS + 5))
        )

        self.assertEqual(controls.MAX_REDIRECT_OPERATIONS, len(window))
        self.assertEqual(controls.MAX_REDIRECT_OPERATIONS + 4, window[-1])
        self.assertEqual(5, window[0])

    def test_both_surfaces_take_their_ceiling_from_here(self) -> None:
        self.assertEqual(
            controls.MAX_REDIRECT_OPERATIONS, bridge.MAX_REDIRECT_OPERATIONS
        )
        self.assertEqual(
            controls.MAX_REDIRECT_OPERATIONS, workbench_server.MAX_REDIRECT_OPERATIONS
        )


class ControlsTestCase(unittest.TestCase):
    """A plan on disk, with a runner as live or as absent as a test needs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plans_root = root / "plans"
        self.plans_root.mkdir()
        self.plan_dir = self.plans_root / "plan-alpha"
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.registry_path = root / "workbench.json"
        write_plan(self.plan_dir, ("pending",))
        self.canonical_plan_dir = plans.canonicalize_path(self.plan_dir)
        self.wakeup: threading.Event | None = None
        self._server: workbench_server.WorkbenchHTTPServer | None = None
        self.snapshots: list[dict[str, Any]] = []

    # -- corpus -----------------------------------------------------------

    def write_state(
        self,
        *,
        run_id: str | None = RUN_ID,
        turn_index: int | None = 1,
        paused: bool = False,
        pause_reason: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "version": 3,
            "plan_dir": str(self.plan_dir),
            "paused": paused,
            "harness": "claude",
            "run_started_at": "2026-06-19T14:00:00+00:00",
        }
        if pause_reason is not None:
            payload["reason"] = pause_reason
            payload["pause_reason"] = pause_reason
        if run_id is not None:
            payload["run_id"] = run_id
        if run_id is not None and turn_index is not None:
            payload["active_turn"] = {
                "run_id": run_id,
                "session_id": SESSION_ID,
                "turn_index": turn_index,
                "session_kind": "implementation",
                "harness": "claude",
            }
        _write_state_payload(self.plan_dir, self.state_dir, payload)

    def write_lock(self, **kwargs: Any) -> None:
        params: dict[str, Any] = {"pid": os.getpid(), "hostname": socket.gethostname()}
        params.update(kwargs)
        _write_lock_file(self.plan_dir, self.state_dir, **params)

    def serve_redirects(self) -> Path:
        endpoint, self.wakeup = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        self.write_lock(redirect_endpoint=endpoint)
        return endpoint

    def live_run(self, **state_kwargs: Any) -> Path:
        """A run that is live, holds a turn, and answers the urgent channel."""
        self.write_state(**state_kwargs)
        return self.serve_redirects()

    def target(self, *, turn_index: int = 1) -> redirect_store.RedirectTarget:
        return redirect_store.RedirectTarget(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=turn_index,
            harness="claude",
        )

    def journal(self) -> redirect_store.RedirectJournalView:
        return redirect_store.read_journal(self.plan_dir, state_dir=self.state_dir)

    def queued(self) -> control_queue.DrainResult:
        return control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)

    def facts(self) -> intervention.RunFacts:
        return intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)

    def action_token(self) -> str:
        return intervention.action_token(self.facts())

    def accept_when_pending(
        self, target: redirect_store.RedirectTarget | None = None
    ) -> threading.Thread:
        """Play the runner: record acceptance while the submit wait is running."""
        wanted = self.target() if target is None else target

        def accept() -> None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                pending = self.journal().pending()
                if pending:
                    redirect_store.accept_request(
                        self.plan_dir,
                        pending[0].operation_id,
                        target=wanted,
                        state_dir=self.state_dir,
                    )
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        return thread

    def submit(self, **overrides: Any) -> workbench_controls.RedirectSubmission:
        """One correction through the shared service, carrying a fresh proof.

        A correction is the one control that is held to the turn it interrupts
        under every authorization, so the token is part of the default request
        rather than something only the strict tests supply.
        """
        params: dict[str, Any] = {
            "operation_id": OPERATION_ID,
            "text": FEEDBACK,
            "state_dir": self.state_dir,
            "wait_seconds_raw": 0.05,
            "action_token": self.action_token(),
        }
        params.update(overrides)
        return workbench_controls.submit_correction(self.plan_dir, **params)

    # -- surfaces ---------------------------------------------------------

    def server(self) -> workbench_server.WorkbenchHTTPServer:
        if self._server is None:
            server = workbench_server.create_server(
                host="127.0.0.1",
                port=0,
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                plan_roots=(),
                include_default_dirs=False,
                token=None,
            )
            thread = threading.Thread(
                target=server.serve_forever, name="workbench-controls-test", daemon=True
            )
            thread.start()

            def _shutdown() -> None:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.addCleanup(_shutdown)
            self._server = server
        return self._server

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict, dict]:
        active = self.server()
        return _http_request(
            f"http://{active.bound_host}:{active.bound_port}{path}",
            method="POST",
            body=body,
            headers={"Authorization": f"Bearer {active.token}"},
            timeout=30.0,
        )

    def get(self, path: str) -> tuple[int, dict, dict]:
        active = self.server()
        return _http_request(
            f"http://{active.bound_host}:{active.bound_port}{path}", timeout=30.0
        )

    def bridge_session(self) -> bridge.BridgeSession:
        self.snapshots.append(
            bridge.build_runs_snapshot(
                state_dir=self.state_dir,
                plan_roots=(self.plans_root,),
                include_default_dirs=False,
            )
        )
        return bridge.BridgeSession(
            snapshot_builder=lambda: self.snapshots[-1], state_dir=self.state_dir
        )

    def handshaken(self) -> bridge.BridgeSession:
        session = self.bridge_session()
        response = self.frame(
            session,
            bridge.METHOD_HANDSHAKE,
            {
                "contract_set_id": bridge.CONTRACT_SET_ID,
                "snapshot_digest": self.snapshots[-1]["digest"],
            },
        )
        assert response["result"]["mutations_enabled"], response
        return session

    def frame(
        self,
        session: bridge.BridgeSession,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str = "req-1",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "request",
            "protocol": bridge.PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        response = session.handle_frame(bridge.encode_frame(payload).rstrip(b"\n"))
        assert response is not None
        return response


class ControlDispatchTests(ControlsTestCase):
    """Check, queue, and wake -- once, for whichever surface asked."""

    def test_each_control_queues_the_command_asl_s_own_cli_queues(self) -> None:
        self.live_run()

        for kind in controls.CONTROL_KINDS:
            with self.subTest(kind=kind):
                outcome = workbench_controls.dispatch_control(
                    self.plan_dir,
                    kind,
                    state_dir=self.state_dir,
                    command_id=f"cmd-{kind}",
                    harness="codex",
                )
                self.assertEqual(
                    controls.queue_command_for(kind), outcome.command.command
                )
                drained = self.queued()
                self.assertEqual((f"cmd-{kind}",), drained.consumed_ids)
                self.assertEqual((), drained.rejected)

    def test_a_queued_control_wakes_the_runner_it_was_queued_for(self) -> None:
        self.live_run()

        outcome = workbench_controls.dispatch_control(
            self.plan_dir,
            controls.CONTROL_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="cmd-wake",
        )

        self.assertEqual(
            control_queue.queue_path_for_plan(self.plan_dir, self.state_dir),
            outcome.dispatch.queue_path,
        )

    def test_an_open_caller_may_queue_for_a_runner_that_has_not_started(self) -> None:
        """The CLI has always allowed this, and the runner is still the
        authority: the command waits in the queue for whoever picks the plan up.
        """
        outcome = workbench_controls.dispatch_control(
            self.plan_dir,
            controls.CONTROL_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="cmd-early",
        )

        self.assertFalse(outcome.activity.runner_present)
        self.assertEqual(controls.ABORT_NOTE_NO_RUNNER, outcome.activity.note_code)
        self.assertEqual(("cmd-early",), self.queued().consumed_ids)

    def test_a_strict_caller_may_not(self) -> None:
        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_STOP_AFTER_CURRENT,
                state_dir=self.state_dir,
                command_id="cmd-strict",
                authorization=controls.AUTHORIZATION_STRICT,
            )

        self.assertEqual(intervention.REASON_NO_ACTIVE_RUN, caught.exception.reason)
        self.assertEqual((), self.queued().consumed_ids)

    def test_a_strict_caller_carrying_a_stale_proof_queues_nothing(self) -> None:
        self.live_run()
        stale = self.action_token()
        self.write_state(turn_index=None)

        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_ABORT,
                state_dir=self.state_dir,
                command_id="cmd-stale",
                authorization=controls.AUTHORIZATION_STRICT,
                action_token=stale,
                grace_seconds=control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            )

        self.assertEqual(
            intervention.REASON_STALE_ACTION_TOKEN, caught.exception.reason
        )
        self.assertEqual(controls.SCOPE_INTERVENTION, caught.exception.scope)
        self.assertEqual((), self.queued().consumed_ids)

    def test_an_abort_reports_what_it_is_about_to_mean(self) -> None:
        """Appending to a durable queue always succeeds, so the honest part of
        the answer is the run it is about to reach.
        """
        self.live_run()

        outcome = workbench_controls.dispatch_control(
            self.plan_dir,
            controls.CONTROL_ABORT,
            state_dir=self.state_dir,
            command_id="cmd-abort",
            grace_seconds=control_queue.DEFAULT_ABORT_GRACE_SECONDS,
        )

        self.assertTrue(outcome.activity.runner_present)
        self.assertTrue(outcome.activity.turn_active)
        self.assertEqual(control_cli.ABORT_WILL_APPLY_NOTE, outcome.activity.note)

    def test_an_abort_against_a_runner_holding_no_turn_says_so(self) -> None:
        self.write_state(turn_index=None)
        self.write_lock()

        activity = workbench_controls.inspect_activity(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertTrue(activity.runner_present)
        self.assertFalse(activity.turn_active)
        self.assertEqual(control_cli.ABORT_DEGRADES_NOTE, activity.note)

    def test_a_plan_with_no_state_at_all_is_simply_not_running(self) -> None:
        """No lock, no state file: "nothing is running" is the true answer, and
        it is the one a local operator queuing ahead of a runner has always got.
        """
        activity = workbench_controls.inspect_activity(
            self.plans_root / "not-a-plan", state_dir=self.state_dir
        )

        self.assertFalse(activity.runner_present)
        self.assertEqual(control_cli.NO_ACTIVE_RUN_NOTE, activity.note)

    def test_a_plan_path_that_cannot_be_resolved_is_no_runner_to_an_open_caller(
        self,
    ) -> None:
        activity = workbench_controls.inspect_activity(
            UNRESOLVABLE_PLAN_DIR, state_dir=self.state_dir
        )

        self.assertIsNone(activity.facts)
        self.assertEqual(control_cli.NO_ACTIVE_RUN_NOTE, activity.note)

    def test_and_a_refusal_to_a_strict_one(self) -> None:
        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.inspect_activity(
                UNRESOLVABLE_PLAN_DIR, state_dir=self.state_dir, strict=True
            )

        self.assertEqual(intervention.REASON_INVALID_PLAN, caught.exception.reason)
        self.assertEqual(controls.SCOPE_INTERVENTION, caught.exception.scope)

    def test_an_unsupported_harness_is_refused_before_anything_is_queued(self) -> None:
        self.live_run()

        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_SELECT_HARNESS,
                state_dir=self.state_dir,
                command_id="cmd-harness",
                harness="telepathy",
            )

        self.assertEqual(controls.REASON_INVALID_HARNESS, caught.exception.reason)
        self.assertEqual((), self.queued().consumed_ids)

    def test_a_grace_the_queue_rejects_names_the_field(self) -> None:
        """The queue answers with a code; a client needs the field back."""
        self.live_run()

        with self.assertRaises(workbench_controls.ControlQueueRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_ABORT,
                state_dir=self.state_dir,
                command_id="cmd-grace",
                grace_seconds=0,
            )

        self.assertEqual(control_queue.REJECT_INVALID_GRACE, caught.exception.reason)
        self.assertIn("grace_seconds", caught.exception.message)
        self.assertEqual((), self.queued().consumed_ids)


class CorrectionTests(ControlsTestCase):
    """What one correction establishes, and what it refuses to guess."""

    def test_a_correction_reaches_the_journal_and_the_receipt_is_read_back(
        self,
    ) -> None:
        self.live_run()
        self.accept_when_pending()

        submission = self.submit(wait_seconds_raw=5.0)

        self.assertEqual(OPERATION_ID, submission.operation_id)
        self.assertTrue(submission.persisted)
        self.assertFalse(submission.replayed)
        self.assertIsNotNone(submission.operation)
        payload = submission.operation_payload()
        assert payload is not None
        self.assertEqual(OPERATION_ID, payload["operation_id"])
        recorded = tuple(item.operation_id for item in self.journal().requests)
        self.assertEqual((OPERATION_ID,), recorded)

    def test_a_receipt_carries_a_digest_and_never_the_words(self) -> None:
        self.live_run()
        self.accept_when_pending()

        submission = self.submit(wait_seconds_raw=5.0)

        payload = submission.operation_payload()
        assert payload is not None
        self.assertEqual(len(FEEDBACK.encode("utf-8")), payload["feedback_bytes"])
        self.assertNotIn(FEEDBACK, repr(payload))

    def test_a_retry_replays_the_durable_record_and_appends_nothing(self) -> None:
        self.live_run()
        self.accept_when_pending()
        first = self.submit(wait_seconds_raw=5.0)

        second = self.submit()

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        # A replay submitted nothing, so it has no outcome of its own; the
        # durable operation carries the truth instead.
        self.assertIsNone(second.outcome)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(1, len(self.journal().requests))

    def test_a_replay_is_judged_before_the_freshness_proof_is_checked(self) -> None:
        """The correction that landed is exactly what made the proof stale, so
        asking the journal first is the only thing that makes a retry possible.
        """
        self.live_run()
        self.accept_when_pending()
        stale = self.action_token()
        self.submit(wait_seconds_raw=5.0, action_token=stale)
        self.write_state(paused=True, pause_reason="verify-failed")

        replay = self.submit(
            authorization=controls.AUTHORIZATION_STRICT, action_token=stale
        )

        self.assertTrue(replay.replayed)
        self.assertEqual(1, len(self.journal().requests))

    def test_an_edited_retry_is_a_conflict_rather_than_a_second_correction(
        self,
    ) -> None:
        """An operator whose response was lost still has an editable field.
        Reporting their edited words as a successful replay of the words the
        worker actually got is the one falsehood this path exists to avoid.
        """
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds_raw=5.0)

        with self.assertRaises(workbench_controls.RedirectConflict) as caught:
            self.submit(text=EDITED_FEEDBACK)

        self.assertEqual(redirect_store.CONFLICT_FEEDBACK, caught.exception.conflict)
        self.assertEqual(redirect_store.ERROR_REPLAY_CONFLICT, caught.exception.reason)
        self.assertEqual(controls.SCOPE_CONFLICT, caught.exception.scope)
        # Nothing was written and the recorded request is untouched, which is
        # why the refusal still carries the receipt for the one on file.
        self.assertEqual(1, len(self.journal().requests))
        self.assertTrue(caught.exception.submission.replayed)

    def test_a_retry_naming_a_different_turn_is_the_same_conflict(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds_raw=5.0)

        with self.assertRaises(workbench_controls.RedirectConflict) as caught:
            self.submit(
                expected_target_raw={
                    "run_id": RUN_ID,
                    "session_id": SESSION_ID,
                    "turn_index": 7,
                    "harness": "claude",
                }
            )

        self.assertEqual(redirect_store.CONFLICT_TARGET, caught.exception.conflict)

    def test_a_turn_that_moved_is_a_refusal_the_receipt_reports(self) -> None:
        """The race a composer actually loses: the turn ended while they typed.

        Admission answers rather than raising, because the answer is about the
        run rather than about the request -- and both surfaces render the same
        receipt out of it, which is how a client learns the turn it lost.
        """
        self.live_run()

        submission = self.submit(
            expected_target_raw={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 4,
                "harness": "claude",
            }
        )

        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, submission.reason)
        self.assertFalse(submission.persisted)
        self.assertEqual((), self.journal().requests)

    def test_a_field_the_journal_cannot_store_is_the_clients_error(self) -> None:
        """One exception covers "this is not a correction" and "this journal
        cannot be written", so the refusal is split on the journal's own reason:
        a cap a client chose past is request-shaped and not worth retrying, and
        each surface turns that into a 400 rather than a server fault.
        """
        self.live_run()
        oversized = "o" * (redirect_store.REDIRECT_MAX_OPERATION_ID_CHARS + 1)

        with self.assertRaises(workbench_controls.RedirectSubmitFailed) as caught:
            self.submit(operation_id=oversized)

        self.assertEqual(
            redirect_store.ERROR_INVALID_OPERATION_ID, caught.exception.reason
        )
        self.assertEqual(controls.SOURCE_JOURNAL, caught.exception.source)
        self.assertTrue(caught.exception.request_shaped)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.submitted_message, caught.exception.message)
        self.assertEqual((), self.journal().requests)

    def test_an_urgent_channel_nobody_is_listening_on_persists_nothing(self) -> None:
        """A lock naming no endpoint: the correction has nowhere to be accepted,
        so it is refused rather than left in the journal for a reader to find.
        """
        self.write_state()
        self.write_lock()

        submission = self.submit()

        self.assertEqual(
            redirect_admission.REFUSE_URGENT_CHANNEL_UNAVAILABLE, submission.reason
        )
        self.assertFalse(submission.persisted)
        self.assertFalse(submission.accepted)

    def test_a_refusal_that_persisted_nothing_names_no_operation(self) -> None:
        """Echoing the client's own id for a correction that never reached the
        journal would invite it to ask after one that does not exist.
        """
        self.live_run()

        submission = self.submit(
            expected_target_raw={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 4,
                "harness": "claude",
            }
        )

        self.assertIsNone(submission.operation_id)
        self.assertIsNone(submission.operation_payload())
        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), view.operations)

    def test_whitespace_is_refused_by_the_shared_service_too(self) -> None:
        self.live_run()

        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            self.submit(text="  \n ")

        self.assertEqual(controls.REASON_EMPTY_TEXT, caught.exception.reason)
        self.assertEqual((), self.journal().requests)

    def test_a_correction_nothing_answered_is_reported_unfinished(self) -> None:
        """Persisted, unanswered, and the run that would have taken it is gone:
        one of the seven boundaries, and none of them is resolved by guessing.
        """
        self.live_run()
        self.submit()
        self.write_lock(pid=DEAD_PID, redirect_endpoint=None)

        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)

        payload = view.operations[0]
        self.assertEqual(redirect_recovery.STATUS_UNANSWERED, payload.status)
        self.assertEqual(
            redirect_cli.STAGE_UNACCEPTED,
            redirect_cli.operation_payload(payload, live_target=view.live_target)[
                "stage"
            ],
        )

    def test_a_correction_delivered_without_a_receipt_is_uncertain(self) -> None:
        """Handed to an attempt that never accounted for it. Whether a model
        read the words is the question the acknowledgment protocol exists
        because nothing cheaper answers, so this declines to answer it.
        """
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds_raw=5.0)
        redirect_store.carry_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        redirect_store.deliver_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=DEAD_PID)

        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)

        payload = view.operation_payloads()[0]
        self.assertEqual(redirect_recovery.STATUS_UNCERTAIN, payload["status"])
        self.assertEqual(redirect_cli.STAGE_UNCERTAIN, payload["stage"])

    def test_an_unfinished_correction_is_what_a_fresh_start_would_inherit(self) -> None:
        """An operator authorizing a relaunch sees what it carries here, rather
        than discovering it in the successor's first prompt.
        """
        self.live_run()
        self.submit()
        self.write_state(paused=True, pause_reason=RECOVERY_PAUSE_REASON)
        self.write_lock(pid=DEAD_PID, redirect_endpoint=None)

        view = workbench_controls.recovery_view(self.plan_dir, state_dir=self.state_dir)

        self.assertTrue(view.decision.required)
        self.assertEqual(
            (OPERATION_ID,), tuple(item.operation_id for item in view.retained)
        )
        self.assertTrue(view.one_use)
        self.assertEqual(redirect_cli.FRESH_CONVERSATION_NOTE, view.note)

    def test_a_recovery_read_is_only_a_read(self) -> None:
        """Performing it here would persist a one-use authorization in the one
        place it must never live: a request that can be replayed.
        """
        self.live_run(paused=True, pause_reason=RECOVERY_PAUSE_REASON)
        before = self.journal().requests

        workbench_controls.recovery_view(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(before, self.journal().requests)
        self.assertEqual((), self.queued().consumed_ids)

    def test_a_status_read_hands_out_the_proof_a_correction_must_carry_back(
        self,
    ) -> None:
        """The client most in need of this read is one whose proof is stale, so
        the read is unauthorized and mints the fresh one.
        """
        self.live_run()

        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(self.action_token(), intervention.action_token(view.facts))

    def test_a_status_read_filtered_to_an_absent_id_reports_nothing(self) -> None:
        self.live_run()
        self.submit()

        view = workbench_controls.status_view(
            self.plan_dir, state_dir=self.state_dir, operation_ids=("wbc-op-absent",)
        )

        self.assertEqual((), view.operations)
        self.assertFalse(view.truncated)

    def test_a_plan_that_cannot_be_read_refuses_rather_than_reporting_nothing(
        self,
    ) -> None:
        """Answering as though a plan held no corrections is the one wrong
        answer a client cannot detect.
        """
        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.status_view(
                UNRESOLVABLE_PLAN_DIR, state_dir=self.state_dir
            )

        self.assertEqual(intervention.REASON_INVALID_PLAN, caught.exception.reason)


class SurfaceParityTests(ControlsTestCase):
    """The browser and the connector reach the same queue and the same journal."""

    def http_redirect(self, **overrides: Any) -> tuple[int, dict, dict]:
        body: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": OPERATION_ID,
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        body.update(overrides)
        return self.post("/api/control/redirect", body)

    def bridge_redirect(
        self, session: bridge.BridgeSession | None = None, **overrides: Any
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": OPERATION_ID,
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        params.update(overrides)
        return self.frame(
            self.handshaken() if session is None else session,
            intervention.ACTION_REDIRECT,
            params,
            request_id="req-redirect",
        )

    def test_both_surfaces_queue_the_same_stop_command(self) -> None:
        self.live_run()

        status, body, _ = self.post(
            "/api/control/stop-after-current",
            {"plan_dir": str(self.plan_dir), "action_token": self.action_token()},
        )
        self.assertEqual(202, status, body)
        http_command = self.queued().accepted[0].command

        self.frame(
            self.handshaken(),
            intervention.ACTION_STOP_AFTER_CURRENT,
            {
                "plan_dir": str(self.plan_dir),
                "operation_id": "amx-op-stop",
                "action_token": self.action_token(),
            },
            request_id="req-stop",
        )
        bridge_command = self.queued().accepted[0].command

        self.assertEqual(http_command, bridge_command)
        self.assertEqual(
            controls.queue_command_for(controls.CONTROL_STOP_AFTER_CURRENT),
            bridge_command,
        )

    def test_both_surfaces_report_the_same_abort_note(self) -> None:
        self.live_run()

        status, body, _ = self.post(
            "/api/control/abort",
            {"plan_dir": str(self.plan_dir), "action_token": self.action_token()},
        )
        self.assertEqual(202, status, body)

        activity = workbench_controls.inspect_activity(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual(activity.note, body["note"])
        self.assertEqual(control_cli.ABORT_WILL_APPLY_NOTE, body["note"])

    def test_a_correction_submitted_in_the_browser_replays_for_the_connector(
        self,
    ) -> None:
        """The journal is the receipt, so a client that reconnects to the other
        surface entirely still learns what became of what it submitted.
        """
        self.live_run()
        self.accept_when_pending()
        status, body, _ = self.http_redirect(wait_seconds=5.0)
        self.assertEqual(202, status, body)

        response = self.bridge_redirect()

        result = response["result"]
        self.assertTrue(result["replayed"])
        self.assertEqual(OPERATION_ID, result["operation_id"])
        self.assertEqual(1, len(self.journal().requests))

    def test_an_edited_retry_is_refused_by_both_with_the_same_durable_reason(
        self,
    ) -> None:
        self.live_run()
        self.accept_when_pending()
        self.assertEqual(202, self.http_redirect(wait_seconds=5.0)[0])

        http_status, http_body, _ = self.http_redirect(text=EDITED_FEEDBACK)
        bridge_response = self.bridge_redirect(text=EDITED_FEEDBACK)

        self.assertEqual(409, http_status)
        self.assertEqual(redirect_store.ERROR_REPLAY_CONFLICT, http_body["reason"])
        self.assertEqual(
            redirect_store.ERROR_REPLAY_CONFLICT,
            bridge_response["error"]["details"]["reason"],
        )
        self.assertEqual(
            redirect_store.CONFLICT_FEEDBACK,
            bridge_response["error"]["details"]["conflict"],
        )
        self.assertEqual(1, len(self.journal().requests))

    def test_a_wait_above_the_ceiling_is_refused_by_both(self) -> None:
        self.live_run()
        over = redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS + 1

        http_status, _, _ = self.http_redirect(wait_seconds=over)
        bridge_response = self.bridge_redirect(wait_seconds=over)

        self.assertEqual(400, http_status)
        self.assertEqual(bridge.ERROR_INVALID_REQUEST, bridge_response["error"]["code"])
        self.assertEqual((), self.journal().requests)

    def test_both_reads_report_an_interrupted_correction_at_the_same_stage(
        self,
    ) -> None:
        self.live_run()
        self.accept_when_pending()
        self.assertEqual(202, self.http_redirect(wait_seconds=5.0)[0])
        redirect_store.carry_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        redirect_store.deliver_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=DEAD_PID)

        status, http_body, _ = self.get(
            f"/api/redirect-status?plan_dir={self.plan_dir}"
        )
        bridge_body = self.frame(
            self.bridge_session(),
            bridge.METHOD_REDIRECT_STATUS,
            {"plan_dir": str(self.plan_dir)},
            request_id="req-status",
        )["result"]

        self.assertEqual(200, status, http_body)
        self.assertEqual(
            redirect_cli.STAGE_UNCERTAIN, http_body["operations"][0]["stage"]
        )
        self.assertEqual(
            redirect_cli.STAGE_UNCERTAIN, bridge_body["operations"][0]["stage"]
        )
        self.assertEqual([OPERATION_ID], bridge_body["uncertain_operation_ids"])

    def test_both_recovery_reads_name_the_same_decision_and_carries(self) -> None:
        self.live_run()
        self.submit()
        self.write_state(paused=True, pause_reason=RECOVERY_PAUSE_REASON)
        self.write_lock(pid=DEAD_PID, redirect_endpoint=None)

        status, http_body, _ = self.get(
            f"/api/conversation-recovery?plan_dir={self.plan_dir}"
        )
        bridge_body = self.frame(
            self.bridge_session(),
            bridge.METHOD_RECOVER_CONVERSATION,
            {"plan_dir": str(self.plan_dir)},
            request_id="req-recovery",
        )["result"]

        self.assertEqual(200, status, http_body)
        self.assertEqual(http_body["decision"], bridge_body["decision"])
        self.assertEqual(http_body["one_use"], bridge_body["one_use"])
        self.assertEqual(
            [OPERATION_ID],
            [item["operation_id"] for item in bridge_body["retained_operations"]],
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
