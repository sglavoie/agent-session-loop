from .workbench_acceptance_test_support import *


class ProductionConfirmation(AcceptanceCase):
    """The operator's own answer, in the order they were asked for it."""

    def test_an_unanswered_confirmation_keeps_acceptance_pending(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.verdict", "pending"
            ),
            "acceptance stays pending until the operator answers",
        )

    def test_a_refused_confirmation_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.verdict", "declined"
            ),
            "acceptance stays pending",
        )

    def test_a_missing_phone_block_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            without(acceptance.STAGE_PRODUCTION, "phone"),
            "missing phone",
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            without(acceptance.STAGE_PRODUCTION, "phone.readiness"),
            "missing readiness",
        )

    def test_readiness_granted_before_it_was_asked_for_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "phone.readiness.granted_at",
                fixture().now - 6100,
            ),
            "granted before it was asked for",
        )

    def test_a_confirmation_asked_before_readiness_was_granted_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "phone.confirmation.requested_at",
                fixture().now - 5950,
            ),
            "before the operator declared the phone ready",
        )

    def test_a_confirmation_answered_before_it_was_asked_is_refused(self) -> None:
        document = valid_document(acceptance.STAGE_PRODUCTION)
        document["phone"]["confirmation"]["answered_at"] = (
            document["phone"]["confirmation"]["requested_at"] - 10
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION, document, "answered before it was asked"
        )

    def test_a_confirmation_of_another_release_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "phone.confirmation.release",
                fixture().digest_package,
            ),
            "not the release being accepted",
        )

    def test_a_confirmation_that_did_not_come_from_the_phone_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(acceptance.STAGE_PRODUCTION, "phone.confirmation.kind", "recorded"),
            "only ever recorded from the phone",
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.host", "studio-mini"
            ),
            "confirmed from the Mac or the Pi is not a phone acceptance",
        )
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION, "phone.confirmation.host", "test-phone"
            ),
            "names a fixture rather than a phone",
        )

    def test_every_phone_check_must_be_confirmed(self) -> None:
        for name in acceptance.PHONE_CHECKS:
            with self.subTest(name=name):
                self.refuses(
                    acceptance.STAGE_PRODUCTION,
                    edited(
                        acceptance.STAGE_PRODUCTION,
                        f"phone.confirmation.checks.{name}",
                        False,
                    ),
                    f"checks.{name}: not confirmed",
                )
                self.refuses(
                    acceptance.STAGE_PRODUCTION,
                    without(
                        acceptance.STAGE_PRODUCTION,
                        f"phone.confirmation.checks.{name}",
                    ),
                    f"missing {name}",
                )

    def test_only_production_carries_a_phone_confirmation(self) -> None:
        # Staging and retirement are not the sessions that ask the operator, so
        # a phone block in either is a field the reader does not recognize.
        for stage in (acceptance.STAGE_STAGING, acceptance.STAGE_RETIREMENT):
            with self.subTest(stage=stage):
                document = valid_document(stage)
                document["phone"] = valid_document(acceptance.STAGE_PRODUCTION)["phone"]
                self.refuses(stage, document, "unrecognized phone")


__all__ = tuple(name for name in globals() if not name.startswith("__"))
