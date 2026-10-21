"""Admission cases: inspecting the target and refusing before persisting."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_redirect_admission_support import *


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
