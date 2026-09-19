"""Prove the cutover harness acts only on production and invents no acceptance.

``tests/workbench_cutover.py`` does its real work against the two machines that
run the Companion, and against a person holding a phone. Neither can happen
here. What can be proved here is everything that would make the cutover
dangerous or its evidence worthless: that it refuses to act on anything but the
production installation, that a backup it says is good really can be read back,
and that the report it renders is one it observed rather than one it was handed.

The shape follows ``tests/test_workbench_staging.py``, which does this for the
rehearsal on the other side of the same document, and the rules the two harness
modules share with the acceptance reader are *pinned* to it rather than
restated. The cutover carries its own copy of the stage's checks so the Pi half
can be driven without this repository, and a copy that drifted would build
reports the gate rejects -- after the production service had been migrated
rather than before it.

The phone tests are the ones worth being stubborn about. Every refusal in
:meth:`tests.workbench_cutover.Journal.render` around the phone exists so that
the only way to a passing production report is a person actually answering, and
a test that could be satisfied by a default would quietly remove that.
"""

from __future__ import annotations

import json
import plistlib
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from asl_workbench import lifecycle, migration
from asl_workbench.installation import (
    CONFIG_PATH,
    DATABASE_PATH,
    DEPLOYMENT_PATH,
    RECEIPTS_DIRECTORY,
    SECRETS_PATH,
    atomic,
)
from asl_workbench.integrations import PI_SITE
from tests import workbench_acceptance as acceptance
from tests import workbench_cutover as cutover
from tests import workbench_staging as staging

MAC = "sglavoies-mac"
PI = "pi"
PHONE = "pixel"
ARTIFACT = "a" * 64
REVISION = "b" * 40

#: A moment the cutover could plausibly have happened at, and the release commit
#: it is evidence for. Fixed rather than sampled, so a failure means the same
#: thing every time it appears.
COMMITTED_AT = 1_700_000_000.0
READINESS_ASKED_AT = COMMITTED_AT + 3600.0
READINESS_GRANTED_AT = READINESS_ASKED_AT + 300.0
CONFIRMATION_ASKED_AT = READINESS_GRANTED_AT + 120.0
CONFIRMATION_ANSWERED_AT = CONFIRMATION_ASKED_AT + 240.0
RECORDED_AT = CONFIRMATION_ANSWERED_AT + 300.0


def production_prefix(root: Path, *, artifact: str | None = ARTIFACT) -> Path:
    """A prefix shaped like the one the cutover backs up and migrates."""
    prefix = root / cutover.PRODUCTION_PREFIX_NAME
    for directory in ("config", "credentials", "state", RECEIPTS_DIRECTORY):
        (prefix / directory).mkdir(parents=True, exist_ok=True)
    atomic(prefix / CONFIG_PATH, json.dumps({"target_id": "pi"}).encode())
    atomic(prefix / SECRETS_PATH, json.dumps({"credentials": []}).encode())
    atomic(
        prefix / f"{RECEIPTS_DIRECTORY}/homepage.json",
        json.dumps({"component": "homepage"}).encode(),
    )
    if artifact is not None:
        atomic(prefix / DEPLOYMENT_PATH, json.dumps({"artifact": artifact}).encode())
    database = prefix / DATABASE_PATH
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE plans (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO plans VALUES (?)", [("one",), ("two",), ("three",)]
        )
        connection.execute("INSERT INTO runs VALUES ('run')")
    database.chmod(0o600)
    return prefix


def complete_journal(path: Path, **overrides: Any) -> cutover.Journal:
    """A journal that recorded the whole cutover, for a test to then break."""
    journal = cutover.Journal(path, clock=lambda: RECORDED_AT)
    journal.release(REVISION, ARTIFACT)
    journal.host(
        "mac",
        host=MAC,
        system="Darwin",
        release=ARTIFACT,
        observed_at=COMMITTED_AT + 60,
    )
    journal.host(
        "pi", host=PI, system="Linux", release=ARTIFACT, observed_at=COMMITTED_AT + 60
    )
    for index, name in enumerate(cutover.PRODUCTION_CHECKS):
        journal.record(
            name,
            host="both",
            observed_at=COMMITTED_AT + 120.0 + index,
            **overrides.get(name, {}),
        )
    journal.phone_readiness(
        requested_at=READINESS_ASKED_AT, granted_at=READINESS_GRANTED_AT
    )
    confirmation: dict[str, Any] = {
        "requested_at": CONFIRMATION_ASKED_AT,
        "answered_at": CONFIRMATION_ANSWERED_AT,
        "verdict": cutover.VERDICT_ACCEPTED,
        "release": ARTIFACT,
        "host": PHONE,
        "checks": dict.fromkeys(cutover.PHONE_CHECKS, True),
    }
    journal.phone_confirmation(**{**confirmation, **overrides.get("phone", {})})
    return journal


class TemporaryCase(unittest.TestCase):
    """A case with a directory of its own to write prefixes and journals into."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)


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


def _digest_of(path: Path) -> dict[str, Any]:
    digest, size = cutover._digest_file(path)  # noqa: SLF001 - rewriting a fixture
    return {"sha256": digest, "bytes": size}


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
        with mock.patch.object(lifecycle, "connector_settings", return_value=settings):
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


if __name__ == "__main__":  # pragma: no cover - the discovered suite runs this
    unittest.main()
