"""What supervision is allowed to decide on its own, and what it must hand back.

The autopilot helper exists to keep a plan moving without a person watching it.
Redirect and conversation recovery are the two places where that goal and the
plan's safety point in opposite directions: both look like an ordinary pause,
and both would be *resolved* by relaunching -- which is exactly the move that
loses an agent's saved reasoning or starts a second worker beside one still
running. So this module pins the boundary from both sides at once, because a
regression on either side reads as the same green suite:

* the ordinary retries still work. ``timeout`` and ``transport-failed`` stay
  eligible, keep the same two-attempt budget, and keep the same 5s/30s backoff,
  since a safety change that quietly stopped retrying transient failures would
  be a supervision loop that no longer supervises;
* the two recovery pauses stop, every time, and spend nothing. They are not in
  ``pause_policy.RECOVERABLE_PAUSE_REASONS``, so no retry budget is consumed
  and none can be exhausted -- a plan that hits one twice is in the same
  position the second time as the first;
* the stop is not silent. A recovery pause surfaces which of the two decisions
  is being handed over, the command that reads its evidence, and the exact
  one-use relaunch that authorizes a fresh conversation when one exists;
* ``--fresh-conversation`` cannot be supervised at all. Two independent layers
  refuse it: the command is rejected when it is parsed, and the flag is dropped
  when child argv is built. One authorization, used once, by the person who
  gave it;
* uncertain evidence stays uncertain. A correction delivered to a worker that
  never receipted it is never carried automatically, and a selection state that
  is not one of the two proceeding states requires a user;
* a stop stays stopped. ``stop-after-current`` and ``abort`` end supervision
  cleanly rather than being relaunched into the work the operator just stopped;
* and urgent redirect is documented for an explicitly authorized supervisor
  without becoming a standing fleet authority.
"""

from __future__ import annotations

import argparse
import itertools
import unittest
from types import ModuleType

from agent_session_loop import (
    conversation_resume,
    conversations,
    events,
    overseer_decisions,
    pause_policy,
    redirect_cli,
    redirect_recovery,
    supervisor_guide,
)
from tests.support import _load_script_module

PLAN_DIR = "/tmp/plan"

PARSED = {"plan_dir": PLAN_DIR, "argv": ["asl", "run", PLAN_DIR, "--json"]}


def _counter_clock():
    """Monotonic-ish clock advancing one unit per call, so no test sleeps."""
    ticks = itertools.count()
    return lambda: float(next(ticks))


def _status_payload(pending, *, lock=None):
    return {
        "ok": True,
        "counts": {"pending": pending, "complete": 0},
        "next_pending_id": None if pending == 0 else "1",
        "lock": lock or {"present": False, "active": False, "stale": False},
        "sessions": [],
        "plan_dir": PLAN_DIR,
    }


def _active_lock():
    return {
        "present": True,
        "active": True,
        "stale": False,
        "plan_dir": PLAN_DIR,
        "reason": "active",
        "created_at": "2026-09-10T12:00:00+00:00",
    }


class _StatusScript:
    """Return scripted status payloads in order, repeating the last one."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, parsed):
        index = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return self.statuses[index]


class _RunScript:
    """Return scripted run payloads and record the chunk indices requested."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.chunks = []

    def __call__(self, parsed, chunk_index):
        self.chunks.append(chunk_index)
        return self.payloads[min(len(self.chunks) - 1, len(self.payloads) - 1)]


def _never_run(parsed, chunk_index):  # pragma: no cover - guard for attach paths
    raise AssertionError("run_invoker must not be called")


def _pause(reason, *, message=None, details=None, policy=None):
    """A paused run payload shaped the way the runner writes one."""
    payload = {
        "ok": False,
        "paused": True,
        "reason": reason,
        "pause_reason": reason,
        "pause_message": message or f"paused: {reason}",
        "pause_policy": policy or pause_policy.classify_pause(reason),
    }
    if details is not None:
        payload["pause_details"] = details
    return payload


def _transport_pause():
    return _pause(
        "transport-failed",
        message="connection reset by peer",
        details={
            "failure_classification": {
                "reason": "transport-failed",
                "retryable": True,
                "fingerprint": "transport-failed:connection-reset",
                "retry_at": None,
            }
        },
    )


def _conversation_recovery_pause(
    *, selection_state="incompatible", mismatched_fields=("root",)
):
    """The pause ``turn_preparation`` writes when no conversation may be resumed."""
    return _pause(
        events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        message="the recorded conversation cannot be resumed",
        details={
            "session": "1",
            "conversation_recovery": {
                "disposition": conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
                "conversation": "conv-1",
                "selection_state": selection_state,
                "mismatched_fields": list(mismatched_fields),
            },
        },
    )


def _successor_blocked_pause(reason="worker-still-alive"):
    """The pause ``turn_dispositions`` writes when a predecessor may still be alive."""
    return _pause(
        events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
        message="the redirected turn could not be confirmed stopped",
        details={
            "session": "1",
            "redirect_continuation": {"reason": reason, "report": None},
        },
    )


class _SupervisionCase(unittest.TestCase):
    """Shared loader and driver for the supervision loop under a fake clock."""

    autopilot: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.autopilot = _load_script_module(
            "asl_autopilot_redirect_supervision",
            "skills",
            "asl",
            "scripts",
            "asl_autopilot.py",
        )

    def _supervise(self, status_invoker, run_invoker, *, sleeps=None, **kwargs):
        return self.autopilot.supervise_run(
            dict(PARSED),
            status_invoker=status_invoker,
            run_invoker=run_invoker,
            clock=_counter_clock(),
            sleep=(lambda seconds: None) if sleeps is None else sleeps.append,
            heartbeat=lambda *_: None,
            **kwargs,
        )


class PreservedRetryPolicyTests(_SupervisionCase):
    """The safety work must not cost the plan its ordinary transient retries."""

    def test_timeout_and_transport_stay_recoverable_with_the_same_budget(self) -> None:
        self.assertIn("timeout", pause_policy.RECOVERABLE_PAUSE_REASONS)
        self.assertIn("transport-failed", pause_policy.RECOVERABLE_PAUSE_REASONS)
        self.assertEqual(2, pause_policy.DEFAULT_MAX_RETRIES)
        self.assertEqual((5.0, 30.0), pause_policy.TRANSPORT_RETRY_DELAYS_SECONDS)
        for reason in ("timeout", "transport-failed"):
            with self.subTest(reason=reason):
                policy = pause_policy.classify_pause(reason)
                self.assertTrue(policy["recoverable"])
                self.assertFalse(policy["requires_user"])
                self.assertEqual(
                    pause_policy.DEFAULT_MAX_RETRIES, policy["max_retries"]
                )

    def test_timeout_retries_twice_without_backing_off(self) -> None:
        # A timeout is retried on the recoverable budget and, unlike a transport
        # failure, relaunches immediately: nothing about it says the provider
        # needs time.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_pause("timeout", message="harness timed out")])
        sleeps: list[float] = []

        result = self._supervise(status, run, sleeps=sleeps, max_retries=2)

        self.assertEqual("paused", result["reason"])
        self.assertEqual([1, 2, 3], run.chunks)
        self.assertEqual([], sleeps)
        self.assertEqual(2, result["retries"]["count"])
        self.assertTrue(result["retries"]["exhausted"])

    def test_transport_retries_still_sleep_five_then_thirty(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_transport_pause()])
        sleeps: list[float] = []

        result = self._supervise(status, run, sleeps=sleeps, max_retries=2)

        self.assertEqual("paused", result["reason"])
        self.assertEqual([1, 2, 3], run.chunks)
        self.assertEqual([5.0, 30.0], sleeps)
        self.assertEqual(35.0, result["retries"]["delayed_seconds"])


class RecoveryPausesSpendNothingTests(_SupervisionCase):
    """Neither recovery pause is retryable, so neither can consume a budget."""

    def test_neither_recovery_reason_is_recoverable(self) -> None:
        for reason in self.autopilot.RECOVERY_DECISION_PAUSE_REASONS:
            with self.subTest(reason=reason):
                policy = pause_policy.classify_pause(reason)
                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual("inspect", policy["user_action"])
                self.assertEqual(0, policy["max_retries"])
                self.assertEqual([], list(policy["backoff_seconds"]))
        self.assertEqual(
            (
                events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
                events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
            ),
            self.autopilot.RECOVERY_DECISION_PAUSE_REASONS,
        )

    def test_recovery_pauses_stop_on_the_first_chunk_and_retry_nothing(self) -> None:
        # The budget is generous (max_retries=2) precisely so an accidental
        # relaunch would show up as extra chunks rather than as a silent
        # difference in a counter.
        for payload in (
            _conversation_recovery_pause(),
            _successor_blocked_pause(),
        ):
            with self.subTest(reason=payload["pause_reason"]):
                status = _StatusScript([_status_payload(1)])
                run = _RunScript([payload])
                sleeps: list[float] = []

                result = self._supervise(status, run, sleeps=sleeps, max_retries=2)

                self.assertFalse(result["ok"], result)
                self.assertEqual("user_required", result["reason"])
                self.assertEqual([1], run.chunks)
                self.assertEqual([], sleeps)
                retries = result["retries"]
                self.assertEqual(0, retries["count"])
                self.assertFalse(retries["exhausted"])
                self.assertEqual(0.0, retries["delayed_seconds"])

    def test_a_repeated_recovery_pause_is_no_worse_the_second_time(self) -> None:
        """Spending nothing has to mean the next supervision starts even."""
        for _ in range(2):
            status = _StatusScript([_status_payload(1)])
            run = _RunScript([_conversation_recovery_pause()])
            result = self._supervise(status, run, max_retries=2)
            self.assertEqual("user_required", result["reason"])
            self.assertEqual([1], run.chunks)
            self.assertEqual(0, result["retries"]["count"])

    def test_supervision_end_reports_a_recovery_stop_as_blocked(self) -> None:
        outcome, text = self.autopilot._supervision_end_fields(
            {"ok": False, "reason": "user_required"}
        )
        self.assertEqual("blocked", outcome)
        self.assertIn("user_required", text)


class RecoveryDecisionSurfacedTests(_SupervisionCase):
    """A stop nobody can act on is the same as no stop at all."""

    def _recovery_block(self, payload, **kwargs):
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([payload])
        result = self._supervise(status, run, **kwargs)
        self.assertEqual("user_required", result["reason"])
        return result["recovery"]

    def test_conversation_recovery_names_the_one_use_authorization(self) -> None:
        recovery = self._recovery_block(
            _conversation_recovery_pause(
                selection_state=conversations.SELECTION_INCOMPATIBLE,
                mismatched_fields=("root", "harness"),
            )
        )

        self.assertEqual(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED, recovery["reason"]
        )
        self.assertEqual(
            self.autopilot.RECOVERY_DECISION_FRESH_CONVERSATION, recovery["decision"]
        )
        self.assertFalse(recovery["auto_resume"])
        self.assertEqual("operator", recovery["authorized_by"])
        self.assertEqual(
            conversations.SELECTION_INCOMPATIBLE, recovery["selection_state"]
        )
        self.assertEqual(["root", "harness"], recovery["mismatched_fields"])
        self.assertEqual(
            f"asl control recover-conversation {PLAN_DIR} --json",
            recovery["inspect_command"],
        )
        # Rendered by the single renderer, so the helper and the CLI cannot
        # print two different relaunches for the same decision.
        self.assertEqual(
            redirect_cli.fresh_conversation_command(PLAN_DIR),
            recovery["authorization_command"],
        )
        self.assertIn("--fresh-conversation", recovery["authorization_command"])

    def test_a_blocked_successor_offers_evidence_and_no_authorization(self) -> None:
        # Nothing an operator can say makes a second worker safe to start beside
        # a predecessor that cannot be shown stopped, so there is no command to
        # offer -- and the absence is stated rather than left as a missing key.
        recovery = self._recovery_block(_successor_blocked_pause("worker-still-alive"))

        self.assertEqual(
            events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED, recovery["reason"]
        )
        self.assertEqual(
            self.autopilot.RECOVERY_DECISION_PREDECESSOR_ALIVE, recovery["decision"]
        )
        self.assertFalse(recovery["auto_resume"])
        self.assertEqual("worker-still-alive", recovery["blocked_reason"])
        self.assertIn("authorization_command", recovery)
        self.assertIsNone(recovery["authorization_command"])
        self.assertEqual(
            f"asl control redirect-status {PLAN_DIR} --json",
            recovery["inspect_command"],
        )

    def test_the_state_dir_travels_into_every_command_offered(self) -> None:
        # A supervised run pointed at an overridden state dir must not hand back
        # commands that read the default one -- an operator following them would
        # inspect a different plan's evidence and see nothing wrong.
        parsed = dict(PARSED, state_dir="/tmp/state")
        for payload in (
            _successor_blocked_pause(),
            _conversation_recovery_pause(),
        ):
            with self.subTest(reason=payload["pause_reason"]):
                recovery = self.autopilot._recovery_payload(
                    parsed, payload, payload["pause_reason"]
                )
                self.assertIn("--state-dir /tmp/state", recovery["inspect_command"])
                authorization = recovery["authorization_command"]
                if authorization is not None:
                    self.assertIn("--state-dir /tmp/state", authorization)

    def test_an_ordinary_pause_carries_no_recovery_block(self) -> None:
        # Only the two decisions get one; a verify-failed pause is answered by
        # the ordinary reply/repair path and would be muddled by a block that
        # names an authorization it does not need.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_pause("quota-exhausted", message="quota exhausted")])
        result = self._supervise(status, run)

        self.assertEqual("user_required", result["reason"])
        self.assertNotIn("recovery", result)

    def test_building_the_block_reads_only_the_pause_it_was_given(self) -> None:
        """The block is derived, so producing it cannot change what it describes."""
        payload = _successor_blocked_pause()
        before = repr(payload)
        parsed = dict(PARSED)

        first = self.autopilot._recovery_payload(
            parsed, payload, payload["pause_reason"]
        )
        second = self.autopilot._recovery_payload(
            parsed, payload, payload["pause_reason"]
        )

        self.assertEqual(first, second)
        self.assertEqual(before, repr(payload))
        self.assertEqual(PARSED, parsed)

    def test_a_recovery_pause_without_details_still_names_the_decision(self) -> None:
        # An older runner, or a pause truncated on its way here, must not cost
        # the operator the one thing the block exists to say.
        recovery = self.autopilot._recovery_payload(
            dict(PARSED),
            {
                "paused": True,
                "pause_reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
            },
            events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
        )
        self.assertEqual(
            self.autopilot.RECOVERY_DECISION_PREDECESSOR_ALIVE, recovery["decision"]
        )
        self.assertFalse(recovery["auto_resume"])
        self.assertIsNone(recovery["blocked_reason"])


class FreshConversationIsNotSupervisableTests(_SupervisionCase):
    """One authorization, used once, by the person who gave it."""

    def test_the_command_is_refused_when_it_is_parsed(self) -> None:
        result = self.autopilot.parse_command(
            f"asl run {PLAN_DIR} --resume --fresh-conversation"
        )
        self.assertFalse(result["ok"])
        self.assertEqual("unsupported-mode", result["reason"])
        self.assertIn("--fresh-conversation", result["error"])
        # The refusal has to say what to do instead, or it is just a wall.
        self.assertIn("--resume --fresh-conversation", result["error"])

    def test_the_flag_is_dropped_even_from_a_hand_built_namespace(self) -> None:
        # The second layer: a caller that never went through `parse_command`
        # still cannot replay the authorization into a child.
        self.assertIn("fresh_conversation", self.autopilot._CHILD_RUN_ARG_SKIP_DESTS)
        namespace = self.autopilot._build_run_parser().parse_args(
            [PLAN_DIR, "--resume"]
        )
        namespace.fresh_conversation = True

        argv = self.autopilot._build_child_run_args(
            namespace, plan_dir=PLAN_DIR, cwd=None, state_dir=None
        )

        self.assertNotIn("--fresh-conversation", argv)
        self.assertIn("--resume", argv)

    def test_an_ordinary_resume_is_still_supervisable(self) -> None:
        result = self.autopilot.parse_command(f"asl run {PLAN_DIR} --resume")
        self.assertTrue(result["ok"], result)
        self.assertIn("--resume", result["run_args"])
        self.assertNotIn("--fresh-conversation", result["run_args"])

    def test_the_authorization_command_is_never_run_by_the_helper(self) -> None:
        """The helper prints the relaunch; it does not become one."""
        command = redirect_cli.fresh_conversation_command(PLAN_DIR)
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_conversation_recovery_pause()])

        result = self._supervise(status, run, max_retries=2)

        self.assertEqual(command, result["recovery"]["authorization_command"])
        # One chunk, and its argv is the supervised command -- not the relaunch.
        self.assertEqual([1], run.chunks)
        self.assertNotIn("--fresh-conversation", PARSED["argv"])


class UncertainEvidenceRequiresAUserTests(unittest.TestCase):
    """States that do not determine an answer are reported, never guessed."""

    def test_every_non_proceeding_selection_state_requires_recovery(self) -> None:
        # Written as the complement, so a selection state added later lands in
        # the recovery set by default rather than falling through to a fresh
        # conversation nobody authorized.
        self.assertEqual(
            set(conversations.SELECTION_STATES),
            set(conversation_resume.PROCEEDING_SELECTION_STATES)
            | set(conversation_resume.RECOVERY_REQUIRED_SELECTION_STATES),
        )
        self.assertEqual(
            (conversations.SELECTION_RESUMABLE, conversations.SELECTION_ABSENT),
            conversation_resume.PROCEEDING_SELECTION_STATES,
        )
        self.assertNotIn(
            conversations.SELECTION_UNCONFIRMED,
            conversation_resume.PROCEEDING_SELECTION_STATES,
        )
        policy = pause_policy.classify_pause(conversation_resume.RECOVERY_PAUSE_REASON)
        self.assertTrue(policy["requires_user"])
        self.assertEqual(0, policy["max_retries"])

    def test_a_delivered_but_unreceipted_correction_is_never_carried(self) -> None:
        # `uncertain` asks whether a model read something before the machine
        # stopped, which the journal does not record -- so the answer is a
        # report, and re-submitting is the operator's move.
        report = redirect_recovery.ReconciliationReport(
            plan_dir=PLAN_DIR,
            run_id="run-new",
            reconciled=True,
            uncertain=(
                redirect_recovery.StrandedCorrection(
                    operation_id="op-1", session_id="1"
                ),
            ),
        )

        self.assertEqual((), redirect_recovery.recoverable_operation_ids(report, "1"))
        self.assertEqual(("op-1",), report.uncertain_for("1"))
        self.assertIn(
            redirect_recovery.STATUS_UNCERTAIN, redirect_recovery.OPERATION_STATUSES
        )

    def test_a_blocked_reconciliation_carries_nothing_at_all(self) -> None:
        report = redirect_recovery.ReconciliationReport(
            plan_dir=PLAN_DIR,
            run_id="run-new",
            reconciled=False,
            blocked_reason="owner-alive",
            recoverable=(
                redirect_recovery.StrandedCorrection(
                    operation_id="op-1", session_id="1"
                ),
            ),
        )

        self.assertTrue(report.blocked)
        self.assertEqual((), redirect_recovery.recoverable_operation_ids(report, "1"))


class StopsStayStoppedTests(_SupervisionCase):
    """Supervision resumes work; it does not undo an operator's stop."""

    def test_stop_after_current_ends_supervision_without_relaunching(self) -> None:
        status = _StatusScript([_status_payload(3), _status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual("stopped", result["reason"])
        self.assertEqual(self.autopilot.STOP_KIND_AFTER_CURRENT, result["stop_kind"])
        self.assertEqual([1], run.chunks)
        self.assertEqual(2, result["pending"])

    def test_an_abort_ends_supervision_even_with_zero_progress(self) -> None:
        # An abort reverts its session to pending, so no session completed. That
        # is a clean stop, not a stall to relaunch out of.
        status = _StatusScript([_status_payload(3), _status_payload(3)])
        run = _RunScript(
            [
                {
                    "ok": True,
                    "paused": False,
                    "reason": "stopped",
                    "control": {
                        "stop_after_current": False,
                        "tail_enabled": False,
                        "abort_turn_requested": False,
                        "abort_grace_seconds": None,
                        "abort_reason": None,
                        "abort_deadline": None,
                        "next_harness": None,
                        "consumed": True,
                        "last_action": "abort-turn-honored",
                    },
                }
            ]
        )

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual("stopped", result["reason"])
        self.assertEqual(self.autopilot.STOP_KIND_ABORT, result["stop_kind"])
        self.assertEqual([1], run.chunks)
        self.assertEqual(0, result["completed"])

    def test_an_active_lock_is_attached_to_rather_than_raced(self) -> None:
        # Another process already drives this plan; supervision waits on it
        # instead of launching a second worker beside it.
        status = _StatusScript(
            [
                _status_payload(1, lock=_active_lock()),
                _status_payload(1, lock=_active_lock()),
                _status_payload(0),
            ]
        )

        result = self._supervise(status, _never_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual("complete", result["reason"])
        self.assertEqual(0, result["chunks"])


class SupervisorPolicySurfaceTests(unittest.TestCase):
    """What the guide tells a supervising agent it may decide."""

    def _operation(self, name):
        payload = supervisor_guide.guide_payload(harness="codex")
        [operation] = [item for item in payload["operations"] if item["name"] == name]
        self.assertIn(operation["command"], supervisor_guide.render_text(payload))
        return operation

    def test_redirect_is_documented_as_requiring_explicit_authorization(self) -> None:
        operation = self._operation("redirect")

        self.assertIn("asl control redirect", operation["command"])
        self.assertIn("explicitly authorized", operation["description"])
        self.assertIn("Never self-authorize", operation["description"])
        # The distinguishing fact against guidance: this one interrupts.
        self.assertIn("interrupts the turn in flight", operation["description"])
        self.assertIn("--operation-id", operation["description"])

    def test_redirect_status_is_the_only_way_to_settle_an_uncertain_submission(
        self,
    ) -> None:
        operation = self._operation("redirect-status")

        self.assertIn("--wait-acknowledged", operation["command"])
        self.assertIn("uncertain", operation["description"])
        self.assertIn("retracts nothing", operation["description"])

    def test_recover_conversation_is_offered_as_a_read(self) -> None:
        operation = self._operation("recover-conversation")

        self.assertEqual(
            f"asl control recover-conversation {supervisor_guide.PLAN_DIR} --json",
            operation["command"],
        )
        self.assertIn("A read only", operation["description"])
        self.assertIn("--fresh-conversation", operation["description"])
        self.assertIn("refuses", operation["description"])

    def test_the_guide_lists_leaf_help_for_every_new_operation(self) -> None:
        for command in (
            "asl control redirect --help",
            "asl control redirect-status --help",
            "asl control recover-conversation --help",
        ):
            with self.subTest(command=command):
                self.assertIn(command, supervisor_guide.LEAF_HELP)

    def test_urgent_redirect_is_not_a_fleet_operation(self) -> None:
        # Every fleet operation either answers a question the run asked or acts
        # between turns. A redirect replaces what a worker was told mid-turn,
        # which is a per-correction authorization rather than standing authority.
        self.assertEqual(
            ("reply", "guidance", "verify", "commit", "resume"),
            overseer_decisions.OPERATIONS,
        )
        self.assertNotIn("redirect", overseer_decisions.OPERATIONS)
        self.assertNotIn("recover-conversation", overseer_decisions.OPERATIONS)


class ArgumentParsingHygieneTests(_SupervisionCase):
    """The refusal must not be reachable only through the happy path."""

    def test_fresh_conversation_refusal_precedes_any_child_construction(self) -> None:
        result = self.autopilot.parse_command(
            f"asl run {PLAN_DIR} --fresh-conversation"
        )
        self.assertFalse(result["ok"])
        self.assertEqual("parse", result["stage"])
        self.assertNotIn("run_args", result)

    def test_a_namespace_without_the_attribute_is_treated_as_unset(self) -> None:
        namespace = argparse.Namespace()
        self.assertFalse(getattr(namespace, "fresh_conversation", False))


if __name__ == "__main__":
    unittest.main()
