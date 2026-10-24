from .workbench_acceptance_test_support import *


class SanitizedEvidence(AcceptanceCase):
    """The reports are pushed, so the reader is also a privacy gate."""

    def test_an_unrecognized_key_is_refused_rather_than_ignored(self) -> None:
        for path, value in (
            ("operator_note", "a note"),
            ("hosts.mac.plan_root", "somewhere"),
            ("release.branch", "main"),
        ):
            with self.subTest(path=path):
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    edited(acceptance.STAGE_RETIREMENT, path, value),
                    "unrecognized",
                )

    def test_a_string_carrying_a_path_or_an_identity_is_refused(self) -> None:
        for value in (
            "Users-someone-plans",
            "shed-pi.local\nsecond line",
            "someone-at-somewhere",
        ):
            with self.subTest(value=value):
                document = valid_document(acceptance.STAGE_RETIREMENT)
                # Placed in a key the schema does allow, so what fires is the
                # content rule rather than the allowlist.
                document["hosts"]["mac"]["host"] = value.replace("-at-", "@").replace(
                    "Users-", "/Users/"
                )
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    document,
                    "a sanitized report carries no",
                )

    def test_an_overlong_string_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT,
                "hosts.mac.system",
                "D" * (acceptance.MAX_STRING_LENGTH + 1),
            ),
            f"longer than {acceptance.MAX_STRING_LENGTH}",
        )

    def test_a_value_of_a_type_the_schema_never_uses_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.mac.observed_at", None),
            "NoneType is not allowed in a report",
        )

    def test_a_negative_number_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "recorded_at", -1),
            "not a usable number",
        )

    def test_an_unbounded_list_is_refused(self) -> None:
        document = valid_document(acceptance.STAGE_RETIREMENT)
        document["hosts"]["mac"]["system"] = ["Darwin"] * (
            acceptance.MAX_SEQUENCE_LENGTH + 1
        )
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            document,
            f"more than {acceptance.MAX_SEQUENCE_LENGTH} entries",
        )

    def test_a_report_that_names_another_stage_is_refused(self) -> None:
        document = valid_document(acceptance.STAGE_PRODUCTION)
        document["stage"] = acceptance.STAGE_STAGING
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            document,
            "a report read as one stage may not describe another",
        )

    def test_a_report_in_another_schema_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT, "schema", acceptance.SCHEMA_VERSION + 1
            ),
            f"accepts schema {acceptance.SCHEMA_VERSION}",
        )


class StageChecks(AcceptanceCase):
    """Each stage owes the checks its own completion contract names."""

    def test_a_check_recorded_as_anything_but_passed_is_refused(self) -> None:
        for stage in acceptance.STAGES:
            name = acceptance.STAGE_CHECKS[stage][0]
            with self.subTest(stage=stage):
                self.refuses(
                    stage,
                    edited(stage, f"checks.{name}.outcome", "skipped"),
                    f"checks.{name}: recorded as 'skipped'",
                )

    def test_an_omitted_check_is_refused_rather_than_read_as_a_shorter_list(
        self,
    ) -> None:
        for stage in acceptance.STAGES:
            name = acceptance.STAGE_CHECKS[stage][-1]
            with self.subTest(stage=stage):
                self.refuses(stage, without(stage, f"checks.{name}"), f"missing {name}")

    def test_a_check_attributed_to_no_deployed_host_is_refused(self) -> None:
        name = acceptance.STAGE_CHECKS[acceptance.STAGE_RETIREMENT][0]
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, f"checks.{name}.host", "laptop"),
            "is not a deployed host",
        )

    def test_a_check_observed_before_the_release_existed_is_refused(self) -> None:
        name = acceptance.STAGE_CHECKS[acceptance.STAGE_STAGING][0]
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(
                acceptance.STAGE_STAGING,
                f"checks.{name}.observed_at",
                fixture().committed_at - 60,
            ),
            "predates the release it is evidence for",
        )

    def test_each_stage_owes_the_checks_its_own_contract_names(self) -> None:
        # A stage cannot be accepted by another stage's evidence simply because
        # the two documents look alike.
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                others: set[str] = set()
                for other in acceptance.STAGES:
                    if other != stage:
                        others |= set(acceptance.STAGE_CHECKS[other])
                self.assertTrue(set(acceptance.STAGE_CHECKS[stage]) - others)


class StagingSoak(AcceptanceCase):
    """Staging owes a full hour, in three named phases."""

    def test_a_soak_shorter_than_an_hour_is_refused(self) -> None:
        fix = fixture()
        document = valid_document(acceptance.STAGE_STAGING)
        document["soak"]["ended_at"] = document["soak"]["started_at"] + 1800
        document["soak"]["phases"] = {"active": 900, "idle": 600, "reconnect": 300}
        self.assertLess(document["soak"]["ended_at"], fix.now)
        self.refuses(acceptance.STAGE_STAGING, document, "short of the 3600s soak")

    def test_a_phase_that_never_happened_is_refused(self) -> None:
        for phase in acceptance.SOAK_PHASES:
            with self.subTest(phase=phase):
                self.refuses(
                    acceptance.STAGE_STAGING,
                    edited(acceptance.STAGE_STAGING, f"soak.phases.{phase}", 0),
                    f"phases.{phase}: never happened",
                )

    def test_an_omitted_phase_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            without(acceptance.STAGE_STAGING, "soak.phases.reconnect"),
            "missing reconnect",
        )

    def test_phases_that_add_up_to_more_than_the_soak_are_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(acceptance.STAGE_STAGING, "soak.phases.active", 9000),
            "add up to more than the soak lasted",
        )

    def test_a_soak_the_other_stages_do_not_carry(self) -> None:
        # The block is staging's, so the other two stages refuse it as a key
        # they do not recognize rather than silently carrying it.
        document = valid_document(acceptance.STAGE_PRODUCTION)
        document["soak"] = valid_document(acceptance.STAGE_STAGING)["soak"]
        self.refuses(acceptance.STAGE_PRODUCTION, document, "unrecognized soak")


__all__ = tuple(name for name in globals() if not name.startswith("__"))
