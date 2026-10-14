"""Admission: what a producer is told, and what it is never told falsely.

The store's tests prove a correction cannot be lost. These prove the second
half — that "accepted" is only ever said when a runner recorded acceptance,
that every refusal that could have been persisted was refused *before* it was,
and that a submission the urgent channel could not carry is closed in the
journal rather than left for some later runner to act on.

The endpoints are real FIFOs served by the production reader, because "is the
urgent channel actually being served" is the fact admission turns on and a fake
would answer it by assumption.
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    clock,
    control_signal,
    redirect_admission,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
)
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "run-20260910-000000"


class FrozenClock:
    """A monotonic source the wait loop advances by asking for the time.

    Driving the clock from ``sleep`` rather than from real time is what keeps
    these tests deterministic: a two-second bound tested against a real clock is
    a two-second test and a flake on a loaded machine.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class AdmissionTestCase(unittest.TestCase):
    """A plan whose live run, active turn, and urgent endpoint are set per test."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.clock = FrozenClock()

    def write_state(
        self,
        *,
        run_id: str | None = RUN_ID,
        active_turn: dict | None = None,
        paused: bool = False,
        finished: bool = False,
        session_kind: str = "implementation",
        turn_index: int = 1,
    ) -> None:
        if active_turn is None and run_id is not None:
            active_turn = {
                "run_id": run_id,
                "session_id": "session-9",
                "turn_index": turn_index,
                "session_kind": session_kind,
                "harness": "claude",
            }
        payload: dict = {
            "version": 3,
            "plan_dir": str(self.plan_dir),
            "paused": paused,
        }
        if run_id is not None:
            payload["run_id"] = run_id
        if active_turn is not None:
            payload["active_turn"] = active_turn
        if finished:
            payload["run_finished_at"] = clock.utc_iso_now()
        _write_state_payload(self.plan_dir, self.state_dir, payload)

    def write_lock(self, **kwargs) -> None:
        params = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }
        params.update(kwargs)
        _write_lock_file(self.plan_dir, self.state_dir, **params)

    def serve_urgent_endpoint(self) -> Path:
        endpoint, self.wakeup = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        return endpoint

    def make_live_run(self, **state_kwargs) -> Path:
        """A run that is live, has an active turn, and serves urgent redirects."""
        self.write_state(**state_kwargs)
        endpoint = self.serve_urgent_endpoint()
        self.write_lock(redirect_endpoint=endpoint)
        return endpoint

    def submit(self, **kwargs) -> redirect_admission.SubmissionOutcome:
        params = {
            "feedback": "stop and re-read the session contract",
            "state_dir": self.state_dir,
            "monotonic": self.clock.monotonic,
            "sleep": self.clock.sleep,
        }
        params.update(kwargs)
        feedback = params.pop("feedback")
        return redirect_admission.submit_redirect(self.plan_dir, feedback, **params)

    def journal(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def assertNothingPersisted(self) -> None:
        self.assertEqual((), self.journal().requests)


class TargetInspectionTests(AdmissionTestCase):
    """Reading whether a redirect is admissible, without writing anything."""

    def test_a_live_run_with_an_active_turn_is_admissible(self) -> None:
        endpoint = self.make_live_run()

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertTrue(facts.admissible)
        self.assertIsNone(facts.refusal)
        self.assertEqual(str(endpoint), facts.urgent_endpoint)
        self.assertEqual(RUN_ID, facts.target.run_id)
        self.assertEqual("session-9", facts.target.session_id)
        self.assertEqual(1, facts.target.turn_index)
        self.assertEqual("claude", facts.target.harness)

    def test_no_run_at_all_refuses(self) -> None:
        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_RUN, facts.refusal)

    def test_a_paused_run_refuses_as_paused(self) -> None:
        """A paused run is answered through the mailbox; there is no turn to cut."""
        self.write_state(paused=True)
        self.write_lock(redirect_endpoint=self.serve_urgent_endpoint())

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_RUN_PAUSED, facts.refusal)

    def test_a_finished_run_refuses(self) -> None:
        self.write_state(finished=True)
        self.write_lock(redirect_endpoint=self.serve_urgent_endpoint())

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_RUN, facts.refusal)

    def test_a_live_run_between_turns_refuses(self) -> None:
        self.write_state(active_turn={})
        self.write_lock(redirect_endpoint=self.serve_urgent_endpoint())

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_TURN, facts.refusal)

    def test_a_turn_from_another_run_is_not_this_runs_turn(self) -> None:
        self.write_state(
            active_turn={
                "run_id": "some-other-run",
                "session_id": "session-9",
                "turn_index": 1,
            }
        )
        self.write_lock(redirect_endpoint=self.serve_urgent_endpoint())

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_TURN, facts.refusal)

    def test_a_malformed_turn_index_is_not_trusted(self) -> None:
        for turn_index in (0, -1, "1", True, None):
            with self.subTest(turn_index=turn_index):
                self.write_state(
                    active_turn={
                        "run_id": RUN_ID,
                        "session_id": "session-9",
                        "turn_index": turn_index,
                    }
                )
                self.write_lock(redirect_endpoint="/nonexistent")

                facts = redirect_admission.inspect_target(
                    self.plan_dir, state_dir=self.state_dir
                )

                self.assertEqual(
                    redirect_admission.REFUSE_NO_ACTIVE_TURN, facts.refusal
                )

    def test_a_review_turn_refuses_as_a_terminal_audit(self) -> None:
        """Redirecting the run's own verdict replays it against unseen feedback."""
        self.write_state(session_kind="review")
        self.write_lock(redirect_endpoint=self.serve_urgent_endpoint())

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_TERMINAL_AUDIT, facts.refusal)
        self.assertEqual("review", facts.session_kind)

    def test_a_remotely_owned_lock_makes_the_run_not_live_at_all(self) -> None:
        """A local FIFO cannot reach another host's runner.

        The refusal is ``no-active-run`` rather than ``remote-owner`` because a
        foreign lock is not usable by this host, so the run never counts as
        live in the first place. ``remote-owner`` is reserved for the narrower
        case below, where the lock changes underneath the two reads.
        """
        self.write_state()
        self.write_lock(hostname="some-other-host", redirect_endpoint="/elsewhere.fifo")

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_RUN, facts.refusal)

    def test_a_lock_whose_owner_is_gone_makes_the_run_not_live(self) -> None:
        self.write_state()
        self.write_lock(pid=4_242_424, redirect_endpoint="/gone.fifo")

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_RUN, facts.refusal)

    def test_a_lock_that_changes_between_the_two_reads_refuses(self) -> None:
        """Run state is read first and the lock second, so this fails closed.

        The window is narrow and the consequence of ignoring it is not: a
        correction admitted against a lock that has already gone stale or
        foreign has no reachable reader, and the producer would be told it is
        on its way.
        """
        cases = {
            redirect_admission.REFUSE_REMOTE_OWNER: {"hostname": "another-host"},
            redirect_admission.REFUSE_STALE_LOCK: {"pid": 4_242_424},
        }
        original = redirect_admission.intervention.inspect_run
        self.addCleanup(
            setattr, redirect_admission.intervention, "inspect_run", original
        )
        for reason, overrides in cases.items():
            with self.subTest(reason=reason):
                self.make_live_run()
                live_facts = original(self.plan_dir, state_dir=self.state_dir)
                self.assertTrue(live_facts.run_active)
                # Replay the run facts the first read established, then let the
                # real second read meet a lock that changed after it.
                redirect_admission.intervention.inspect_run = (
                    lambda *_args, _facts=live_facts, **_kwargs: _facts
                )
                self.write_lock(redirect_endpoint="/gone.fifo", **overrides)

                facts = redirect_admission.inspect_target(
                    self.plan_dir, state_dir=self.state_dir
                )

                redirect_admission.intervention.inspect_run = original
                self.assertEqual(reason, facts.refusal)

    def test_a_run_that_publishes_no_urgent_endpoint_refuses(self) -> None:
        """The control queue's drain checkpoint is not a substitute."""
        self.write_state()
        self.write_lock()

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(
            redirect_admission.REFUSE_URGENT_CHANNEL_UNAVAILABLE, facts.refusal
        )

    def test_a_named_target_that_no_longer_matches_refuses_as_stale(self) -> None:
        self.make_live_run(turn_index=4)

        facts = redirect_admission.inspect_target(
            self.plan_dir,
            state_dir=self.state_dir,
            expected_target=redirect_store_models.RedirectTarget(
                RUN_ID, "session-9", 3
            ),
        )

        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, facts.refusal)

    def test_inspection_writes_nothing(self) -> None:
        self.make_live_run()

        redirect_admission.inspect_target(self.plan_dir, state_dir=self.state_dir)

        self.assertFalse(
            redirect_store_journal.journal_path_for_plan(
                self.plan_dir, self.state_dir
            ).exists()
        )

    def test_an_unusable_plan_directory_raises_rather_than_refuses(self) -> None:
        with self.assertRaises(redirect_admission.RedirectAdmissionError) as caught:
            redirect_admission.inspect_target(
                self.plan_dir / "no" / "such\0path", state_dir=self.state_dir
            )

        self.assertEqual(
            redirect_admission.REFUSE_INVALID_PLAN, caught.exception.reason
        )


class RefusalTests(AdmissionTestCase):
    """Every refusable submission is refused before anything is persisted."""

    def test_a_refusal_leaves_the_correction_with_its_author(self) -> None:
        self.write_state()
        self.write_lock()

        outcome = self.submit()

        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, outcome.outcome)
        self.assertEqual(
            redirect_admission.REFUSE_URGENT_CHANNEL_UNAVAILABLE, outcome.reason
        )
        self.assertFalse(outcome.persisted)
        self.assertNothingPersisted()

    def test_every_run_state_refusal_persists_nothing(self) -> None:
        cases = {
            redirect_admission.REFUSE_NO_ACTIVE_RUN: lambda: None,
            redirect_admission.REFUSE_RUN_PAUSED: lambda: self.write_state(paused=True),
            redirect_admission.REFUSE_TERMINAL_AUDIT: lambda: self.write_state(
                session_kind="review"
            ),
            redirect_admission.REFUSE_NO_ACTIVE_TURN: lambda: self.write_state(
                active_turn={}
            ),
        }
        endpoint = self.serve_urgent_endpoint()
        for reason, arrange in cases.items():
            with self.subTest(reason=reason):
                arrange()
                self.write_lock(redirect_endpoint=endpoint)

                outcome = self.submit()

                self.assertEqual(redirect_admission.OUTCOME_REJECTED, outcome.outcome)
                self.assertEqual(reason, outcome.reason)
                self.assertFalse(outcome.persisted)
                self.assertNothingPersisted()

    def test_a_transient_refusal_is_reported_as_unavailable_not_rejected(self) -> None:
        """`unavailable` invites a retry; `rejected` says do not bother."""
        self.write_state()
        self.write_lock(hostname="another-host", redirect_endpoint="/elsewhere.fifo")

        self.assertEqual(
            redirect_admission.OUTCOME_REJECTED,
            self.submit().outcome,
        )
        for reason in redirect_admission.UNAVAILABLE_REASONS:
            self.assertIn(reason, redirect_admission.REFUSAL_REASONS)

    def test_a_stale_named_target_is_refused_without_persisting(self) -> None:
        self.make_live_run(turn_index=4)

        outcome = self.submit(
            expected_target=redirect_store_models.RedirectTarget(RUN_ID, "session-9", 3)
        )

        self.assertEqual(redirect_admission.OUTCOME_REJECTED, outcome.outcome)
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, outcome.reason)
        self.assertNothingPersisted()

    def test_malformed_feedback_raises_before_the_run_is_even_read(self) -> None:
        with self.assertRaises(redirect_store_models.RedirectStoreError):
            self.submit(feedback="   ")
        self.assertNothingPersisted()

    def test_an_out_of_range_wait_is_refused(self) -> None:
        self.make_live_run()

        for wait in (-1.0, redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS + 1):
            with self.assertRaises(redirect_admission.RedirectAdmissionError):
                self.submit(wait_seconds=wait)
        self.assertNothingPersisted()


class SubmissionOutcomeTests(AdmissionTestCase):
    """What a producer learns once the correction is durable."""

    def test_an_unanswered_submission_is_uncertain_and_stays_open(self) -> None:
        """Settling here would race an acceptance already in flight."""
        self.make_live_run()

        outcome = self.submit()

        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, outcome.outcome)
        self.assertTrue(outcome.persisted)
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request(outcome.operation_id).state,
        )

    def test_the_wait_is_bounded_by_the_monotonic_clock(self) -> None:
        self.make_live_run()

        outcome = self.submit(wait_seconds=2.0)

        self.assertGreaterEqual(outcome.waited_seconds, 2.0)
        self.assertLess(outcome.waited_seconds, 2.5)
        self.assertEqual(
            redirect_admission.ACCEPTANCE_POLL_INTERVAL_SECONDS, max(self.clock.slept)
        )

    def test_a_zero_wait_reports_uncertain_immediately(self) -> None:
        self.make_live_run()

        outcome = self.submit(wait_seconds=0.0)

        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, outcome.outcome)
        self.assertEqual([], self.clock.slept)

    def test_a_runner_that_accepts_during_the_wait_is_reported_as_accepted(
        self,
    ) -> None:
        self.make_live_run()
        target = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        ).target
        accepted: list[str] = []

        def accept_on_second_poll(seconds: float) -> None:
            self.clock.slept.append(seconds)
            self.clock.now += seconds
            if len(self.clock.slept) == 2 and not accepted:
                pending = redirect_admission.pending_redirects(
                    self.plan_dir, target=target, state_dir=self.state_dir
                )
                accepted.append(pending[0].operation_id)
                redirect_admission.accept_redirect(
                    self.plan_dir,
                    pending[0].operation_id,
                    target=target,
                    state_dir=self.state_dir,
                )

        outcome = self.submit(sleep=accept_on_second_poll)

        self.assertEqual(redirect_admission.OUTCOME_ACCEPTED, outcome.outcome)
        self.assertTrue(outcome.accepted)
        self.assertEqual(accepted[0], outcome.operation_id)
        self.assertLess(outcome.waited_seconds, 2.0)

    def test_a_runner_that_settles_during_the_wait_is_reported_as_rejected(
        self,
    ) -> None:
        self.make_live_run()

        def settle(seconds: float) -> None:
            self.clock.slept.append(seconds)
            self.clock.now += seconds
            for request in self.journal().pending():
                redirect_store_requests.reject_request(
                    self.plan_dir,
                    request.operation_id,
                    reason=redirect_store_vocab.REASON_RUN_ENDED,
                    state_dir=self.state_dir,
                )

        outcome = self.submit(sleep=settle)

        self.assertEqual(redirect_admission.OUTCOME_REJECTED, outcome.outcome)
        self.assertEqual(redirect_store_vocab.REASON_RUN_ENDED, outcome.reason)

    def test_the_urgent_channel_receives_the_wakeup(self) -> None:
        self.make_live_run()

        outcome = self.submit(wait_seconds=0.0)

        self.assertTrue(outcome.delivery.delivered)
        self.assertEqual(control_signal.REASON_DELIVERED, outcome.delivery.reason)
        self.assertTrue(self.wakeup.wait(timeout=5.0))

    def test_a_wakeup_that_lands_is_still_not_an_acceptance(self) -> None:
        """A delivered byte proves a reader existed, not that it took the request."""
        self.make_live_run()

        outcome = self.submit()

        self.assertTrue(outcome.delivery.delivered)
        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, outcome.outcome)

    def test_an_uncertain_outcome_still_names_the_operation(self) -> None:
        """Without an id the operator has no way to find out what happened."""
        self.make_live_run()

        outcome = self.submit()

        self.assertIsNotNone(outcome.operation_id)
        self.assertIn(outcome.operation_id, str(self.journal().to_payload()))

    def test_a_retried_submission_does_not_become_a_second_correction(self) -> None:
        self.make_live_run()

        first = self.submit(operation_id="op-1")
        second = self.submit(operation_id="op-1")

        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(1, len(self.journal().requests))

    def test_the_outcome_payload_carries_no_feedback_text(self) -> None:
        self.make_live_run()
        secret = "never put this in a projection"

        outcome = self.submit(feedback=secret)

        self.assertNotIn(secret, str(outcome.to_payload()))


class UndeliverableChannelTests(AdmissionTestCase):
    """A submission the urgent channel could not carry is closed, not left open."""

    def setUp(self) -> None:
        super().setUp()
        self.write_state()
        # A lock naming an endpoint that no process is reading: the exact state
        # a runner leaves behind when it dies without tearing its FIFO down.
        abandoned = self.state_dir / "abandoned-redirect.fifo"
        abandoned.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(abandoned, 0o600)
        self.write_lock(redirect_endpoint=abandoned)

    def test_a_dead_endpoint_yields_unavailable_rather_than_accepted(self) -> None:
        outcome = self.submit()

        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, outcome.outcome)
        self.assertFalse(outcome.delivery.delivered)
        self.assertEqual(control_signal.REASON_ENDPOINT_CLOSED, outcome.delivery.reason)

    def test_the_undeliverable_request_is_durably_settled(self) -> None:
        """So no later runner can decide the correction landed after all."""
        outcome = self.submit()

        settled = self.journal().request(outcome.operation_id)
        self.assertEqual(redirect_store_vocab.STATE_REJECTED, settled.state)
        self.assertEqual(
            redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE, settled.reason
        )

    def test_a_settled_undeliverable_request_cannot_later_be_accepted(self) -> None:
        outcome = self.submit()

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_admission.accept_redirect(
                self.plan_dir,
                outcome.operation_id,
                target=redirect_store_models.RedirectTarget(RUN_ID, "session-9", 1),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_ALREADY_SETTLED, caught.exception.reason
        )

    def test_the_feedback_is_still_recoverable_after_the_refusal(self) -> None:
        """Refused is not lost: the operator's words are still in the journal."""
        outcome = self.submit(feedback="the correction that did not land")

        self.assertEqual(
            "the correction that did not land",
            redirect_store_journal.read_feedback(
                self.plan_dir, outcome.operation_id, state_dir=self.state_dir
            ),
        )

    def test_an_unwritable_settlement_leaves_the_request_open_not_accepted(
        self,
    ) -> None:
        original = redirect_store_requests.reject_request

        def failing(*args, **kwargs):
            raise redirect_store_models.RedirectStoreError(
                redirect_store_vocab.ERROR_JOURNAL_WRITE, "device is gone"
            )

        redirect_store_requests.reject_request = failing
        self.addCleanup(setattr, redirect_store_requests, "reject_request", original)

        outcome = self.submit()

        redirect_store_requests.reject_request = original
        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, outcome.outcome)
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request(outcome.operation_id).state,
        )


class RunnerSideTests(AdmissionTestCase):
    """The read and accept path the live runner uses."""

    def test_pending_requests_are_scoped_to_the_turn_that_owns_them(self) -> None:
        self.make_live_run()
        this_turn = redirect_store_models.RedirectTarget(RUN_ID, "session-9", 1)
        next_turn = redirect_store_models.RedirectTarget(RUN_ID, "session-9", 2)
        mine = self.submit(wait_seconds=0.0).operation_id
        redirect_store_requests.submit_request(
            self.plan_dir,
            target=next_turn,
            feedback="for the next turn",
            state_dir=self.state_dir,
        )

        pending = redirect_admission.pending_redirects(
            self.plan_dir, target=this_turn, state_dir=self.state_dir
        )

        self.assertEqual((mine,), tuple(r.operation_id for r in pending))

    def test_accepting_settles_the_request_and_empties_the_queue(self) -> None:
        self.make_live_run()
        target = redirect_store_models.RedirectTarget(RUN_ID, "session-9", 1)
        outcome = self.submit(wait_seconds=0.0)

        accepted = redirect_admission.accept_redirect(
            self.plan_dir, outcome.operation_id, target=target, state_dir=self.state_dir
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(
            (),
            redirect_admission.pending_redirects(
                self.plan_dir, target=target, state_dir=self.state_dir
            ),
        )

    def test_a_runner_cannot_accept_a_request_aimed_at_another_turn(self) -> None:
        self.make_live_run()
        outcome = self.submit(wait_seconds=0.0)

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_admission.accept_redirect(
                self.plan_dir,
                outcome.operation_id,
                target=redirect_store_models.RedirectTarget(RUN_ID, "session-9", 2),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_TARGET_MISMATCH, caught.exception.reason
        )


if __name__ == "__main__":
    unittest.main()
