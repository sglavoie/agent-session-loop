# mypy: ignore-errors
"""Focused test_asl_redirect_store test group 3."""

# ruff: noqa: F403, F405
from ._test_asl_redirect_store_support import *


class LaunchDeclarationTests(RedirectStoreTestCase):
    """The line that makes a missing delivery mean something.

    A carry and a delivery bracket a window, and a writer that dies inside it
    leaves the same bytes on disk whether it sent the prompt or not.
    ``launching`` splits that window: before it nothing can have gone out, after
    it nobody reading the file may say so. Every claim here is about which side
    of that line a given shape falls on.
    """

    def carried(
        self, operation_id: str = "op"
    ) -> redirect_store_models.RedirectRequest:
        self.submit(operation_id=operation_id)
        redirect_store_requests.accept_request(
            self.plan_dir,
            operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )
        return redirect_store_carry.carry_request(
            self.plan_dir,
            operation_id,
            continuation=_target(2),
            state_dir=self.state_dir,
        )

    def launch(self, operation_id: str = "op") -> redirect_store_models.RedirectRequest:
        return redirect_store_carry.launch_request(
            self.plan_dir,
            operation_id,
            continuation=_target(2),
            state_dir=self.state_dir,
        )

    def test_a_carry_with_no_declaration_is_answerable(self) -> None:
        """The shape automatic recovery is allowed to act on."""
        request = self.carried()

        self.assertTrue(request.carried)
        self.assertFalse(request.launching)
        self.assertFalse(request.launch_ambiguous)
        self.assertEqual(
            ("op",),
            tuple(item.operation_id for item in self.read().strandable_by("run-2")),
        )

    def test_a_declared_launch_with_no_outcome_is_not(self) -> None:
        self.carried()

        self.launch()

        request = self.read().request("op")
        self.assertTrue(request.launching)
        self.assertFalse(request.delivered)
        self.assertTrue(request.launch_ambiguous)
        # The words may already be in front of a worker, so no later run may be
        # handed them: stranding is the automatic path this shape is kept out of.
        self.assertEqual((), self.read().strandable_by("run-2"))

    def test_a_delivery_closes_the_window_it_opened(self) -> None:
        self.carried()
        self.launch()

        redirect_store_delivery.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        request = self.read().request("op")
        self.assertTrue(request.delivered)
        self.assertFalse(request.launch_ambiguous)

    def test_the_attempt_that_opened_the_window_may_still_close_it(self) -> None:
        """A live process saying "I did not send it" is evidence; silence is not."""
        self.carried()
        self.launch()

        released = redirect_store_delivery.release_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertTrue(released.released)
        self.assertFalse(released.carried)
        self.assertFalse(released.launching)
        self.assertFalse(released.launch_ambiguous)
        # Back in the ordinary recoverable set, exactly as if it had never been
        # declared: a known pre-submission refusal costs the operator nothing.
        self.assertEqual(
            ("op",),
            tuple(item.operation_id for item in self.read().strandable_by("run-2")),
        )

    def test_a_recarry_after_a_release_starts_the_window_closed(self) -> None:
        self.carried()
        self.launch()
        redirect_store_delivery.release_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        recarried = redirect_store_carry.carry_request(
            self.plan_dir, "op", continuation=_target(3), state_dir=self.state_dir
        )

        self.assertFalse(recarried.launching)
        self.assertFalse(recarried.launch_ambiguous)

    def test_declaring_the_same_launch_twice_appends_nothing(self) -> None:
        """A retried launch is one launch, and the file is what proves it."""
        self.carried()
        first = self.launch()

        second = self.launch()

        self.assertEqual(first.launching_seq, second.launching_seq)
        self.assertEqual(4, len(self.raw_lines()))

    def test_a_declaration_after_the_delivery_it_precedes_is_a_no_op(self) -> None:
        self.carried()
        self.launch()
        redirect_store_delivery.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        again = self.launch()

        self.assertTrue(again.delivered)
        self.assertEqual(5, len(self.raw_lines()))

    def test_a_declaration_naming_another_attempt_is_refused(self) -> None:
        self.carried()

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_carry.launch_request(
                self.plan_dir, "op", continuation=_target(3), state_dir=self.state_dir
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_CONTINUATION_MISMATCH, caught.exception.reason
        )
        self.assertFalse(self.read().request("op").launching)

    def test_a_declaration_for_an_uncarried_request_is_refused(self) -> None:
        self.submit(operation_id="op")
        redirect_store_requests.accept_request(
            self.plan_dir, "op", target=_target(), state_dir=self.state_dir
        )

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.launch()

        self.assertEqual(
            redirect_store_vocab.ERROR_NOT_CARRIED, caught.exception.reason
        )

    def test_replay_refuses_a_second_declaration_written_by_hand(self) -> None:
        """The appender's idempotence is not a rule replay may take on trust."""
        self.carried()
        self.launch()

        self.append_raw(
            json.dumps(
                {
                    "version": redirect_store_vocab.REDIRECT_JOURNAL_VERSION,
                    "kind": redirect_store_vocab.KIND_LAUNCHING,
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

        view = self.read()
        self.assertEqual(
            [redirect_store_vocab.REJECT_ALREADY_LAUNCHING],
            [item.reason for item in view.rejected],
        )
        self.assertEqual(4, view.request("op").launching_seq)

    def test_replay_refuses_a_declaration_after_the_launch_happened(self) -> None:
        self.carried()
        self.launch()
        redirect_store_delivery.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.append_raw(
            json.dumps(
                {
                    "version": redirect_store_vocab.REDIRECT_JOURNAL_VERSION,
                    "kind": redirect_store_vocab.KIND_LAUNCHING,
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

        self.assertEqual(
            [redirect_store_vocab.REJECT_ALREADY_DELIVERED],
            [item.reason for item in self.read().rejected],
        )

    def test_a_settled_correction_is_never_launch_ambiguous(self) -> None:
        """Ambiguity is about an open question, not about a missing line."""
        self.carried()
        self.launch()
        redirect_store_delivery.acknowledge_request(
            self.plan_dir, "op", attempt=_target(2), state_dir=self.state_dir
        )

        request = self.read().request("op")
        self.assertTrue(request.acknowledged)
        self.assertFalse(request.launch_ambiguous)


class RecordVersionTests(RedirectStoreTestCase):
    """What a line asserts depends on the version its writer stamped on it.

    ``continued`` is the record whose meaning moved, twice. Version 1 appended
    it once the worker holding the words was up, so the line *is* a delivery.
    Version 2 appends it while the prompt is still being assembled, so the same
    line is an undertaking -- but that writer had no way to say when the prompt
    was about to go out, so its silence afterwards settles nothing. Version 3
    has ``launching``, which is what finally makes a missing delivery mean the
    prompt never left. Replay has nothing but the version to tell the three
    apart, and getting it wrong in any direction loses a correction: read a
    version-1 carry as an undertaking and an upgraded plan hands a worker words
    it already had, read a version-2 carry as provably unsent and a prompt that
    may have gone out is sent again.
    """

    def accepted(
        self, operation_id: str = "op"
    ) -> redirect_store_models.RedirectRequest:
        self.submit(operation_id=operation_id)
        return redirect_store_requests.accept_request(
            self.plan_dir,
            operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

    def append_legacy(self, kind: str, operation_id: str = "op") -> None:
        """Append the line a pre-split writer would have written."""
        version = redirect_store_vocab.REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION
        self.append_raw(
            json.dumps(
                {
                    "version": version,
                    "kind": kind,
                    "id": operation_id,
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

    def test_every_line_a_writer_appends_names_the_current_version(self) -> None:
        self.accepted()
        redirect_store_carry.carry_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )
        redirect_store_delivery.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertEqual(
            [redirect_store_vocab.REDIRECT_JOURNAL_VERSION] * 4,
            [line["version"] for line in self.raw_lines()],
        )

    def test_a_legacy_carry_replays_as_the_delivery_it_recorded(self) -> None:
        self.accepted()

        self.append_legacy("continued")

        stored = self.read().request("op")
        self.assertEqual((), self.read().rejected)
        self.assertTrue(stored.carried)
        self.assertTrue(stored.delivered)
        self.assertTrue(stored.delivered_legacy)
        self.assertTrue(stored.reached_a_worker)
        # One line, so one moment: the carry and the launch it stood for.
        self.assertEqual(stored.continued_at, stored.delivered_at)
        self.assertEqual(stored.continued_seq, stored.delivered_seq)

    def append_unlaunchable(self, kind: str, operation_id: str = "op") -> None:
        """Append the line a writer with the split but no launch record wrote."""
        version = redirect_store_vocab.REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION
        self.append_raw(
            json.dumps(
                {
                    "version": version,
                    "kind": kind,
                    "id": operation_id,
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

    def test_a_current_carry_is_an_undertaking_until_the_launch_is_written(
        self,
    ) -> None:
        """The distinction the version bump exists to preserve."""
        self.accepted()

        redirect_store_carry.carry_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        stored = self.read().request("op")
        self.assertTrue(stored.carried)
        self.assertFalse(stored.delivered)
        self.assertFalse(stored.delivered_legacy)
        self.assertFalse(stored.reached_a_worker)
        # And an undertaking whose writer *could* have declared the launch, so
        # the absence of that declaration is evidence rather than a gap.
        self.assertFalse(stored.launch_unrecorded)
        self.assertFalse(stored.launch_ambiguous)

    def test_a_pre_launch_version_carry_settles_nothing_by_its_silence(self) -> None:
        """The version-2 shape this repair exists to stop misreading.

        Its writer had no ``launching`` line, so "carried and nothing since"
        covers both a prompt that never went out and one that did. Reading it
        the first way is how a correction reaches a second worker.
        """
        self.accepted()

        self.append_unlaunchable("continued")

        stored = self.read().request("op")
        self.assertEqual((), self.read().rejected)
        self.assertTrue(stored.carried)
        self.assertFalse(stored.delivered)
        self.assertTrue(stored.launch_unrecorded)
        self.assertTrue(stored.launch_ambiguous)
        self.assertEqual((), self.read().strandable_by("run-2"))

    def test_a_pre_launch_version_carry_is_still_its_attempt_to_settle(self) -> None:
        """Ambiguous to a reader, not to the process that wrote it.

        An upgraded runner replaying its predecessor's carry may not act on it,
        but nothing about the older line stops a delivery or a release from
        being recorded against it in the ordinary way.
        """
        self.accepted()
        self.append_unlaunchable("continued")

        released = redirect_store_delivery.release_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertTrue(released.released)
        self.assertFalse(released.launch_unrecorded)
        self.assertFalse(released.launch_ambiguous)

    def test_a_line_whose_version_is_not_a_version_is_refused(self) -> None:
        """``True == 1``, and the legacy branch must not be reachable that way."""
        self.accepted()
        self.append_raw(
            json.dumps(
                {
                    "version": True,
                    "kind": "continued",
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

        view = self.read()

        self.assertEqual(
            [redirect_store_vocab.REJECT_UNSUPPORTED_VERSION],
            [r.reason for r in view.rejected],
        )
        self.assertFalse(view.request("op").carried)
        self.assertFalse(view.request("op").delivered)

    def test_a_legacy_carry_can_never_be_released(self) -> None:
        """Releasing is the undo of a carry nothing saw, and this one was seen."""
        self.accepted()
        self.append_legacy("continued")

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_delivery.release_request(
                self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_ALREADY_DELIVERED, caught.exception.reason
        )
        self.assertTrue(self.read().request("op").carried)

    def test_a_legacy_receipt_still_settles_its_correction(self) -> None:
        """An upgrade must not invalidate the receipts already on the file."""
        self.accepted()
        self.append_legacy("continued")

        self.append_legacy("acknowledged")

        stored = self.read().request("op")
        self.assertEqual((), self.read().rejected)
        self.assertTrue(stored.acknowledged)
        self.assertTrue(stored.delivered)

    def test_a_legacy_carry_leaves_no_room_for_a_second_delivery(self) -> None:
        """A mixed-version journal records one launch per carry, not two.

        ``deliver_request`` is idempotent rather than a refusal, so what proves
        the point is the file: the legacy line already said what this call
        would say, and nothing is appended over it.
        """
        self.accepted()
        self.append_legacy("continued")

        delivered = redirect_store_delivery.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertTrue(delivered.delivered_legacy)
        self.assertEqual(3, len(self.raw_lines()))
