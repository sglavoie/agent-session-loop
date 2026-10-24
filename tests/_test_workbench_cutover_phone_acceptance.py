# mypy: ignore-errors
# ruff: noqa: F403, F405
"""Workbench cutover phone acceptance."""

from ._test_workbench_cutover_support import *


class PhoneAcceptance(TemporaryCase):
    """The half a person answers, and the refusals that keep it theirs."""

    def journal(self, **overrides: Any) -> cutover.Journal:
        return complete_journal(self.root / "journal.json", **overrides)

    def render(self, journal: cutover.Journal) -> dict[str, Any]:
        return journal.render(committed_at=COMMITTED_AT, recorded_at=RECORDED_AT)

    def pending(self) -> cutover.Journal:
        """A cutover that asked, and is waiting. The plan's default state."""
        journal = cutover.Journal(self.root / "pending.json", clock=lambda: RECORDED_AT)
        journal.release(REVISION, ARTIFACT)
        journal.host("mac", host=MAC, system="Darwin", release=ARTIFACT)
        journal.host("pi", host=PI, system="Linux", release=ARTIFACT)
        for name in cutover.PRODUCTION_CHECKS:
            journal.record(name, host="both", observed_at=COMMITTED_AT + 120.0)
        return journal

    def test_a_fixture_cannot_be_started_before_the_operator_says_when(self) -> None:
        journal = self.pending()
        with self.assertRaises(cutover.CutoverError) as caught:
            journal.granted()
        self.assertIn("has not declared the phone ready", str(caught.exception))
        journal.phone_readiness(requested_at=READINESS_ASKED_AT)
        with self.assertRaises(cutover.CutoverError):
            journal.granted()
        journal.phone_readiness(
            requested_at=READINESS_ASKED_AT, granted_at=READINESS_GRANTED_AT
        )
        self.assertEqual(journal.granted(), READINESS_GRANTED_AT)

    def test_a_confirmation_cannot_be_asked_for_before_readiness_is_granted(
        self,
    ) -> None:
        journal = self.pending()
        with self.assertRaises(cutover.CutoverError):
            journal.phone_confirmation(
                requested_at=CONFIRMATION_ASKED_AT, release=ARTIFACT, host=PHONE
            )
        journal.phone_readiness(
            requested_at=READINESS_ASKED_AT, granted_at=READINESS_GRANTED_AT
        )
        with self.assertRaises(cutover.CutoverError) as caught:
            journal.phone_confirmation(
                requested_at=READINESS_ASKED_AT, release=ARTIFACT, host=PHONE
            )
        self.assertIn("before the operator declared", str(caught.exception))

    def test_an_unanswered_request_leaves_acceptance_pending(self) -> None:
        journal = self.pending()
        journal.phone_readiness(
            requested_at=READINESS_ASKED_AT, granted_at=READINESS_GRANTED_AT
        )
        journal.phone_confirmation(
            requested_at=CONFIRMATION_ASKED_AT, release=ARTIFACT, host=PHONE
        )
        self.assertEqual(
            journal.data["phone"]["confirmation"]["verdict"], cutover.VERDICT_PENDING
        )
        self.assertEqual(journal.missing(), [])
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("stays pending until they answer", str(caught.exception))

    def test_a_cutover_that_never_asked_cannot_be_rendered(self) -> None:
        journal = self.pending()
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("never asked to ready the phone", str(caught.exception))
        journal.phone_readiness(
            requested_at=READINESS_ASKED_AT, granted_at=READINESS_GRANTED_AT
        )
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("never asked to confirm", str(caught.exception))

    def test_an_acceptance_answers_all_four_questions(self) -> None:
        for name in cutover.PHONE_CHECKS:
            answers = dict.fromkeys(cutover.PHONE_CHECKS, True)
            answers[name] = False
            with (
                self.subTest(unconfirmed=name),
                self.assertRaises(cutover.CutoverError) as caught,
            ):
                self.journal(phone={"checks": answers})
            self.assertIn(name, str(caught.exception))

    def test_an_answer_to_a_question_nobody_asked_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError):
            self.journal(phone={"checks": {"phone-worked": True}})

    def test_a_partial_acceptance_recorded_around_the_writer_cannot_be_rendered(
        self,
    ) -> None:
        journal = self.journal()
        journal.data["phone"]["confirmation"]["checks"]["live-reply"] = False
        journal.save()
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("live-reply", str(caught.exception))

    def test_an_acceptance_of_another_release_is_not_this_one_s(self) -> None:
        journal = self.journal(phone={"release": "c" * 64})
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("confirmed a release", str(caught.exception))

    def test_an_acceptance_from_a_deployed_host_is_not_a_phone_acceptance(self) -> None:
        for host in (MAC, PI):
            with (
                self.subTest(host=host),
                self.assertRaises(cutover.CutoverError) as caught,
            ):
                self.journal(phone={"host": host})
            self.assertIn("deployed host", str(caught.exception))

    def test_an_acceptance_from_a_fixture_is_refused(self) -> None:
        for host in ("test-phone", "localhost", "simulated-pixel", "my-fixture"):
            with self.subTest(host=host), self.assertRaises(cutover.CutoverError):
                self.journal(phone={"host": host})

    def test_an_acceptance_from_something_that_is_not_a_device_is_refused(self) -> None:
        for host in ("", "a phone", "PIXEL", "pixel/7"):
            with self.subTest(host=host), self.assertRaises(cutover.CutoverError):
                self.journal(phone={"host": host})

    def test_an_answer_that_predates_its_question_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError):
            self.journal(phone={"answered_at": CONFIRMATION_ASKED_AT - 1.0})
        journal = cutover.Journal(self.root / "readiness.json")
        with self.assertRaises(cutover.CutoverError):
            journal.phone_readiness(
                requested_at=READINESS_ASKED_AT, granted_at=READINESS_ASKED_AT - 1.0
            )

    def test_an_accepted_verdict_without_a_moment_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError):
            self.journal(phone={"answered_at": None})

    def test_a_verdict_nobody_gave_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError):
            self.journal(phone={"verdict": "probably"})

    def test_an_acceptance_outside_the_window_cannot_be_rendered(self) -> None:
        for moment in (COMMITTED_AT - 1.0, RECORDED_AT + 3600.0):
            journal = self.journal()
            journal.data["phone"]["confirmation"]["answered_at"] = moment
            journal.save()
            with self.subTest(moment=moment), self.assertRaises(cutover.CutoverError):
                self.render(journal)

    def test_an_acceptance_reordered_around_the_writer_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["phone"]["readiness"]["granted_at"] = CONFIRMATION_ASKED_AT + 1.0
        journal.save()
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("before the phone was declared ready", str(caught.exception))
