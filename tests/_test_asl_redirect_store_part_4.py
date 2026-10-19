# mypy: ignore-errors
"""Focused test_asl_redirect_store test group 4."""

# ruff: noqa: F403, F405
from ._test_asl_redirect_store_support import *


class BoundsTests(RedirectStoreTestCase):
    """A full journal must still be able to answer what is already in it."""

    def test_a_submission_stops_short_of_the_cap(self) -> None:
        limit = (
            redirect_store_vocab.REDIRECT_JOURNAL_MAX_BYTES
            - redirect_store_vocab.REDIRECT_SETTLEMENT_RESERVE_BYTES
        )
        self.journal_path().parent.mkdir(parents=True, exist_ok=True)
        self.journal_path().write_bytes(b"x\n" * (limit // 2))

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.submit()

        self.assertEqual(
            redirect_store_vocab.ERROR_JOURNAL_FULL, caught.exception.reason
        )

    def test_the_reserve_keeps_a_pending_request_settleable(self) -> None:
        """The failure this reserve exists to prevent is an unanswerable request."""
        request = self.submit(operation_id="op")
        padding = (
            redirect_store_vocab.REDIRECT_JOURNAL_MAX_BYTES
            - redirect_store_vocab.REDIRECT_SETTLEMENT_RESERVE_BYTES
            - self.journal_path().stat().st_size
        )
        with self.journal_path().open("ab") as handle:
            handle.write(b"x\n" * (padding // 2))

        with self.assertRaises(redirect_store_models.RedirectStoreError):
            self.submit(operation_id="op-2", feedback="a second correction")
        accepted = redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)

    def test_a_failed_append_leaves_the_journal_exactly_as_it_was(self) -> None:
        self.submit(operation_id="op")
        before = self.journal_path().read_bytes()

        with self.assertRaises(redirect_store_models.RedirectStoreError):
            self.submit(
                operation_id="op-2",
                feedback="x" * (redirect_store_vocab.REDIRECT_MAX_FEEDBACK_BYTES + 1),
            )

        self.assertEqual(before, self.journal_path().read_bytes())


class ConcurrencyTests(RedirectStoreTestCase):
    """Concurrent producers must not lose, merge, or duplicate a correction."""

    def test_concurrent_submissions_all_survive_with_distinct_sequences(self) -> None:
        count = 24

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            requests = list(
                pool.map(
                    lambda index: self.submit(feedback=f"correction {index}"),
                    range(count),
                )
            )

        self.assertEqual(count, len({r.operation_id for r in requests}))
        self.assertEqual(count, len({r.submitted_seq for r in requests}))
        view = self.read()
        self.assertEqual(count, len(view.requests))
        self.assertEqual((), view.rejected)
        self.assertEqual(
            {f"correction {index}" for index in range(count)},
            {
                redirect_store_journal.read_feedback(
                    self.plan_dir, r.operation_id, state_dir=self.state_dir
                )
                for r in requests
            },
        )

    def test_concurrent_retries_of_one_id_produce_one_request(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _index: self.submit(operation_id="op"), range(16))
            )

        self.assertEqual({1}, {r.submitted_seq for r in results})
        self.assertEqual(1, len(self.raw_lines()))

    def test_concurrent_acceptances_settle_a_request_exactly_once(self) -> None:
        """The race that would otherwise redirect two turns from one request."""
        request = self.submit(operation_id="op")

        def attempt(_index: int) -> str:
            try:
                return redirect_store_requests.accept_request(
                    self.plan_dir, "op", target=_target(), state_dir=self.state_dir
                ).state
            except redirect_store_models.RedirectStoreError as error:
                return error.reason

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(16)))

        self.assertEqual({redirect_store_vocab.STATE_ACCEPTED}, set(outcomes))
        kinds = [line["kind"] for line in self.raw_lines()]
        self.assertEqual(1, kinds.count(redirect_store_vocab.KIND_ACCEPTED))
        self.assertEqual(
            redirect_store_vocab.STATE_ACCEPTED,
            self.read().request(request.operation_id).state,
        )

    def test_an_acceptance_racing_an_expiry_yields_one_terminal_record(self) -> None:
        request = self.submit(operation_id="op", ttl_seconds=1)
        past = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)

        def accept(_index: int) -> str:
            try:
                return redirect_store_requests.accept_request(
                    self.plan_dir, "op", target=_target(), state_dir=self.state_dir
                ).state
            except redirect_store_models.RedirectStoreError as error:
                return error.reason

        def expire(_index: int) -> str:
            return str(
                len(
                    redirect_store_requests.expire_due_requests(
                        self.plan_dir, state_dir=self.state_dir, now=past
                    )
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: (accept(index), expire(index)), range(12)))

        kinds = [line["kind"] for line in self.raw_lines()]
        terminal = [k for k in kinds if k != redirect_store_vocab.KIND_SUBMITTED]
        self.assertEqual(1, len(terminal))
        self.assertIn(
            self.read().request("op").state,
            (redirect_store_vocab.STATE_ACCEPTED, redirect_store_vocab.STATE_EXPIRED),
        )


class DurabilityTests(RedirectStoreTestCase):
    """The append is flushed before the caller is told it happened."""

    def test_every_completed_append_is_fullsynced(self) -> None:
        synced: list[int] = []
        original = redirect_store_journal.durability.fullsync

        def record(fd: int) -> None:
            synced.append(fd)
            original(fd)

        redirect_store_journal.durability.fullsync = record
        self.addCleanup(
            setattr, redirect_store_journal.durability, "fullsync", original
        )

        request = self.submit()
        redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertEqual(2, len(synced))

    def test_a_short_write_is_rolled_back_rather_than_left_torn(self) -> None:
        self.submit(operation_id="op")
        before = self.journal_path().read_bytes()
        journal_inode = self.journal_path().stat().st_ino
        original = os.write

        def truncated_write(fd: int, data: bytes) -> int:
            # Scoped to this journal by inode. `redirect_store_journal.os` is the real
            # `os` module, so an unconditional patch would truncate the test
            # runner's own writes for the duration of this block.
            if os.fstat(fd).st_ino != journal_inode:
                return original(fd, data)
            return original(fd, data[: len(data) // 2])

        os.write = truncated_write
        self.addCleanup(setattr, os, "write", original)

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.submit(operation_id="op-2", feedback="second correction")

        os.write = original
        self.assertEqual(
            redirect_store_vocab.ERROR_JOURNAL_WRITE, caught.exception.reason
        )
        self.assertEqual(before, self.journal_path().read_bytes())
        self.assertEqual(1, len(self.read().requests))

    def test_a_failed_append_is_not_reported_as_a_submission(self) -> None:
        """A producer told the write failed must not find it accepted later."""
        original = redirect_store_journal.durability.fullsync

        def failing(_fd: int) -> None:
            raise OSError("device is gone")

        redirect_store_journal.durability.fullsync = failing
        self.addCleanup(
            setattr, redirect_store_journal.durability, "fullsync", original
        )

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.submit(operation_id="op")

        self.assertEqual(
            redirect_store_vocab.ERROR_JOURNAL_WRITE, caught.exception.reason
        )
        redirect_store_journal.durability.fullsync = original
        self.assertIsNone(self.read().request("op"))
