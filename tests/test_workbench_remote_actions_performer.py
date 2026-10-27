"""Every retained control routed through the real performer, plus bulk archive."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    intervention,
    mailbox_appenders,
    state,
    workbench_archive,
    workbench_controls,
    workbench_launch,
    workbench_operations,
    workbench_remote_actions,
)
from asl_workbench import archive as portable_archive
from asl_workbench import catalog, remote_actions
from asl_workbench.operations import (
    METHOD_ABORT,
    METHOD_LAUNCH,
    METHOD_PLAN_ARCHIVE,
    METHOD_QUESTION_REPLY,
    METHOD_REDIRECT,
    METHOD_REPLY,
    METHOD_SELECT_HARNESS,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_UNLOCK,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_REJECTED,
    REASON_OPERATION_CONFLICT,
)
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _write_lock_file,
)
from tests.support_workbench_remote_actions import (
    ACTION_TOKEN,
    NOW,
    PLAN_KEY,
    REPLY_REF,
    REPLY_TEXT,
    RUN_ID,
    RUN_KEY,
    SESSION_ID,
    PerformerTestCase,
    iso,
)

ASL_TEST_PORTABLE = True


class PerformerTests(PerformerTestCase):
    """Every method reaches the code the browser's own route reaches."""

    def test_a_key_becomes_the_plan_discovery_already_publishes(self) -> None:
        self.live_run()

        result = self.perform(bridge_token=self.token())

        self.assertEqual(OPERATION_ACCEPTED, result.status)
        self.assertEqual(
            [control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT], self.queued()
        )

    def test_an_unknown_key_is_rejected_rather_than_guessed_at(self) -> None:
        result = self.perform(plan_key="f" * 16)

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)
        self.assertEqual([], self.rows())

    def test_all_three_retained_controls_route_to_the_queue(self) -> None:
        cases = {
            METHOD_STOP_AFTER_CURRENT: control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            METHOD_ABORT: control_queue.COMMAND_REQUEST_ABORT_TURN,
            METHOD_SELECT_HARNESS: control_queue.COMMAND_SELECT_FUTURE_HARNESS,
        }
        for index, (method, command) in enumerate(cases.items()):
            with self.subTest(method=method):
                self.live_run()
                result = self.performer.perform(
                    self.request(
                        operation_id=f"op-{index}",
                        method=method,
                        bridge_token=self.token(),
                        harness="codex" if method == METHOD_SELECT_HARNESS else None,
                    ),
                    body=None,
                    now=NOW,
                )
                self.assertEqual(OPERATION_ACCEPTED, result.status)
                self.assertIn(command, self.queued())

    def test_a_control_without_the_proof_is_refused_by_asl_itself(self) -> None:
        """The same strict authorization the browser's own route applies."""
        self.live_run()

        result = self.perform(bridge_token="stale-token")

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)
        self.assertEqual([], self.queued())

    def test_a_refused_control_leaves_a_settled_row_not_an_open_one(self) -> None:
        """An open row would block the next command against the same plan."""
        self.live_run()

        self.perform(bridge_token="stale-token")

        self.assertEqual(OPERATION_REJECTED, self.row()["status"])
        self.assertEqual(
            [],
            workbench_operations.open_operations(
                self.plan_dir, state_dir=self.state_dir
            ),
        )

    def test_intent_is_on_disk_before_the_side_effect(self) -> None:
        """Dispatch-before-effect is the only thing that makes a restart honest."""
        self.live_run()
        seen: list[str] = []
        original = workbench_remote_actions.workbench_controls.dispatch_control

        def watched(*args: Any, **kwargs: Any) -> Any:
            seen.append(self.row()["status"])
            return original(*args, **kwargs)

        workbench_remote_actions.workbench_controls.dispatch_control = watched  # type: ignore[assignment]
        self.addCleanup(
            setattr,
            workbench_remote_actions.workbench_controls,
            "dispatch_control",
            original,
        )

        self.perform(bridge_token=self.token())

        self.assertEqual([OPERATION_DISPATCHED], seen)

    def test_a_replayed_operation_is_never_carried_out_twice(self) -> None:
        self.live_run()
        token = self.token()

        first = self.perform(bridge_token=token)
        second = self.perform(bridge_token=token)

        self.assertEqual(OPERATION_ACCEPTED, first.status)
        self.assertEqual(OPERATION_ACCEPTED, second.status)
        self.assertEqual(
            [control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT], self.queued()
        )

    def test_conflicting_operation_reuse_reports_the_actual_refusal(self) -> None:
        self.live_run()
        token = self.token()
        self.perform(bridge_token=token)

        result = self.perform(method=METHOD_ABORT, bridge_token=token)

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertEqual(REASON_OPERATION_CONFLICT, result.reason)
        self.assertEqual(
            [control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT], self.queued()
        )

    def test_a_second_plan_is_not_blocked_by_the_first_plans_operation(self) -> None:
        """The one-at-a-time rule is per plan, exactly as it is locally."""
        other = self.make_plan("plan-beta")
        self.write_state(other)
        self.live_run()

        self.perform(bridge_token=self.token())
        result = self.performer.perform(
            self.request(
                operation_id="op-2",
                method=METHOD_UNLOCK,
                plan_key=catalog.plan_key(str(other)),
            ),
            body=None,
            now=NOW,
        )

        self.assertIn(result.status, (OPERATION_CONSUMED, OPERATION_REJECTED))

    def test_unlock_is_terminal_and_never_forced(self) -> None:
        self.live_run()
        lock_path = state.plan_state_dir(self.plan_dir, self.state_dir) / (
            state.LOCK_FILE_NAME
        )
        self.assertTrue(lock_path.exists())

        result = self.performer.perform(
            self.request(operation_id="op-u", method=METHOD_UNLOCK),
            body=None,
            now=NOW,
        )

        # A live lock is not taken away from a process that may still hold it.
        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertTrue(lock_path.exists())

    def test_unlock_releases_a_lock_whose_owner_is_gone(self) -> None:
        self.write_state()
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=_WORKBENCH_DEAD_PID,
            hostname=socket.gethostname(),
        )

        result = self.performer.perform(
            self.request(operation_id="op-u", method=METHOD_UNLOCK),
            body=None,
            now=NOW,
        )

        self.assertEqual(OPERATION_CONSUMED, result.status)
        self.assertEqual(OPERATION_CONSUMED, self.row("op-u")["status"])

    def test_a_body_over_the_ceiling_is_refused_whole(self) -> None:
        """A truncated reply is prose a run would act on as if it were complete."""
        self.live_run()

        result = self.performer.perform(
            self.request(
                operation_id="op-r",
                method=METHOD_QUESTION_REPLY,
                reply_ref=REPLY_REF,
                action_token=ACTION_TOKEN,
                bridge_token=self.token(),
            ),
            body="x" * (remote_actions.MAX_BODY_CHARS + 1),
            now=NOW,
        )

        self.assertEqual(OPERATION_REJECTED, result.status)

    def test_a_body_that_was_not_redeemed_is_not_an_empty_reply(self) -> None:
        self.live_run()

        with self.assertRaises(remote_actions.BodyUnavailable):
            self.performer.perform(
                self.request(
                    operation_id="op-r",
                    method=METHOD_QUESTION_REPLY,
                    reply_ref=REPLY_REF,
                    action_token=ACTION_TOKEN,
                    bridge_token=self.token(),
                ),
                body=None,
                now=NOW,
            )

    def test_a_delivered_launch_is_the_launch_the_browser_makes(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self.launch_result()
        ) as launched:
            result = self.performer.perform(
                self.request(operation_id="op-l", method=METHOD_LAUNCH),
                body=None,
                now=NOW,
            )

        launched.assert_called_once()
        # ``consumed``: the process it promised exists, so the effect is done.
        self.assertEqual(OPERATION_CONSUMED, result.status)
        self.assertEqual(OPERATION_CONSUMED, self.row("op-l")["status"])

    def test_a_relaunch_of_the_same_operation_spawns_nothing(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self.launch_result()
        ) as launched:
            first = self.performer.perform(
                self.request(operation_id="op-l", method=METHOD_LAUNCH),
                body=None,
                now=NOW,
            )
            second = self.performer.perform(
                self.request(operation_id="op-l", method=METHOD_LAUNCH),
                body=None,
                now=NOW + 1.0,
            )

        launched.assert_called_once()
        self.assertEqual(first.status, second.status)

    def test_a_delivered_reply_carries_the_redeemed_prose_into_the_run(self) -> None:
        """``accepted``, not ``consumed``: a spawned process is not a reader."""
        self.make_paused()

        with mock.patch.object(
            workbench_launch,
            "resume_plan_with_reply",
            return_value=self.launch_result(reply=True),
        ) as resumed:
            result = self.performer.perform(
                self.request(
                    operation_id="op-p",
                    method=METHOD_REPLY,
                    reply_ref=REPLY_REF,
                    action_token=ACTION_TOKEN,
                ),
                body=REPLY_TEXT,
                now=NOW,
            )

        self.assertEqual(OPERATION_ACCEPTED, result.status)
        self.assertEqual(REPLY_TEXT, resumed.call_args.kwargs["reply_text"])

    def test_a_reply_to_a_plan_with_no_question_open_is_rejected(self) -> None:
        self.live_run()

        result = self.performer.perform(
            self.request(
                operation_id="op-q",
                method=METHOD_QUESTION_REPLY,
                reply_ref=REPLY_REF,
                action_token=ACTION_TOKEN,
                bridge_token=self.token(),
            ),
            body=REPLY_TEXT,
            now=NOW,
        )

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)

    def test_an_accepted_live_reply_reconciles_when_the_runner_consumes_it(
        self,
    ) -> None:
        self.live_run(
            active_turn={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "session_kind": "implementation",
                "harness": "claude",
                "input_wait_active": True,
            }
        )
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "May the fixture continue?",
            state_dir=self.state_dir,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=1,
        )
        request = self.request(
            operation_id="op-live",
            method=METHOD_QUESTION_REPLY,
            reply_ref=REPLY_REF,
            action_token=ACTION_TOKEN,
            bridge_token=self.token(),
            question_id=question.id,
        )

        first = self.performer.perform(request, body=REPLY_TEXT, now=NOW)
        self.assertEqual(OPERATION_ACCEPTED, first.status)
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        dispatched_at = float(self.row("op-live")["dispatched_at"])
        from datetime import UTC, datetime

        event = {
            "created_at": datetime.fromtimestamp(
                dispatched_at + 1.0, tz=UTC
            ).isoformat(),
            "name": events.REPLY_PROVIDED_EVENT,
            "payload": {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "reply_length": len(REPLY_TEXT),
                "source": {
                    "origin": "mailbox",
                    "run_id": RUN_ID,
                    "session_id": SESSION_ID,
                    "turn_index": 1,
                    "question_id": question.id,
                    "reply_length": len(REPLY_TEXT),
                },
            },
            "version": 1,
        }
        with (plan_state / events.EVENTS_FILE_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

        settled = self.performer.reconcile(request, now=dispatched_at + 2.0)

        assert settled is not None
        self.assertEqual(OPERATION_CONSUMED, settled.status)
        self.assertEqual(OPERATION_CONSUMED, self.row("op-live")["status"])

    def test_a_redirect_with_no_endpoint_to_reach_is_rejected(self) -> None:
        """Redirect carries prose too, and is still decided by ASL, not here."""
        self.live_run()

        result = self.performer.perform(
            self.request(
                operation_id="op-x",
                method=METHOD_REDIRECT,
                reply_ref=REPLY_REF,
                action_token=ACTION_TOKEN,
                bridge_token=self.token(),
            ),
            body="go back to the earlier session",
            now=NOW,
        )

        # The lock this fixture writes names no redirect endpoint, so ASL
        # refuses -- with its own word, reached through its own route.
        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)

    def test_unpersisted_redirect_is_rejected_in_the_journal_and_on_replay(
        self,
    ) -> None:
        self.live_run()
        refused = workbench_controls.RedirectSubmission(
            facts=intervention.inspect_run(self.plan_dir, state_dir=self.state_dir),
            operation_id=None,
            operation=None,
            live_target=None,
            outcome="rejected",
            reason="target-changed",
            target=None,
            waited_seconds=0.0,
            replayed=False,
            persisted=False,
            accepted=False,
            journal_complete=True,
        )
        request = self.request(
            operation_id="op-x",
            method=METHOD_REDIRECT,
            reply_ref=REPLY_REF,
            action_token=ACTION_TOKEN,
            bridge_token=self.token(),
        )
        with mock.patch.object(
            workbench_controls, "submit_correction", return_value=refused
        ) as submit:
            first = self.performer.perform(request, body=REPLY_TEXT, now=NOW)
            self.assertEqual(OPERATION_REJECTED, first.status)
            self.assertEqual("target-changed", first.reason)
            self.assertEqual(OPERATION_REJECTED, self.row("op-x")["status"])
            self.assertEqual(
                [],
                workbench_operations.open_operations(
                    self.plan_dir, state_dir=self.state_dir
                ),
            )
            replay = self.performer.perform(request, body=REPLY_TEXT, now=NOW + 1)
            self.assertEqual(OPERATION_REJECTED, replay.status)
            submit.assert_called_once()
            # A new correction must reach ASL, rather than being blocked by
            # the previous refusal's operation row.
            again = self.perform(
                body=REPLY_TEXT,
                operation_id="op-next",
                method=METHOD_REDIRECT,
                bridge_token=self.token(),
            )
            self.assertEqual("target-changed", again.reason)
            self.assertEqual(2, submit.call_count)

    def test_asls_private_refusal_word_stays_on_this_machine(self) -> None:
        self.live_run()
        request = self.request(bridge_token="stale-token")

        result = self.performer.perform(request, body=None, now=NOW)
        payload = remote_actions.operation_payload(request, result, observed_at=NOW)

        self.assertIsNotNone(result.reason)
        self.assertNotIn(str(result.reason), json.dumps(payload))


# --------------------------------------------------------------------------
# Archiving
# --------------------------------------------------------------------------


class BulkArchiveTests(PerformerTestCase):
    """One operation per plan, reassembled into the bulk answer it came from."""

    def archivable(self, name: str) -> Path:
        plan_dir = self.make_plan(name, ("complete",))
        self.write_state(
            plan_dir,
            run_id=None,
            active_turn=None,
            completed_at=iso(-86400),
        )
        return plan_dir

    def test_a_delivered_archive_takes_the_bulk_route(self) -> None:
        plan_dir = self.archivable("plan-old")

        result = self.performer.perform(
            self.request(
                operation_id="op-a",
                method=METHOD_PLAN_ARCHIVE,
                plan_key=catalog.plan_key(str(plan_dir)),
            ),
            body=None,
            now=NOW,
        )

        self.assertEqual(OPERATION_CONSUMED, result.status)
        self.assertFalse(plan_dir.exists())

    def test_a_blocked_archive_is_a_rejection_that_says_why(self) -> None:
        self.live_run()

        result = self.performer.perform(
            self.request(operation_id="op-a", method=METHOD_PLAN_ARCHIVE),
            body=None,
            now=NOW,
        )

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)
        self.assertTrue(self.plan_dir.exists())

    def test_per_entry_results_survive_the_decomposition(self) -> None:
        """A partial success reads the same from either side of the wire."""
        good = self.request(operation_id="op-a", plan_key=PLAN_KEY)
        bad = self.request(operation_id="op-b", plan_key=RUN_KEY)
        outcomes = [
            workbench_remote_actions.bulk_outcome(
                good, remote_actions.MutationResult(status=OPERATION_CONSUMED)
            ),
            workbench_remote_actions.bulk_outcome(
                bad,
                remote_actions.MutationResult(
                    status=OPERATION_REJECTED,
                    reason=portable_archive.REASON_STATE_ERROR,
                ),
            ),
        ]

        bulk = workbench_remote_actions.bulk_archive_result(outcomes)
        payload = bulk.to_payload()

        self.assertEqual(1, payload["archived_count"])
        self.assertEqual(1, payload["failed_count"])
        self.assertEqual(2, len(payload["results"]))
        self.assertEqual(
            portable_archive.REASON_STATE_ERROR, payload["results"][1]["reason"]
        )

    def test_a_batch_report_names_plans_by_key_and_never_by_path(self) -> None:
        outcome = workbench_remote_actions.bulk_outcome(
            self.request(plan_key=self.plan_key),
            remote_actions.MutationResult(status=OPERATION_CONSUMED),
        )
        payload = workbench_remote_actions.bulk_archive_result([outcome]).to_payload()

        self.assertNotIn(str(self.root), json.dumps(payload))
        self.assertEqual(self.plan_key, payload["results"][0]["plan_dir"])

    def test_the_local_bulk_route_and_the_delivered_one_agree_on_shape(self) -> None:
        plan_dir = self.archivable("plan-old")
        local = workbench_archive.archive_many(
            [str(plan_dir)],
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )
        delivered = workbench_remote_actions.bulk_archive_result(
            [
                workbench_remote_actions.bulk_outcome(
                    self.request(plan_key=catalog.plan_key(str(plan_dir))),
                    remote_actions.MutationResult(status=OPERATION_CONSUMED),
                )
            ]
        )

        self.assertEqual(sorted(local.to_payload()), sorted(delivered.to_payload()))
        self.assertEqual(
            local.to_payload()["archived_count"],
            delivered.to_payload()["archived_count"],
        )


# --------------------------------------------------------------------------
# Restarting, against a real journal
# --------------------------------------------------------------------------
