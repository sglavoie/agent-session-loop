# mypy: ignore-errors
"""Focused test_asl_redirect_ack test group 3."""

# ruff: noqa: F403, F405
from ._test_asl_redirect_ack_support import *


class AnnotationReserveTests(RedirectAckTestCase):
    """A journal too full for new corrections still accounts for its old ones."""

    def fill_to_the_submission_cap(self) -> None:
        padding = (
            redirect_store_vocab.REDIRECT_JOURNAL_MAX_BYTES
            - redirect_store_vocab.REDIRECT_SETTLEMENT_RESERVE_BYTES
            - self.journal_path().stat().st_size
        )
        # Padded in long lines rather than two-byte ones: the cap counts bytes,
        # and a few thousand junk lines replay in a fraction of the time two
        # million of them take.
        line = b"x" * 8191 + b"\n"
        whole, remainder = divmod(padding, len(line))
        with self.journal_path().open("ab") as handle:
            handle.write(line * whole)
            if remainder:
                handle.write(b"x" * (remainder - 1) + b"\n")

    def test_delivery_and_receipt_still_land_past_the_submission_cap(self) -> None:
        # The reserve's justification: an accepted correction whose delivery
        # could not be recorded would be one no receipt could ever be checked
        # against, which is worse than a journal that refuses new corrections.
        request = self.submit(accept=True)
        self.fill_to_the_submission_cap()
        with self.assertRaises(redirect_store_models.RedirectStoreError) as refused:
            self.submit(SECOND)
        self.assertEqual(
            redirect_store_vocab.ERROR_JOURNAL_FULL, refused.exception.reason
        )

        continuation = target(2, harness="codex")
        carried = self.carry(
            continuation=continuation, accepted=(request.operation_id,)
        )
        receipt = redirect_ack.acknowledge_correction(
            self.plan_dir,
            request.operation_id,
            token=redirect_ack.attempt_token(continuation),
            live_attempt=continuation,
            state_dir=self.state_dir,
        )

        self.assertEqual([request.operation_id], list(carried.operation_ids()))
        self.assertFalse(receipt.repeated)
        # Read back through replay, so both annotations are proven to be in the
        # file rather than only in the returned records.
        settled = self.read().request(request.operation_id)
        self.assertTrue(settled.carried)
        self.assertTrue(settled.acknowledged)


class ObservationalWaitTests(RedirectAckTestCase):
    """What an observer may conclude from silence: nothing actionable."""

    def setUp(self) -> None:
        super().setUp()
        self.request = self.submit(FIRST)
        self.continuation = target(2, harness="codex")
        self.carry(continuation=self.continuation)

    def wait(self, **kwargs: Any) -> redirect_ack.AcknowledgmentWait:
        clock_values = kwargs.pop("clock_values", None)
        slept: list[float] = kwargs.pop("slept", [])
        ticks = iter(clock_values or (0.0, 0.0))

        def monotonic() -> float:
            try:
                return next(ticks)
            except StopIteration:
                return 10_000.0

        params: dict[str, Any] = {
            "attempt": self.continuation,
            "state_dir": self.state_dir,
            "monotonic": monotonic,
            "sleep": slept.append,
        }
        params.update(kwargs)
        return redirect_ack.wait_for_acknowledgment(self.plan_dir, **params)

    def test_a_timeout_writes_nothing_and_reports_no_failure(self) -> None:
        """The session's rule, in one assertion set.

        A worker can be busy applying the correction for far longer than any
        interesting wait, so a timeout that cancelled or re-interrupted would
        attack exactly the work the correction asked for.
        """
        lines = len(self.journal_lines())

        result = self.wait(timeout_seconds=0.0)

        self.assertFalse(result.complete)
        self.assertEqual((self.request.operation_id,), result.outstanding)
        self.assertEqual((), result.acknowledged)
        self.assertEqual(lines, len(self.journal_lines()))
        payload = result.to_payload()
        self.assertTrue(payload["observational"])
        self.assertNotIn("failed", payload)
        # The correction is still carried, still accepted, still unacknowledged:
        # the wait ending changed nothing about it.
        stored = self.read().request(self.request.operation_id)
        self.assertTrue(stored.carried)
        self.assertFalse(stored.acknowledged)

    def test_a_receipt_that_lands_during_the_wait_completes_it(self) -> None:
        slept: list[float] = []

        def acknowledge_on_second_poll(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 1:
                redirect_ack.acknowledge_correction(
                    self.plan_dir,
                    self.request.operation_id,
                    token=redirect_ack.attempt_token(self.continuation),
                    live_attempt=self.continuation,
                    state_dir=self.state_dir,
                )

        result = self.wait(
            timeout_seconds=30.0,
            clock_values=(0.0, 1.0, 2.0),
            sleep=acknowledge_on_second_poll,
        )

        self.assertTrue(result.complete)
        self.assertEqual((self.request.operation_id,), result.acknowledged)
        self.assertEqual([redirect_ack.ACK_WAIT_POLL_INTERVAL_SECONDS], slept)

    def test_an_id_the_journal_never_recorded_stays_outstanding(self) -> None:
        result = self.wait(operation_ids=("op-missing",), timeout_seconds=0.0)

        self.assertEqual(("op-missing",), result.outstanding)

    def test_patience_is_bounded_even_when_a_caller_asks_for_more(self) -> None:
        """A caller cannot ask an observer to watch forever.

        The ceiling is what keeps a mistaken timeout from turning a read-only
        observation into a process that outlives the run it was watching.
        """
        slept: list[float] = []

        result = self.wait(
            timeout_seconds=10_000.0,
            clock_values=(0.0, redirect_ack.MAX_ACK_WAIT_SECONDS + 1.0),
            slept=slept,
        )

        self.assertFalse(result.complete)
        # Past the ceiling the observer is done; had it honoured the 10,000
        # seconds it was handed, it would have slept and polled again.
        self.assertEqual([], slept)


class AcknowledgeCommandTests(RedirectAckTestCase):
    """``asl inbox acknowledge``: the worker's own surface."""

    def setUp(self) -> None:
        super().setUp()
        self.request = self.submit(FIRST, accept=True)
        self.continuation = target(2, harness="codex")
        self.carry(
            continuation=self.continuation, accepted=(self.request.operation_id,)
        )

    def args(self, **overrides: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "plan": None,
            "state_dir": str(self.state_dir),
            "json": True,
            "redirect_id": None,
            "attempt": redirect_ack.attempt_token(self.continuation),
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def run_command(
        self,
        *,
        live: redirect_store_models.RedirectTarget | None = None,
        **overrides: Any,
    ) -> tuple[int, dict[str, Any]]:
        """Drive the handler with the plan resolved and the live attempt dialled.

        Plan resolution and the run-state read are both patched, because the
        subject here is what the command does with an attempt it was given --
        which attempt is live is ``_live_attempt``'s own contract, and the run
        state that would carry one is not something this journal has.
        """
        captured: list[dict[str, Any]] = []
        context = _FakeCommandContext(self.plan_dir)
        with (
            mock.patch.object(mailbox_cli_args, "resolve", return_value=context),
            mock.patch.object(mailbox_cli_inbox, "live_attempt", return_value=live),
            mock.patch.object(responses, "print_json", captured.append),
        ):
            code = mailbox_cli_inbox.inbox_acknowledge_from_args(self.args(**overrides))
        return code, captured[0]

    def test_the_default_receipts_exactly_what_the_attempt_carries(self) -> None:
        code, payload = self.run_command(live=self.continuation)

        self.assertEqual(0, code)
        self.assertEqual([self.request.operation_id], payload["requested"])
        self.assertEqual(
            [self.request.operation_id],
            [record["operation_id"] for record in payload["acknowledged"]],
        )
        self.assertEqual([], payload["refused"])
        self.assertTrue(self.read().request(self.request.operation_id).acknowledged)

    def test_a_mismatched_token_refuses_every_named_correction(self) -> None:
        """One claim, checked once: a bad token is not a per-id problem."""
        code, payload = self.run_command(
            live=self.continuation,
            redirect_id=[self.request.operation_id],
            attempt=redirect_ack.attempt_token(target(1)),
        )

        self.assertEqual(1, code)
        self.assertEqual([], payload["acknowledged"])
        self.assertEqual(
            [redirect_ack.ERROR_ATTEMPT_MISMATCH],
            [record["reason"] for record in payload["refused"]],
        )
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_a_command_run_with_no_live_attempt_refuses(self) -> None:
        code, payload = self.run_command(
            live=None, redirect_id=[self.request.operation_id]
        )

        self.assertEqual(1, code)
        self.assertIsNone(payload["live_attempt"])
        self.assertEqual(
            [redirect_ack.ERROR_NO_LIVE_ATTEMPT],
            [record["reason"] for record in payload["refused"]],
        )

    def test_a_bare_command_with_no_live_attempt_refuses_rather_than_reassures(
        self,
    ) -> None:
        # Nothing to enumerate and nothing to check an enumeration against, so
        # the honest answer is the refusal rather than "no corrections carried",
        # which a stale worker would read as permission to skip the receipt.
        code, payload = self.run_command(live=None)

        self.assertEqual(1, code)
        self.assertEqual(redirect_ack.ERROR_NO_LIVE_ATTEMPT, payload["reason"])
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_a_live_attempt_carrying_nothing_reports_an_empty_receipt(self) -> None:
        code, payload = self.run_command(live=target(3, harness="codex"))

        self.assertEqual(0, code)
        self.assertEqual([], payload["requested"])
        self.assertEqual([], payload["acknowledged"])
        self.assertEqual([], payload["refused"])

    def test_an_unknown_id_does_not_suppress_the_receipt_beside_it(self) -> None:
        code, payload = self.run_command(
            live=self.continuation,
            redirect_id=["op-missing", self.request.operation_id],
        )

        self.assertEqual(1, code)
        self.assertEqual(
            [self.request.operation_id],
            [record["operation_id"] for record in payload["acknowledged"]],
        )
        self.assertEqual(
            [redirect_store_vocab.ERROR_UNKNOWN_REQUEST],
            [record["reason"] for record in payload["refused"]],
        )

    def test_the_receipt_payload_never_carries_the_operators_words(self) -> None:
        _, payload = self.run_command(live=self.continuation)

        self.assertNotIn(FIRST, json.dumps(payload))


class RunLoopBoundaryTests(RedirectAckTestCase):
    """Where the run loop draws the boundary, and what it records there."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = FakeRunContext(event_path=self.root / "events.jsonl")

    def pending(
        self,
        *,
        session_id: str = SESSION_ID,
        operation_ids: tuple[str, ...] = (),
    ) -> redirect_continuation.PendingRedirect:
        return redirect_continuation.PendingRedirect(
            session_id=session_id,
            harness="claude",
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=operation_ids,
            feedback_digests=(),
            feedback_bytes=0,
        )

    def carried(
        self,
        *,
        pending: redirect_continuation.PendingRedirect | None,
        session_id: str = SESSION_ID,
        turn_index: int = 2,
    ) -> redirect_ack.CarriedCorrections | None:
        return run_loop_carried_corrections._carried_corrections(
            self.ctx,
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir,
            pending=pending,
            session_id=session_id,
            turn_index=turn_index,
            harness="codex",
        )

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self.ctx.persistence.events)

    def test_a_turn_that_was_not_corrected_sweeps_nothing(self) -> None:
        """Not an optimisation: the gate's session match, restated.

        A correction is never swept into a session the operator was not talking
        about, so a fresh turn reads a prompt with no correction block even if
        the journal holds feedback for another session.
        """
        self.submit(FIRST)

        self.assertIsNone(self.carried(pending=None))
        self.assertEqual([], self.events())

    def test_a_correction_for_a_different_session_does_not_cross_over(self) -> None:
        self.submit(FIRST)

        self.assertIsNone(self.carried(pending=self.pending(session_id="2")))
        self.assertEqual([], self.events())

    def test_the_continuation_records_what_it_carried(self) -> None:
        accepted = self.submit(FIRST, accept=True)
        late = self.submit(SECOND)

        carried = self.carried(
            pending=self.pending(operation_ids=(accepted.operation_id,))
        )

        self.assertEqual(
            (accepted.operation_id, late.operation_id), carried.operation_ids()
        )
        name, payload = self.events()[-1]
        self.assertEqual("redirect_continuation", name)
        self.assertEqual("carried", payload["outcome"])
        self.assertEqual(f"{RUN_ID}/1/2", payload["attempt"])
        self.assertEqual(2, payload["carried_count"])
        self.assertNotIn(FIRST, json.dumps(payload))
        self.assertNotIn(SECOND, json.dumps(payload))

    def test_an_unusable_journal_degrades_to_session_eleven_behaviour(self) -> None:
        """The prompt is still built, and the corrections are not lost.

        The resumed worker is still told which operations were interrupted, and
        the failure is recorded rather than silently changing what it reads.
        """
        self.journal_path().parent.mkdir(parents=True, exist_ok=True)
        self.journal_path().write_text("not json\n", encoding="utf-8")
        self.journal_path().chmod(0o000)
        self.addCleanup(self.journal_path().chmod, 0o600)

        carried = self.carried(pending=self.pending(operation_ids=("op-1",)))

        self.assertIsNone(carried)
        name, payload = self.events()[-1]
        self.assertEqual("redirect_continuation", name)
        self.assertEqual("carry-failed", payload["outcome"])
        self.assertIn("reason", payload["error"])
