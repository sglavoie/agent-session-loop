"""Corrections the next attempt could not be shown to have received.

Split from tests/test_asl_redirect_continuation.py."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_vocab,
    turn_dispositions,
)

from .asl_redirect_continuation_support import (
    FEEDBACK,
    SESSION_ID,
    WORKER_PID,
)
from .asl_redirect_run_support import _RedirectedRun


class UnrecordableDeliveryTests(_RedirectedRun):
    """The delivery line the journal would not take, on a prompt already sent.

    The run does everything right and the disk does not: the correction is
    carried, declared outbound, handed to a worker, and then the append that
    would say so is refused. What is left is the shape a crash in the same
    window leaves, which is the point -- the words went out, nothing durable
    says they went out, and the only wrong answer is to send them again.
    """

    def setUp(self) -> None:
        super().setUp()
        self.prompts: list[str] = []

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt") or ""))
        return super()._fake_worker(name, **kwargs)

    def _run_with_refused_delivery(self) -> dict[str, Any]:
        with mock.patch.object(
            redirect_store_delivery,
            "deliver_request",
            side_effect=redirect_store_models.RedirectStoreError(
                redirect_store_vocab.ERROR_JOURNAL_WRITE,
                "injected delivery append failure",
            ),
        ):
            return self.run_loop_once()

    def _request(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request

    def test_the_turn_still_gets_the_correction(self) -> None:
        """The journal refusing to keep a record does not withhold the words."""
        self._run_with_refused_delivery()

        self.assertEqual(["codex", "codex"], self.harness_calls)
        self.assertIn(FEEDBACK, self.prompts[1])

    def test_the_failure_is_visible_in_the_run_s_own_record(self) -> None:
        """Reported rather than raised: a worker is already holding the prompt."""
        self._run_with_refused_delivery()

        continuations = self.events_named(events.REDIRECT_CONTINUATION_EVENT)
        self.assertEqual(
            [
                turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
                "carried",
                "resumed",
                "launching",
                "delivered",
            ],
            [record["outcome"] for record in continuations],
        )
        delivered = continuations[4]
        self.assertEqual([], delivered["operation_ids"])
        self.assertEqual(
            [redirect_store_vocab.ERROR_JOURNAL_WRITE],
            [refusal["reason"] for refusal in delivered["refused"]],
        )
        self.assertNotIn(FEEDBACK, json.dumps(continuations))

    def test_the_carry_is_left_standing_rather_than_handed_back(self) -> None:
        self._run_with_refused_delivery()

        request = self._request()
        # No delivery, because none could be written -- and no release either,
        # because the launch record forbids reading that absence as proof.
        self.assertTrue(request.carried)
        self.assertFalse(request.delivered)
        self.assertFalse(request.released)
        self.assertTrue(request.launching)
        self.assertTrue(request.launch_ambiguous)

    def test_the_next_run_is_not_offered_the_words_again(self) -> None:
        """The defect this repair closes: a second worker told the same thing."""
        self._run_with_refused_delivery()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-redirect-2",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertEqual((), report.released)
        self.assertEqual((), report.recoverable_for(SESSION_ID))
        self.assertEqual(("op-1",), report.uncertain_for(SESSION_ID))

    def test_a_turn_that_only_went_quiet_is_not_a_refusal(self) -> None:
        """The same standing carry, from an adapter that never reported at all.

        No injected failure here and no submission callback either: only the
        silence a lost reporting thread leaves. It has to read as the question
        rather than as the release, or every harness that forgets to call back
        becomes a duplicate delivery.
        """
        original = self._fake_worker

        def quiet(name: str, **kwargs: Any) -> harnesses.HarnessResult:
            if self.harness_calls:
                kwargs.pop("on_prompt_submitted", None)
            return original(name, **kwargs)

        self._fake_worker = quiet  # type: ignore[method-assign]
        self.run_loop_once()

        request = self._request()
        self.assertIn(FEEDBACK, self.prompts[1])
        self.assertTrue(request.launch_ambiguous)
        self.assertFalse(request.released)


class ProvenRefusalAfterLaunchTests(_RedirectedRun):
    """A worker that spawned, refused to submit, and said which it was.

    The launch record makes silence ambiguous, so this is what keeps ambiguity
    from swallowing the recoverable cases with it: a turn whose result names a
    refusal has proven the prompt never went out, and the words it was holding
    go back to be delivered properly rather than being left for an operator to
    puzzle over.
    """

    REFUSAL = "the provider declined to restore conv-1"

    def setUp(self) -> None:
        super().setUp()
        self.prompts: list[str] = []

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.harness_calls:
            return super()._fake_worker(name, **kwargs)
        self.harness_calls.append(name)
        kwargs["on_spawn"](WORKER_PID)
        kwargs["abort"].release()
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=1,
            stdout="",
            stderr=f"{self.REFUSAL}\n",
            conversation_restore_refused=self.REFUSAL,
        )

    def _request(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request

    def test_the_refusal_hands_the_correction_back(self) -> None:
        self.run_loop_once(expect_exit_code=1)

        request = self._request()
        self.assertIn(FEEDBACK, self.prompts[1])
        self.assertFalse(request.carried)
        self.assertFalse(request.delivered)
        self.assertTrue(request.released)
        self.assertFalse(request.launch_ambiguous)

    def test_the_released_correction_is_offered_to_the_next_run(self) -> None:
        self.run_loop_once(expect_exit_code=1)

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-redirect-2",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertEqual(("op-1",), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.uncertain_for(SESSION_ID))


class UndeclarableLaunchTests(_RedirectedRun):
    """The journal refusing the one line written while non-delivery is provable.

    A journal that cannot take the launch record cannot take the delivery
    record either, so submitting anyway would manufacture the ambiguity this
    repair exists to avoid. The turn is not started at all: the correction goes
    back intact and the run stops to be told what is wrong with the disk.
    """

    def setUp(self) -> None:
        super().setUp()
        self.prompts: list[str] = []

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt") or ""))
        return super()._fake_worker(name, **kwargs)

    def _run_with_refused_launch(self) -> dict[str, Any]:
        with mock.patch.object(
            redirect_store_carry,
            "launch_request",
            side_effect=redirect_store_models.RedirectStoreError(
                redirect_store_vocab.ERROR_JOURNAL_WRITE,
                "injected launch append failure",
            ),
        ):
            return self.run_loop_once(expect_exit_code=1)

    def _request(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request

    def test_the_prompt_is_never_submitted(self) -> None:
        self._run_with_refused_launch()

        self.assertEqual(["codex"], self.harness_calls)
        self.assertEqual(1, len(self.prompts))

    def test_the_run_pauses_on_the_journal_rather_than_the_plan(self) -> None:
        payload = self._run_with_refused_launch()

        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REDIRECT_DELIVERY_UNRECORDABLE, payload["reason"]
        )
        self.assertNotIn(FEEDBACK, json.dumps(payload))

    def test_the_correction_goes_back_whole(self) -> None:
        self._run_with_refused_launch()

        request = self._request()
        self.assertTrue(request.accepted)
        self.assertFalse(request.carried)
        self.assertFalse(request.delivered)
        self.assertTrue(request.released)
        self.assertFalse(request.launch_ambiguous)
        continuations = self.events_named(events.REDIRECT_CONTINUATION_EVENT)
        self.assertEqual(
            [
                turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
                "carried",
                "resumed",
                "launching-failed",
                "released",
            ],
            [record["outcome"] for record in continuations],
        )

    def test_the_next_run_may_still_deliver_it(self) -> None:
        self._run_with_refused_launch()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-redirect-2",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertEqual(("op-1",), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.uncertain_for(SESSION_ID))


if __name__ == "__main__":
    unittest.main()
