from .workbench_acceptance_test_support import *


class AcceptedEvidence(AcceptanceCase):
    """The positive case, so every refusal below is about the change it made."""

    def test_each_stage_has_a_report_the_gate_accepts(self) -> None:
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                self.accepts(stage, valid_document(stage))

    def test_an_earlier_release_whose_package_never_changed_is_still_current(
        self,
    ) -> None:
        # Staleness is about the package, not about the calendar: a commit
        # behind HEAD that only moved documentation deployed the same software.
        fix = fixture()
        document = valid_document(acceptance.STAGE_RETIREMENT)
        document["release"] = {
            "revision": fix.package,
            "artifact": fix.digest_package,
        }
        for host in document["hosts"].values():
            host["release"] = fix.digest_package
        with mock.patch.dict(os.environ, {acceptance.ENV_RELEASE_REVISION: fix.docs}):
            self.accepts(acceptance.STAGE_RETIREMENT, document)

    def test_a_staging_rehearsal_remains_valid_after_package_changes(self) -> None:
        # Staging records a completed rehearsal, not the current production
        # deployment. A later package commit cannot undo what was exercised.
        fix = fixture()
        document = valid_document(acceptance.STAGE_STAGING)
        document["release"] = {
            "revision": fix.package,
            "artifact": fix.digest_package,
        }
        for host in document["hosts"].values():
            host["release"] = fix.digest_package
        self.accepts(acceptance.STAGE_STAGING, document)

    def test_the_template_carries_every_field_the_gate_requires(self) -> None:
        # What a deployment session is handed has to be what the gate reads, or
        # the session finds out field by field from failures.
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(
                    set(acceptance.template(stage)), set(valid_document(stage))
                )
                self.assertEqual(
                    set(acceptance.template(stage)["checks"]),
                    set(acceptance.STAGE_CHECKS[stage]),
                )


class MissingEvidence(AcceptanceCase):
    """ "We could not check" must never be spelled the way "it passed" is."""

    def test_a_report_that_was_never_written_is_refused(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for stage in acceptance.STAGES:
            with self.subTest(stage=stage):
                with self.assertRaises(acceptance.EvidenceError) as caught:
                    acceptance.verify(
                        stage,
                        repository=fixture().root,
                        path=directory / f"{stage}.json",
                    )
                self.assertIn("has not been recorded", str(caught.exception))

    def test_a_report_that_is_not_a_json_object_is_refused(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for body, because in (
            ("not json at all", "not readable as JSON"),
            ("[]", "not a JSON object"),
            ('"staging"', "not a JSON object"),
        ):
            with self.subTest(body=body):
                path = directory / "staging.json"
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(acceptance.EvidenceError) as caught:
                    acceptance.verify(
                        acceptance.STAGE_STAGING,
                        repository=fixture().root,
                        path=path,
                    )
                self.assertIn(because, str(caught.exception))

    def test_a_report_larger_than_the_bound_is_refused(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "staging.json"
        path.write_text("0" * (acceptance.MAX_DOCUMENT_BYTES + 1), encoding="utf-8")
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                acceptance.STAGE_STAGING, repository=fixture().root, path=path
            )
        self.assertIn("exceeds", str(caught.exception))

    def test_an_unreachable_pi_checkout_is_a_refusal_rather_than_a_skip(self) -> None:
        with (
            mock.patch.object(
                workbench_acceptance_contract, "locate_pi_repository", return_value=None
            ),
            self.assertRaises(acceptance.EvidenceError) as caught,
        ):
            acceptance.evidence_directory({})
        self.assertIn("no Pi checkout is reachable", str(caught.exception))

    def test_the_gate_reads_the_registered_pi_location(self) -> None:
        # The path is named by the plan, so it is asserted rather than assumed.
        pi = Path("/tmp/pi-checkout")
        with mock.patch.object(
            workbench_acceptance_contract, "locate_pi_repository", return_value=pi
        ):
            path = acceptance.evidence_path(acceptance.STAGE_PRODUCTION, {})
        self.assertEqual(
            path,
            pi / "services/pi-monitoring/docs/workbench/evidence/production.json",
        )


class WrongReleaseEvidence(AcceptanceCase):
    """A report is about one release, and the artifact is what says which."""

    def test_a_digest_that_the_named_commit_does_not_pack_to_is_refused(self) -> None:
        fix = fixture()
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(acceptance.STAGE_STAGING, "release.artifact", fix.digest_package),
            "this evidence is about another release",
        )

    def test_a_fabricated_digest_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "release.artifact", "0" * 64),
            "this evidence is about another release",
        )

    def test_a_revision_that_is_not_a_commit_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "release.revision", "f" * 40),
            "not a commit in this repository",
        )

    def test_a_revision_that_is_not_in_the_expected_form_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "release.revision", "HEAD"),
            "not in the expected form",
        )

    def test_a_host_running_another_release_is_refused(self) -> None:
        # The failure this rule exists for: a report that accepts one release
        # while one of the two machines is still running another.
        self.refuses(
            acceptance.STAGE_PRODUCTION,
            edited(
                acceptance.STAGE_PRODUCTION,
                "hosts.pi.release",
                fixture().digest_package,
            ),
            "not the release being accepted",
        )

    def test_a_staging_host_running_another_release_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(
                acceptance.STAGE_STAGING,
                "hosts.pi.release",
                fixture().digest_package,
            ),
            "not the release being accepted",
        )

    def test_retirement_must_name_the_accepted_production_deployment(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        retirement = valid_document(acceptance.STAGE_RETIREMENT)
        production = valid_document(acceptance.STAGE_PRODUCTION)
        production["release"] = {
            "revision": fixture().package,
            "artifact": fixture().digest_package,
        }
        for host in production["hosts"].values():
            host["release"] = fixture().digest_package
        retirement_path = directory / "retirement.json"
        retirement_path.write_text(json.dumps(retirement), encoding="utf-8")
        (directory / "production.json").write_text(
            json.dumps(production), encoding="utf-8"
        )
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                acceptance.STAGE_RETIREMENT,
                repository=fixture().root,
                path=retirement_path,
            )
        self.assertIn("accepted production deployment", str(caught.exception))

    def test_retirement_rechecks_the_production_phone_acceptance(self) -> None:
        retirement = valid_document(acceptance.STAGE_RETIREMENT)
        path = self.written(acceptance.STAGE_RETIREMENT, retirement)
        production_path = path.with_name("production.json")
        production = json.loads(production_path.read_text(encoding="utf-8"))
        production["phone"]["confirmation"]["verdict"] = "pending"
        production_path.write_text(json.dumps(production), encoding="utf-8")
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                acceptance.STAGE_RETIREMENT,
                repository=fixture().root,
                path=path,
            )
        self.assertIn("acceptance stays pending", str(caught.exception))


class StaleEvidence(AcceptanceCase):
    """Evidence about software this repository has since moved past."""

    def test_a_package_commit_after_the_release_makes_live_deployments_stale(
        self,
    ) -> None:
        fix = fixture()
        for stage in (acceptance.STAGE_PRODUCTION, acceptance.STAGE_RETIREMENT):
            with self.subTest(stage=stage):
                document = valid_document(stage)
                document["release"] = {
                    "revision": fix.package,
                    "artifact": fix.digest_package,
                }
                for host in document["hosts"].values():
                    host["release"] = fix.digest_package
                self.refuses(stage, document, "the evidence is stale")

    def test_a_release_that_was_never_merged_is_refused(self) -> None:
        fix = fixture()
        document = valid_document(acceptance.STAGE_STAGING)
        document["release"] = {
            "revision": fix.branch,
            "artifact": fix.digest_package,
        }
        for host in document["hosts"].values():
            host["release"] = fix.digest_package
        self.refuses(acceptance.STAGE_STAGING, document, "is not an ancestor")

    def test_a_report_that_predates_the_release_it_accepts_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_STAGING,
            edited(
                acceptance.STAGE_STAGING,
                "recorded_at",
                fixture().committed_at - 1,
            ),
            "predates the release it accepts",
        )

    def test_a_report_dated_in_the_future_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT,
                "recorded_at",
                fixture().now + acceptance.MAX_CLOCK_SKEW_SECONDS + 600,
            ),
            "dated in the future",
        )

    def test_an_observation_later_than_the_report_carrying_it_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(
                acceptance.STAGE_RETIREMENT,
                "hosts.mac.observed_at",
                fixture().now + 3600,
            ),
            "later than the report",
        )

    def test_the_release_under_acceptance_defaults_to_head(self) -> None:
        fix = fixture()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(acceptance.ENV_RELEASE_REVISION, None)
            self.assertEqual(acceptance.accepted_revision(fix.root), fix.repackaged)


class SyntheticEvidence(AcceptanceCase):
    """A suite on one laptop cannot produce a report about two real machines."""

    def test_provenance_that_is_not_the_actual_host_is_refused(self) -> None:
        for role in ("mac", "pi"):
            with self.subTest(role=role):
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    edited(
                        acceptance.STAGE_RETIREMENT, f"hosts.{role}.kind", "simulated"
                    ),
                    "is only ever recorded from the machine it happened on",
                )

    def test_a_hostname_that_names_a_fixture_is_refused(self) -> None:
        for name in ("localhost", "test-mac", "fake-pi", "mock-host", "pi.example"):
            with self.subTest(name=name):
                self.refuses(
                    acceptance.STAGE_RETIREMENT,
                    edited(acceptance.STAGE_RETIREMENT, "hosts.mac.host", name),
                    "names a fixture rather than a machine",
                )

    def test_a_host_running_the_wrong_operating_system_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.mac.system", "Linux"),
            "the mac runs Darwin",
        )
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.pi.system", "Darwin"),
            "the pi runs Linux",
        )

    def test_one_machine_recorded_as_two_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            edited(acceptance.STAGE_RETIREMENT, "hosts.pi.host", "studio-mini"),
            "recorded from one machine is not a two-host deployment",
        )

    def test_a_missing_host_is_refused(self) -> None:
        self.refuses(
            acceptance.STAGE_RETIREMENT,
            without(acceptance.STAGE_RETIREMENT, "hosts.pi"),
            "missing pi",
        )


__all__ = tuple(name for name in globals() if not name.startswith("__"))
