# mypy: ignore-errors
# ruff: noqa: F403, F405
"""Focused test_workbench_cutover test group 1."""

from ._test_workbench_cutover_support import *


class CutoverContract(unittest.TestCase):
    """What the harness copies from the gate and the rehearsal, pinned to both."""

    def test_the_checks_the_harness_records_are_the_checks_the_gate_requires(
        self,
    ) -> None:
        self.assertEqual(
            cutover.PRODUCTION_CHECKS,
            acceptance.STAGE_CHECKS[acceptance.STAGE_PRODUCTION],
        )

    def test_the_phone_checks_are_the_gate_s_phone_checks(self) -> None:
        self.assertEqual(cutover.PHONE_CHECKS, acceptance.PHONE_CHECKS)

    def test_the_shared_constants_are_the_gate_s_constants(self) -> None:
        for name in (
            "SCHEMA_VERSION",
            "HOST_ROLES",
            "OUTCOME_PASSED",
            "VERDICT_ACCEPTED",
            "PROVENANCE_ACTUAL_HOST",
            "MAX_CLOCK_SKEW_SECONDS",
            "FORBIDDEN_HOST_MARKERS",
        ):
            with self.subTest(constant=name):
                self.assertEqual(
                    getattr(cutover, name), getattr(acceptance, name), name
                )
        self.assertEqual(cutover.STAGE, acceptance.STAGE_PRODUCTION)

    def test_the_identifiers_a_rehearsal_moves_are_the_ones_a_cutover_keeps(
        self,
    ) -> None:
        # The two modules are inverses over the same list, which is only true
        # while it is the same list.
        self.assertEqual(cutover.OWNED_SITE_FIELDS, staging.OWNED_SITE_FIELDS)
        self.assertEqual(cutover.PRODUCTION_PREFIX_NAME, staging.PRODUCTION_PREFIX_NAME)


class ProductionOnly(unittest.TestCase):
    """A cutover acts on production, or it is not a cutover."""

    def test_the_production_site_is_accepted(self) -> None:
        cutover.check_production(PI_SITE)

    def test_the_rehearsal_s_own_site_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError):
            cutover.check_production(staging.staging_site())

    def test_moving_any_owned_identifier_aside_is_refused(self) -> None:
        moved = {
            "port": "9444",
            "bind_port": 8767,
            "public_origin": "https://pi.example.ts.net:9444",
            "publisher_user": "asl-companion-rehearsal",
            "publisher_topic": "asl-alerts-rehearsal",
            "marker": "# ASL companion rehearsal v1",
            "group_heading": "ASL Workbench rehearsal",
            "monitor_name": "ASL Workbench rehearsal health",
        }
        self.assertEqual(sorted(moved), sorted(cutover.OWNED_SITE_FIELDS))
        for field, value in moved.items():
            with (
                self.subTest(field=field),
                self.assertRaises(cutover.CutoverError) as caught,
            ):
                cutover.check_production(replace(PI_SITE, **{field: value}))
            self.assertIn(field, str(caught.exception))

    def test_a_site_that_calls_itself_anything_but_production_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError):
            cutover.check_production(replace(PI_SITE, deployment="staging"))

    def test_a_site_that_cannot_reach_the_phone_is_refused(self) -> None:
        # The deployment that takes the Companion's place is the one the phone
        # acceptance is then run against; a silent one could pass every
        # technical check and fail the only one that needs a person.
        with self.assertRaises(cutover.CutoverError):
            cutover.check_production(replace(PI_SITE, notifications=None))

    def test_a_changed_route_is_refused_even_though_it_is_not_owned(self) -> None:
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.check_production(replace(PI_SITE, summary_path="/v2/summary"))
        self.assertIn("summary_path", str(caught.exception))

    def test_the_production_prefix_is_accepted_and_others_are_not(self) -> None:
        cutover.check_prefix_production(Path("/home/pi/.local/share/asl-companion"))
        for name in (
            "asl-workbench-staging",
            "asl-companion-old",
            "asl-companion.bak",
            "",
        ):
            with self.subTest(name=name), self.assertRaises(cutover.CutoverError):
                cutover.check_prefix_production(Path(name))

    def test_no_command_will_act_on_anything_but_the_production_prefix(self) -> None:
        for action in ("backup", "owner", "migrate", "status", "install", "remove"):
            with self.subTest(action=action), self.assertRaises(cutover.CutoverError):
                cutover.main(
                    [
                        action,
                        "--prefix",
                        "/tmp/asl-workbench-staging",
                        "--destination",
                        "/tmp/out",
                    ]
                )

    def test_the_site_command_reports_the_production_route_and_names(self) -> None:
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cutover.main(["site"]), 0)
        reported = json.loads(printed.call_args.args[0])
        self.assertEqual(reported["public_origin"], PI_SITE.public_origin)
        self.assertEqual(reported["bind_port"], PI_SITE.bind_port)
        self.assertEqual(reported["label"], lifecycle.LABEL)
        self.assertEqual(reported["unit"], lifecycle.UNIT)
        self.assertEqual(reported["prefix"], cutover.PRODUCTION_PREFIX_NAME)


class Backups(TemporaryCase):
    """A backup nobody has read back is a hope, not a rollback."""

    def setUp(self) -> None:
        super().setUp()
        self.prefix = production_prefix(self.root)
        self.destination = self.root / "cutover-backups"

    def capture(self) -> dict[str, Any]:
        return cutover.capture_backup(
            self.prefix,
            self.destination,
            role=lifecycle.ROLE_SERVICE,
            clock=lambda: COMMITTED_AT + 60.0,
        )

    def archive(self, receipt: dict[str, Any]) -> Path:
        return self.destination / receipt["archive"]

    def test_a_backup_holds_every_member_the_prefix_carries(self) -> None:
        receipt = self.capture()
        self.assertEqual(
            sorted(receipt["files"]), sorted(migration.members_of(self.prefix))
        )
        self.assertEqual(receipt["role"], lifecycle.ROLE_SERVICE)
        self.assertEqual(receipt["artifact"], ARTIFACT)

    def test_a_backup_records_what_continuity_is_then_compared_against(self) -> None:
        receipt = self.capture()
        self.assertEqual(receipt["tables"][DATABASE_PATH], {"plans": 3, "runs": 1})

    def test_a_captured_backup_verifies(self) -> None:
        receipt = self.capture()
        self.assertEqual(cutover.verify_backup(self.archive(receipt)), receipt)

    def test_a_backup_of_a_prefix_being_written_to_is_still_consistent(self) -> None:
        # The service is running when the backup is taken, which is why the
        # database goes through SQLite rather than the filesystem.
        with closing(sqlite3.connect(self.prefix / DATABASE_PATH)) as connection:
            connection.execute("BEGIN")
            connection.execute("INSERT INTO plans VALUES ('four')")
            receipt = self.capture()
            connection.rollback()
        self.assertEqual(receipt["tables"][DATABASE_PATH]["plans"], 3)
        cutover.verify_backup(self.archive(receipt))

    def test_a_backup_is_written_outside_the_prefix_it_protects(self) -> None:
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.capture_backup(
                self.prefix,
                self.prefix / "backups" / "here",
                role=lifecycle.ROLE_SERVICE,
            )
        self.assertIn("inside the prefix", str(caught.exception))

    def test_a_backup_of_something_that_is_not_production_is_refused(self) -> None:
        other = production_prefix(self.root / "elsewhere").rename(
            self.root / "asl-workbench-staging"
        )
        with self.assertRaises(cutover.CutoverError):
            cutover.capture_backup(other, self.destination, role=lifecycle.ROLE_SERVICE)

    def test_a_backup_for_a_role_nothing_is_deployed_under_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError):
            cutover.capture_backup(self.prefix, self.destination, role="laptop")

    def test_a_prefix_that_predates_releases_is_still_backed_up(self) -> None:
        # The first backup of the cutover is of the legacy installation, which
        # has no deployment document at all.
        prefix = production_prefix(self.root / "legacy", artifact=None)
        receipt = cutover.capture_backup(
            prefix, self.root / "legacy-backups", role=lifecycle.ROLE_CONNECTOR
        )
        self.assertEqual(receipt["artifact"], "")
        cutover.verify_backup(self.root / "legacy-backups" / receipt["archive"])

    def test_an_archive_that_changed_since_it_was_written_is_refused(self) -> None:
        receipt = self.capture()
        archive = self.archive(receipt)
        archive.write_bytes(archive.read_bytes()[:-1024])
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.verify_backup(archive)
        self.assertIn("is not the archive", str(caught.exception))

    def test_an_archive_missing_a_member_it_claims_is_refused(self) -> None:
        receipt = self.capture()
        archive = self.archive(receipt)
        name = receipt["archive"].removesuffix(".tar")
        with tarfile.open(archive) as bundle:
            kept = [
                member
                for member in bundle.getmembers()
                if not member.name.endswith(SECRETS_PATH)
            ]
            extracted = {
                member.name: bundle.extractfile(member).read()  # type: ignore[union-attr]
                for member in kept
            }
        with tarfile.open(archive, "w") as rebuilt:
            for member_name, data in extracted.items():
                path = self.root / "staged" / Path(member_name).name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                rebuilt.add(path, arcname=member_name)
        self.assertTrue(name)
        atomic(
            archive.with_suffix(".json"),
            json.dumps(
                {**receipt, **_digest_of(archive)}, indent=2, sort_keys=True
            ).encode(),
        )
        archive.with_suffix(".json").chmod(0o600)
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.verify_backup(archive)
        self.assertIn("missing", str(caught.exception))

    def test_an_archive_that_carries_a_path_of_its_own_is_refused(self) -> None:
        receipt = self.capture()
        archive = self.archive(receipt)
        escape = self.root / "escape"
        escape.write_bytes(b"x")
        with tarfile.open(archive, "a") as bundle:
            bundle.add(escape, arcname="../escape")
        atomic(
            archive.with_suffix(".json"),
            json.dumps(
                {**receipt, **_digest_of(archive)}, indent=2, sort_keys=True
            ).encode(),
        )
        archive.with_suffix(".json").chmod(0o600)
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.verify_backup(archive)
        self.assertIn("not a member", str(caught.exception))

    def test_a_receipt_for_another_archive_is_refused(self) -> None:
        first = self.capture()
        archive = self.archive(first)
        other = cutover.capture_backup(
            self.prefix,
            self.destination,
            role=lifecycle.ROLE_CONNECTOR,
            clock=lambda: COMMITTED_AT + 120.0,
        )
        archive.write_bytes((self.destination / other["archive"]).read_bytes())
        with self.assertRaises(cutover.CutoverError):
            cutover.verify_backup(archive)

    def test_row_counts_are_recomputed_rather_than_read_back(self) -> None:
        receipt = self.capture()
        sidecar = self.archive(receipt).with_suffix(".json")
        claimed = json.loads(sidecar.read_bytes())
        claimed["tables"][DATABASE_PATH]["plans"] = 4
        atomic(sidecar, json.dumps(claimed).encode())
        sidecar.chmod(0o600)
        with self.assertRaises(cutover.CutoverError):
            cutover.verify_backup(self.archive(receipt))

    def test_a_database_that_cannot_be_read_is_refused_rather_than_counted(
        self,
    ) -> None:
        broken = self.root / "broken.db"
        broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        with self.assertRaises(cutover.CutoverError):
            cutover.table_counts(broken)

    def test_a_member_list_that_grew_to_hold_releases_is_refused(self) -> None:
        with (
            mock.patch.object(
                migration, "members_of", return_value=("releases/artifact.zip",)
            ),
            self.assertRaises(cutover.CutoverError),
        ):
            cutover.backup_members(self.prefix)
