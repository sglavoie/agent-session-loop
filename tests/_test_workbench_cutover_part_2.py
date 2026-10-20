# mypy: ignore-errors
# ruff: noqa: F403, F405
"""Focused test_workbench_cutover test group 2."""

from ._test_workbench_cutover_support import *


class SingleOwner(TemporaryCase):
    """One production process under one name, on each host."""

    def setUp(self) -> None:
        super().setUp()
        self.prefix = self.root / cutover.PRODUCTION_PREFIX_NAME
        self.definitions = self.prefix / lifecycle.DEFINITIONS_DIRECTORY
        self.definitions.mkdir(parents=True)

    def install_unit(self, prefix: Path | None = None) -> Path:
        name, content = lifecycle.definition(
            prefix or self.prefix,
            self.prefix / "releases" / "current",
            "/usr/bin/python3",
            lifecycle.ROLE_SERVICE,
        )
        path = self.definitions / name
        path.write_bytes(content)
        return path

    def install_agent(self, origin: str = PI_SITE.public_origin) -> Path:
        settings = {
            "asl": str(self.root / "asl"),
            "asl_state": str(self.root / "state"),
            "target_id": "pi",
            "connector_id": "mac",
            "plan_roots": [str(self.root / "plans")],
            "settings": {"public_origin": origin},
        }
        with mock.patch.object(
            lifecycle_attachment, "connector_settings", return_value=settings
        ):
            name, content = lifecycle.definition(
                self.prefix, self.prefix, "/usr/bin/python3", lifecycle.ROLE_CONNECTOR
            )
        path = self.definitions / name
        path.write_bytes(content)
        return path

    def test_the_unit_the_release_writes_is_accepted_under_the_companion_s_name(
        self,
    ) -> None:
        self.install_unit()
        found = cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_SERVICE)
        self.assertEqual(found["unit"], lifecycle.UNIT)
        self.assertIn(str(self.prefix), found["exec_start"])

    def test_the_launch_agent_the_release_writes_is_accepted(self) -> None:
        self.install_agent()
        found = cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_CONNECTOR)
        self.assertEqual(found["label"], lifecycle.LABEL)
        self.assertEqual(found["origin"], PI_SITE.public_origin)

    def test_a_connector_pointed_away_from_the_private_url_is_refused(self) -> None:
        self.install_agent(origin="https://pi.example.ts.net:9444")
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_CONNECTOR)
        self.assertIn("private URL", str(caught.exception))

    def test_a_definition_left_over_from_a_rehearsal_is_refused(self) -> None:
        path = self.install_agent()
        parsed = plistlib.loads(path.read_bytes())
        parsed["Label"] = staging.STAGING_LABEL
        path.write_bytes(plistlib.dumps(parsed))
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_CONNECTOR)
        self.assertIn(lifecycle.LABEL, str(caught.exception))

    def test_a_definition_that_points_at_another_prefix_is_refused(self) -> None:
        self.install_unit(prefix=self.root / "asl-companion-old")
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_SERVICE)
        self.assertIn("does not point at this prefix", str(caught.exception))

    def test_an_agent_that_runs_somewhere_else_is_refused(self) -> None:
        path = self.install_agent()
        parsed = plistlib.loads(path.read_bytes())
        parsed["WorkingDirectory"] = str(self.root / "asl-companion-old")
        path.write_bytes(plistlib.dumps(parsed))
        with self.assertRaises(cutover.CutoverError):
            cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_CONNECTOR)

    def test_a_unit_that_carries_a_site_of_its_own_is_refused(self) -> None:
        path = self.install_unit()
        path.write_bytes(
            path.read_text(encoding="utf-8")
            .replace("--prefix", f"--site {PI_SITE.bind_port} --prefix")
            .encode()
        )
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_SERVICE)
        self.assertIn("moves its site aside", str(caught.exception))

    def test_nothing_installed_is_named_rather_than_passed(self) -> None:
        for role in (lifecycle.ROLE_SERVICE, lifecycle.ROLE_CONNECTOR):
            with self.subTest(role=role), self.assertRaises(cutover.CutoverError):
                cutover.check_single_owner(self.prefix, role=role)

    def test_a_unit_without_a_command_is_refused(self) -> None:
        (self.definitions / lifecycle.UNIT).write_text("[Service]\n", encoding="utf-8")
        with self.assertRaises(cutover.CutoverError):
            cutover.check_single_owner(self.prefix, role=lifecycle.ROLE_SERVICE)


class JournalRefusals(TemporaryCase):
    """A report is what was observed, or it is not a report."""

    def journal(self, **overrides: Any) -> cutover.Journal:
        return complete_journal(self.root / "journal.json", **overrides)

    def render(self, journal: cutover.Journal) -> dict[str, Any]:
        return journal.render(committed_at=COMMITTED_AT, recorded_at=RECORDED_AT)

    def test_a_complete_cutover_renders(self) -> None:
        document = self.render(self.journal())
        self.assertEqual(document["stage"], acceptance.STAGE_PRODUCTION)
        self.assertEqual(document["release"]["artifact"], ARTIFACT)
        self.assertEqual(document["phone"]["confirmation"]["host"], PHONE)

    def test_the_rendered_report_is_one_the_gate_accepts(self) -> None:
        # The gate's own structural rules, run against the harness's output.
        # Everything the gate checks against git -- the revision, the rebuilt
        # digest, the staleness rule -- needs a real repository and belongs to
        # tests/test_workbench_acceptance.py; everything else is proved here, so
        # a drift between the two modules fails before a cutover rather than
        # after one.
        document = self.render(self.journal())
        acceptance.check_sanitized(document, acceptance.STAGE_PRODUCTION)
        acceptance.require_keys(
            document,
            ("schema", "stage", "recorded_at", "release", "hosts", "checks", "phone"),
            acceptance.STAGE_PRODUCTION,
        )
        hosts = {}
        for role in acceptance.HOST_ROLES:
            hosts[role] = acceptance.check_provenance(
                document["hosts"][role], role, f"production.hosts.{role}"
            )
        acceptance.check_checks(
            document, acceptance.STAGE_PRODUCTION, COMMITTED_AT, RECORDED_AT
        )
        acceptance.check_phone(document, ARTIFACT, hosts, COMMITTED_AT, RECORDED_AT)

    def test_the_journal_survives_the_process_that_wrote_it(self) -> None:
        # The cutover spans two hosts and an unbounded wait for a person; a
        # journal that only existed in memory would lose the technical half,
        # which is the half that cannot simply be redone.
        self.journal()
        reopened = cutover.Journal(self.root / "journal.json")
        self.assertEqual(reopened.missing(), [])
        self.render(reopened)

    def test_an_unrecorded_check_is_named_rather_than_assumed(self) -> None:
        journal = self.journal()
        del journal.data["checks"]["old-links"]
        journal.save()
        self.assertEqual(journal.missing(), ["old-links"])
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("old-links", str(caught.exception))

    def test_a_check_that_did_not_pass_cannot_be_rendered(self) -> None:
        journal = self.journal(integrations={"outcome": "failed"})
        self.assertEqual(journal.missing(), ["integrations"])
        with self.assertRaises(cutover.CutoverError):
            self.render(journal)

    def test_a_failed_check_that_is_then_redone_replaces_it(self) -> None:
        journal = self.journal(integrations={"outcome": "failed"})
        journal.record("integrations", host="pi", observed_at=COMMITTED_AT + 3000.0)
        self.assertEqual(journal.missing(), [])
        self.render(journal)

    def test_a_check_the_stage_does_not_owe_is_refused_at_the_point_of_writing(
        self,
    ) -> None:
        journal = cutover.Journal(self.root / "fresh.json")
        with self.assertRaises(cutover.CutoverError):
            journal.record("everything-worked", host="both")
        with self.assertRaises(cutover.CutoverError):
            journal.record("migration", host="laptop")
        with self.assertRaises(cutover.CutoverError):
            journal.host("router", host="r", system="Linux", release=ARTIFACT)
        with self.assertRaises(cutover.CutoverError):
            journal.release("short", ARTIFACT)
        with self.assertRaises(cutover.CutoverError):
            journal.release(REVISION, "short")

    def test_a_cutover_without_a_release_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["release"] = None
        journal.save()
        with self.assertRaises(cutover.CutoverError):
            self.render(journal)

    def test_a_host_that_ran_another_release_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.host("pi", host=PI, system="Linux", release="c" * 64)
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("release this report does not accept", str(caught.exception))

    def test_a_host_of_the_wrong_kind_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.host("pi", host=PI, system="Darwin", release=ARTIFACT)
        with self.assertRaises(cutover.CutoverError):
            self.render(journal)

    def test_one_machine_cannot_be_both_halves(self) -> None:
        journal = self.journal()
        journal.host("mac", host=PI, system="Darwin", release=ARTIFACT)
        with self.assertRaises(cutover.CutoverError) as caught:
            self.render(journal)
        self.assertIn("same machine", str(caught.exception))

    def test_a_missing_host_cannot_be_rendered(self) -> None:
        journal = self.journal()
        del journal.data["hosts"]["mac"]
        journal.save()
        with self.assertRaises(cutover.CutoverError):
            self.render(journal)

    def test_an_observation_outside_the_window_cannot_be_rendered(self) -> None:
        for moment in (COMMITTED_AT - 1.0, RECORDED_AT + 3600.0):
            journal = self.journal()
            journal.record("migration", host="pi", observed_at=moment)
            with self.subTest(moment=moment), self.assertRaises(cutover.CutoverError):
                self.render(journal)

    def test_an_unparseable_moment_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["checks"]["migration"]["observed_at"] = "recently"
        journal.save()
        with self.assertRaises(cutover.CutoverError):
            self.render(journal)

    def test_the_report_is_dated_by_the_clock_rather_than_by_its_author(self) -> None:
        journal = self.journal()
        self.assertEqual(
            journal.render(committed_at=COMMITTED_AT)["recorded_at"], RECORDED_AT
        )
