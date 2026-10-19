"""Prove the staging harness cannot stage over production or invent evidence.

``tests/workbench_staging.py`` is the other module in this tree whose real work
happens against two machines rather than a fixture: it installs services, moves
a Serve route, and records an hour of soak. None of that can run here, and the
two things that would make it dangerous or worthless both can be proved here --
that it never names a production resource, and that the report it produces is
one it actually observed.

The shape follows ``tests/test_workbench_acceptance.py``, which does the same
job for the reader on the other side of the same document: a non-discovered
module that only runs when the hosts are in front of it, and a discovered test
beside it that keeps the module honest in the meantime.

Where a rule is shared with the acceptance reader it is *pinned* to it rather
than restated, because the harness carries its own copy of the stage's checks
so the Pi half can be driven without this repository, and a copy that drifts
would build reports the gate then rejects -- after the hour of soak rather than
before it.
"""

from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from asl_workbench import lifecycle, lifecycle_attachment, runtime
from asl_workbench.http import MODE_REMOTE
from asl_workbench.integrations import PI_SITE
from asl_workbench.notifications import (
    DEPLOYMENT_PRODUCTION,
    PI_NOTIFICATION_SITE,
    NotificationError,
    select_publisher,
)
from tests import workbench_acceptance as acceptance
from tests import workbench_staging as staging

MAC = "sglavoies-mac"
PI = "pi"
ARTIFACT = "a" * 64
REVISION = "b" * 40

#: A moment the rehearsal could plausibly have happened at, and the release
#: commit it would have been evidence for. Fixed rather than sampled: a test
#: that reads the clock proves a different thing every time it runs.
COMMITTED_AT = 1_700_000_000.0
SOAK_STARTED_AT = COMMITTED_AT + 600.0
SOAK_ENDED_AT = SOAK_STARTED_AT + staging.SOAK_MINIMUM_SECONDS
RECORDED_AT = SOAK_ENDED_AT + 600.0


def complete_journal(path: Path, **overrides: Any) -> staging.Journal:
    """A journal that recorded the whole rehearsal, for a test to then break."""
    journal = staging.Journal(path, clock=lambda: RECORDED_AT)
    journal.release(REVISION, ARTIFACT)
    journal.host(
        "mac",
        host=MAC,
        system="Darwin",
        release=ARTIFACT,
        observed_at=COMMITTED_AT + 60.0,
    )
    journal.host(
        "pi", host=PI, system="Linux", release=ARTIFACT, observed_at=COMMITTED_AT + 60.0
    )
    for index, name in enumerate(staging.STAGING_CHECKS):
        journal.record(
            name,
            host="both",
            observed_at=COMMITTED_AT + 120.0 + index,
            **overrides.get(name, {}),
        )
    journal.soak(
        started_at=SOAK_STARTED_AT,
        ended_at=SOAK_ENDED_AT,
        phases={"active": 1800.0, "idle": 1200.0, "reconnect": 600.0},
    )
    return journal


class TemporaryCase(unittest.TestCase):
    """A case with a directory of its own to write journals and units into."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)


class StagedContract(unittest.TestCase):
    """What the harness copies from the gate, pinned to the gate."""

    def test_the_checks_the_harness_records_are_the_checks_the_gate_requires(
        self,
    ) -> None:
        self.assertEqual(
            staging.STAGING_CHECKS, acceptance.STAGE_CHECKS[acceptance.STAGE_STAGING]
        )

    def test_the_shared_constants_are_the_gate_s_constants(self) -> None:
        for name in (
            "SCHEMA_VERSION",
            "SOAK_MINIMUM_SECONDS",
            "SOAK_PHASES",
            "HOST_ROLES",
            "OUTCOME_PASSED",
            "PROVENANCE_ACTUAL_HOST",
            "MAX_CLOCK_SKEW_SECONDS",
        ):
            with self.subTest(constant=name):
                self.assertEqual(
                    getattr(staging, name), getattr(acceptance, name), name
                )
        self.assertEqual(staging.STAGE, acceptance.STAGE_STAGING)

    def test_the_harness_names_no_machine_the_gate_reads_as_a_fixture(self) -> None:
        # The staging names end up in the report's checks and in the resources
        # the rehearsal installs; a name the gate reads as synthetic would fail
        # acceptance after the rehearsal rather than before it.
        names = (
            staging.STAGING_LABEL,
            staging.STAGING_UNIT,
            *staging.STAGING_PREFIX_NAMES.values(),
        )
        for name in names:
            for marker in acceptance.FORBIDDEN_HOST_MARKERS:
                with self.subTest(name=name, marker=marker):
                    self.assertNotIn(marker, name.lower())


class Isolation(unittest.TestCase):
    """The rehearsal's site shares nothing production is recognised by."""

    def test_the_staging_site_is_accepted(self) -> None:
        staging.check_isolated(staging.staging_site())

    def test_the_production_site_is_refused_outright(self) -> None:
        with self.assertRaises(staging.StagingError):
            staging.check_isolated(PI_SITE)

    def test_reusing_any_owned_identifier_is_refused(self) -> None:
        for field in staging.OWNED_SITE_FIELDS:
            shared = replace(staging.staging_site(), **{field: getattr(PI_SITE, field)})
            with self.subTest(field=field), self.assertRaises(staging.StagingError):
                staging.check_isolated(shared)

    def test_a_rehearsal_that_could_publish_is_refused(self) -> None:
        holding = replace(staging.staging_site(), notifications=PI_NOTIFICATION_SITE)
        with self.assertRaises(staging.StagingError):
            staging.check_isolated(holding)

    def test_a_rehearsal_that_calls_itself_production_is_refused(self) -> None:
        mislabelled = replace(staging.staging_site(), deployment=DEPLOYMENT_PRODUCTION)
        with self.assertRaises(staging.StagingError):
            staging.check_isolated(mislabelled)

    def test_a_rehearsal_that_could_adopt_an_older_name_is_refused(self) -> None:
        for field in ("legacy_monitor_names", "legacy_group_headings"):
            adopting = replace(
                staging.staging_site(), **{field: getattr(PI_SITE, field)}
            )
            with self.subTest(field=field), self.assertRaises(staging.StagingError):
                staging.check_isolated(adopting)

    def test_the_application_itself_refuses_to_publish_for_this_site(self) -> None:
        # Not the harness's own rule: the site carries no notification site, so
        # the release's publisher selection is what refuses, which is the
        # refusal that still holds if the harness is bypassed entirely.
        site = staging.staging_site()
        self.assertIsNone(
            select_publisher(mode=MODE_REMOTE, deployment=site.deployment, token=None)
        )
        with self.assertRaises(NotificationError):
            select_publisher(
                mode=MODE_REMOTE,
                deployment=site.deployment,
                token="secret",
                site=PI_NOTIFICATION_SITE,
            )


class Prefixes(unittest.TestCase):
    """No rehearsal prefix reaches the installation that is already there."""

    def test_the_rehearsal_s_own_prefixes_are_accepted(self) -> None:
        for name in staging.STAGING_PREFIX_NAMES.values():
            with self.subTest(prefix=name):
                staging.check_prefix_isolated(Path.home() / ".local/share" / name)

    def test_the_production_prefix_is_refused(self) -> None:
        with self.assertRaises(staging.StagingError):
            staging.check_prefix_isolated(
                Path.home() / ".local/share" / staging.PRODUCTION_PREFIX_NAME
            )

    def test_a_prefix_inside_the_production_one_is_refused(self) -> None:
        with self.assertRaises(staging.StagingError):
            staging.check_prefix_isolated(
                Path.home()
                / ".local/share"
                / staging.PRODUCTION_PREFIX_NAME
                / "asl-workbench-staging"
            )

    def test_an_unrelated_prefix_is_refused(self) -> None:
        for candidate in (Path(""), Path("/tmp/somewhere-else")):
            with (
                self.subTest(prefix=candidate),
                self.assertRaises(staging.StagingError),
            ):
                staging.check_prefix_isolated(candidate)


class RestagedUnit(TemporaryCase):
    """The unit the release writes, starting the release on another port."""

    def unit(self, prefix: Path | None = None) -> bytes:
        name, content = lifecycle.definition(
            prefix or self.root / "prefix",
            self.root / "release",
            "/usr/bin/python3",
            lifecycle.ROLE_SERVICE,
        )
        self.assertEqual(name, lifecycle.UNIT)
        return content

    def test_quoting_round_trips_every_character_the_release_escapes(self) -> None:
        words = ["/usr/bin/python3", 'a"b', "c\\d", "50%", "$HOME", "line\nbreak"]
        line = " ".join(lifecycle.quote_unit(word) for word in words)
        self.assertEqual(staging.unquote_unit(line), words)

    def test_only_the_exec_start_moves(self) -> None:
        shipped = self.unit().decode("utf-8").splitlines()
        staged = (
            staging.restage_unit(self.unit(), site=staging.staging_site())
            .decode("utf-8")
            .splitlines()
        )
        self.assertEqual(len(shipped), len(staged))
        for before, after in zip(shipped, staged, strict=True):
            if before.startswith("ExecStart="):
                self.assertNotEqual(before, after)
            else:
                self.assertEqual(before, after)

    def test_the_staged_command_starts_the_release_against_the_staging_site(
        self,
    ) -> None:
        site = staging.staging_site()
        prefix = self.root / "prefix"
        staged = staging.restage_unit(self.unit(prefix), site=site).decode("utf-8")
        line = next(
            entry for entry in staged.splitlines() if entry.startswith("ExecStart=")
        )
        words = staging.unquote_unit(line.removeprefix("ExecStart="))
        self.assertEqual(words[:5], ["/usr/bin/python3", "-E", "-s", "-B", "-c"])
        self.assertEqual(words[6:], [str(prefix)])
        self.assertEqual(words[5], staging.staging_serve_program(site))

    def test_the_staged_command_binds_the_site_the_harness_checked(self) -> None:
        site = staging.staging_site()
        prefix = self.root / "prefix"
        program = staging.staging_serve_program(site)
        with (
            mock.patch.object(runtime, "run") as run,
            mock.patch.object(sys, "argv", ["staging", str(prefix)]),
        ):
            exec(compile(program, "<staging unit>", "exec"), {})  # noqa: S102
        self.assertEqual(run.call_args.args, (prefix,))
        started = run.call_args.kwargs["site"]
        self.assertEqual(started, site)
        staging.check_isolated(started)

    def test_a_unit_the_release_did_not_write_is_refused(self) -> None:
        shipped = self.unit().decode("utf-8")
        foreign = shipped.replace("asl_workbench.runtime", "companion.runtime").encode()
        doubled = (shipped + shipped).encode()
        for content in (foreign, doubled, b"[Service]\n"):
            with (
                self.subTest(content=content[:32]),
                self.assertRaises(staging.StagingError),
            ):
                staging.restage_unit(content, site=staging.staging_site())

    def test_the_staged_unit_names_a_prefix_this_rehearsal_owns(self) -> None:
        # The systemd half of check_owner matches on the prefix at the end of
        # ExecStart, so the staged line has to keep it there.
        prefix = self.root / "asl-workbench-staging"
        staged = staging.restage_unit(
            self.unit(prefix), site=staging.staging_site()
        ).decode("utf-8")
        self.assertTrue(
            any(
                line.startswith("ExecStart=")
                and line.endswith(" " + lifecycle.quote_unit(prefix))
                for line in staged.splitlines()
            )
        )


class RestagedLaunchAgent(TemporaryCase):
    """The launch agent the release writes, under a label launchd has free."""

    def plist(self) -> bytes:
        prefix = self.root / "asl-workbench-staging"
        settings = {
            "asl": str(self.root / "asl"),
            "asl_state": str(self.root / "state"),
            "target_id": "pi",
            "connector_id": "mac",
            "plan_roots": [str(self.root / "plans")],
            "settings": {"public_origin": staging.staging_site().public_origin},
        }
        with mock.patch.object(
            lifecycle_attachment, "connector_settings", return_value=settings
        ):
            name, content = lifecycle.definition(
                prefix, prefix, "/usr/bin/python3", lifecycle.ROLE_CONNECTOR
            )
        self.assertEqual(name, lifecycle.LABEL + ".plist")
        return content

    def test_only_the_label_moves(self) -> None:
        shipped = plistlib.loads(self.plist())
        staged = plistlib.loads(staging.restage_plist(self.plist()))
        self.assertEqual(staged.pop("Label"), staging.STAGING_LABEL)
        self.assertNotEqual(staging.STAGING_LABEL, lifecycle.LABEL)
        shipped.pop("Label")
        self.assertEqual(staged, shipped)

    def test_a_launch_agent_the_release_did_not_write_is_refused(self) -> None:
        foreign = plistlib.dumps({"Label": "com.example.other"})
        with self.assertRaises(staging.StagingError):
            staging.restage_plist(foreign)

    def test_restaging_onto_the_production_label_is_refused(self) -> None:
        with self.assertRaises(staging.StagingError):
            staging.restage_plist(self.plist(), label=lifecycle.LABEL)

    def fixtures(self) -> Path:
        """The rehearsal's own state, with everything the environment names."""
        root = self.root / staging.STAGING_FIXTURE_NAME
        (root / "tls").mkdir(parents=True, exist_ok=True)
        (root / "tls" / "bundle.pem").write_bytes(b"-- never read here --\n")
        (root / "xdg").mkdir(exist_ok=True)
        (root / "tmp").mkdir(exist_ok=True)
        return root

    def test_the_environment_is_extended_and_nothing_else_moves(self) -> None:
        """Extended rather than replaced, and the rest of the file is the release's.

        The connector is started with no inherited environment at all, so
        anything dropped here is a variable the release decided it needed and
        the rehearsal quietly took away.
        """
        added = staging.staging_environment(self.fixtures())
        shipped = plistlib.loads(self.plist())
        staged = plistlib.loads(staging.restage_plist(self.plist(), environment=added))

        self.assertEqual(
            staged["EnvironmentVariables"],
            {**shipped["EnvironmentVariables"], **added},
        )
        staged.pop("EnvironmentVariables")
        shipped.pop("EnvironmentVariables")
        self.assertEqual(staged.pop("Label"), staging.STAGING_LABEL)
        shipped.pop("Label")
        self.assertEqual(staged, shipped)

    def test_the_rehearsal_names_a_trust_store_and_both_default_plan_roots(
        self,
    ) -> None:
        """Whichever of the three is missing is a resource production owns.

        The plan roots are the sharper one: the connector scans its named roots
        *and* the defaults, and the defaults on this Mac are where the
        operator's real plans live -- so a rehearsal that left them alone would
        publish them to a staging store and have no fixture corpus at all.
        """
        added = staging.staging_environment(self.fixtures())

        self.assertEqual(
            set(added), {staging.TRUST_VARIABLE, "XDG_DATA_HOME", "TMPDIR"}
        )
        for name, value in added.items():
            with self.subTest(variable=name):
                self.assertIn(staging.STAGING_FIXTURE_NAME, Path(value).parts)
                self.assertTrue(Path(value).exists(), value)

    def test_a_variable_the_release_already_sets_is_not_overwritten(self) -> None:
        """At that point the rehearsal would be changing the release's mind."""
        parsed = plistlib.loads(self.plist())
        parsed["EnvironmentVariables"] = {
            **parsed["EnvironmentVariables"],
            staging.TRUST_VARIABLE: "/etc/ssl/cert.pem",
        }
        with self.assertRaises(staging.StagingError):
            staging.restage_plist(
                plistlib.dumps(parsed),
                environment=staging.staging_environment(self.fixtures()),
            )

    def test_a_value_outside_the_rehearsals_own_state_is_refused(self) -> None:
        """A path somewhere else is one the cleanup step would leave behind."""
        root = self.fixtures()
        stray = self.root / "cert.pem"
        stray.write_bytes(b"-- stray --\n")
        for value in (str(stray), "relative.pem", str(root / "absent")):
            with (
                self.subTest(value=value),
                self.assertRaises(staging.StagingError),
            ):
                staging.restage_plist(
                    self.plist(), environment={staging.TRUST_VARIABLE: value}
                )


class StagingManager(TemporaryCase):
    """The release's manager, pointed at names production is not using."""

    def manager(
        self, role: str, prefix: Path | None = None
    ) -> staging.StagingNativeManager:
        with mock.patch.object(Path, "home", return_value=self.root):
            return staging.StagingNativeManager(
                role, prefix=prefix or self.root / "asl-workbench-staging"
            )

    def test_neither_role_touches_a_production_definition(self) -> None:
        for role in lifecycle.DEPLOYMENT_ROLES:
            manager = self.manager(role)
            with self.subTest(role=role):
                self.assertNotIn(lifecycle.LABEL, str(manager.destination))
                self.assertNotIn(lifecycle.UNIT, str(manager.destination))
                self.assertNotIn(lifecycle.LABEL, manager.name)
                self.assertNotIn(lifecycle.UNIT, manager.name)
                self.assertIn(
                    manager.name, (staging.STAGING_LABEL, staging.STAGING_UNIT)
                )

    def test_each_rehearsal_prefix_gets_native_names_of_its_own(self) -> None:
        """Three prefixes run side by side; one unit name cannot serve them all."""
        prefixes = sorted(set(staging.STAGING_PREFIX_NAMES.values()))
        names = set()
        for name in prefixes:
            prefix = self.root / name
            for role in lifecycle.DEPLOYMENT_ROLES:
                manager = self.manager(role, prefix)
                names.add((manager.name, str(manager.destination)))
                self.assertIn(name, manager.name)
        self.assertEqual(len(names), len(prefixes) * len(lifecycle.DEPLOYMENT_ROLES))

    def test_an_absent_definition_is_this_rehearsal_s_to_write(self) -> None:
        for role in lifecycle.DEPLOYMENT_ROLES:
            with self.subTest(role=role):
                self.manager(role).check_owner(self.root / "asl-workbench-staging")

    def test_a_definition_from_another_rehearsal_is_refused(self) -> None:
        prefix = self.root / "asl-workbench-staging"
        other = self.root / "asl-workbench-staging-rollback"
        service = self.manager(lifecycle.ROLE_SERVICE, prefix)
        service.destination.parent.mkdir(parents=True, exist_ok=True)
        _, shipped = lifecycle.definition(
            other, other, "/usr/bin/python3", lifecycle.ROLE_SERVICE
        )
        service.destination.write_bytes(
            staging.restage_unit(shipped, site=staging.staging_site())
        )
        with self.assertRaises(staging.StagingError):
            service.check_owner(prefix)

    def test_a_manager_is_refused_a_prefix_that_is_not_the_one_it_stages(self) -> None:
        service = self.manager(lifecycle.ROLE_SERVICE)
        with self.assertRaises(staging.StagingError):
            service.check_owner(self.root / "asl-workbench-staging-rollback")

    def test_a_connector_definition_is_owned_by_the_prefix_it_reads(self) -> None:
        prefix = self.root / "asl-workbench-staging"
        connector = self.manager(lifecycle.ROLE_CONNECTOR, prefix)
        connector.destination.parent.mkdir(parents=True, exist_ok=True)
        connector.destination.write_bytes(
            plistlib.dumps(
                {
                    "Label": connector.label,
                    "ProgramArguments": [
                        "/usr/bin/python3",
                        str(
                            self.root
                            / "asl-workbench-staging-migration"
                            / lifecycle.CONNECTOR_TOKEN_PATH
                        ),
                    ],
                }
            )
        )
        with self.assertRaises(staging.StagingError):
            connector.check_owner(prefix)

    def test_the_manager_the_installation_is_given_is_the_staging_one(self) -> None:
        prefix = self.root / "asl-workbench-staging"
        with mock.patch.object(Path, "home", return_value=self.root):
            factory = staging.staging_manager_factory(prefix)
            manager = factory(lifecycle.ROLE_SERVICE)
        self.assertIsInstance(manager, staging.StagingNativeManager)
        self.assertEqual(manager.name, staging.STAGING_UNIT)


class JournalRefusals(TemporaryCase):
    """A report is what was observed, or it is not a report."""

    def journal(self, **overrides: Any) -> staging.Journal:
        return complete_journal(self.root / "journal.json", **overrides)

    def render(self, journal: staging.Journal) -> dict[str, Any]:
        return journal.render(committed_at=COMMITTED_AT, recorded_at=RECORDED_AT)

    def test_a_complete_rehearsal_renders(self) -> None:
        document = self.render(self.journal())
        self.assertEqual(document["stage"], acceptance.STAGE_STAGING)
        self.assertEqual(document["release"]["artifact"], ARTIFACT)

    def test_the_rendered_report_is_one_the_gate_accepts(self) -> None:
        # The gate's own structural rules, run against the harness's output.
        # Everything the gate checks against git -- the revision, the rebuilt
        # digest, the staleness rule -- needs a real repository and belongs to
        # tests/test_workbench_acceptance.py; everything else is proved here,
        # so a drift between the two modules fails before a rehearsal, not
        # after one.
        document = self.render(self.journal())
        acceptance.check_sanitized(document, acceptance.STAGE_STAGING)
        acceptance.require_keys(
            document,
            ("schema", "stage", "recorded_at", "release", "hosts", "checks", "soak"),
            acceptance.STAGE_STAGING,
        )
        for role in acceptance.HOST_ROLES:
            acceptance.check_provenance(
                document["hosts"][role], role, f"staging.hosts.{role}"
            )
        acceptance.check_checks(
            document, acceptance.STAGE_STAGING, COMMITTED_AT, RECORDED_AT
        )
        acceptance.check_soak(document, COMMITTED_AT, RECORDED_AT)

    def test_the_journal_survives_the_process_that_wrote_it(self) -> None:
        # The soak is an hour long and the rehearsal spans two hosts; a journal
        # that only existed in memory would lose its first half to any
        # interruption.
        self.journal()
        reopened = staging.Journal(self.root / "journal.json")
        self.assertEqual(reopened.missing(), [])
        self.render(reopened)

    def test_an_unrecorded_check_is_named_rather_than_assumed(self) -> None:
        journal = self.journal()
        del journal.data["checks"]["rollback"]
        journal.save()
        self.assertEqual(journal.missing(), ["rollback"])
        with self.assertRaises(staging.StagingError) as caught:
            self.render(journal)
        self.assertIn("rollback", str(caught.exception))

    def test_a_check_that_did_not_pass_cannot_be_rendered(self) -> None:
        journal = self.journal(recovery={"outcome": "failed"})
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_check_the_stage_does_not_owe_is_refused_at_the_point_of_writing(
        self,
    ) -> None:
        journal = staging.Journal(self.root / "journal.json")
        with self.assertRaises(staging.StagingError):
            journal.record("everything-worked", host="both")
        with self.assertRaises(staging.StagingError):
            journal.record("rollback", host="laptop")
        with self.assertRaises(staging.StagingError):
            journal.host("router", host="r", system="Linux", release=ARTIFACT)

    def test_a_rehearsal_without_a_release_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["release"] = None
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_host_that_ran_another_release_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.host(
            "pi",
            host=PI,
            system="Linux",
            release="c" * 64,
            observed_at=COMMITTED_AT + 60.0,
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_host_of_the_wrong_kind_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.host(
            "pi",
            host=PI,
            system="Darwin",
            release=ARTIFACT,
            observed_at=COMMITTED_AT + 60.0,
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_one_machine_cannot_be_both_halves(self) -> None:
        journal = self.journal()
        journal.host(
            "pi",
            host=MAC,
            system="Linux",
            release=ARTIFACT,
            observed_at=COMMITTED_AT + 60.0,
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_missing_host_cannot_be_rendered(self) -> None:
        journal = self.journal()
        del journal.data["hosts"]["mac"]
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_an_observation_outside_the_window_cannot_be_rendered(self) -> None:
        for observed_at in (COMMITTED_AT - 1.0, RECORDED_AT + 3600.0):
            journal = self.journal()
            journal.record("rollback", host="pi", observed_at=observed_at)
            with (
                self.subTest(observed_at=observed_at),
                self.assertRaises(staging.StagingError),
            ):
                self.render(journal)

    def test_an_unparseable_moment_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["checks"]["rollback"]["observed_at"] = "yesterday"
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_short_soak_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.soak(
            started_at=SOAK_STARTED_AT,
            ended_at=SOAK_ENDED_AT - 1.0,
            phases={"active": 1800.0, "idle": 1200.0, "reconnect": 599.0},
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_phase_that_never_happened_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.soak(
            started_at=SOAK_STARTED_AT,
            ended_at=SOAK_ENDED_AT,
            phases={"active": 3600.0, "idle": 0.0, "reconnect": 0.0},
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_phases_that_outlast_the_soak_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.soak(
            started_at=SOAK_STARTED_AT,
            ended_at=SOAK_ENDED_AT,
            phases={"active": 3600.0, "idle": 3600.0, "reconnect": 3600.0},
        )
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_a_soak_missing_a_phase_is_refused_at_the_point_of_writing(self) -> None:
        journal = self.journal()
        with self.assertRaises(staging.StagingError):
            journal.soak(
                started_at=SOAK_STARTED_AT,
                ended_at=SOAK_ENDED_AT,
                phases={"active": 1800.0, "idle": 1800.0},
            )
        with self.assertRaises(staging.StagingError):
            journal.soak(
                started_at=SOAK_STARTED_AT,
                ended_at=SOAK_ENDED_AT,
                phases={
                    "active": 1800.0,
                    "idle": 1200.0,
                    "reconnect": 600.0,
                    "extra": 1.0,
                },
            )

    def test_a_rehearsal_without_a_soak_cannot_be_rendered(self) -> None:
        journal = self.journal()
        journal.data["soak"] = None
        journal.save()
        with self.assertRaises(staging.StagingError):
            self.render(journal)

    def test_the_report_is_dated_by_the_clock_rather_than_by_its_author(self) -> None:
        journal = self.journal()
        document = journal.render(committed_at=COMMITTED_AT)
        self.assertEqual(document["recorded_at"], RECORDED_AT)
        self.assertIs(staging.Journal(self.root / "fresh.json").clock, time.time)


class StagingCommands(TemporaryCase):
    """The commands the Pi half is driven with refuse what the harness does."""

    def test_the_site_command_reports_the_staging_route_and_names(self) -> None:
        with mock.patch("sys.stdout") as out:
            self.assertEqual(staging.main(["site"]), 0)
        printed = json.loads("".join(call.args[0] for call in out.write.call_args_list))
        self.assertEqual(printed["label"], staging.STAGING_LABEL)
        self.assertEqual(printed["unit"], staging.STAGING_UNIT)
        self.assertEqual(printed["port"], staging.STAGING_PORT)
        self.assertEqual(printed["deployment"], "staging")
        self.assertIn(f"--https={staging.STAGING_PORT}", printed["serve"])
        self.assertNotIn(PI_SITE.port, printed["serve"])

    def test_no_command_will_act_on_the_production_prefix(self) -> None:
        production = self.root / staging.PRODUCTION_PREFIX_NAME
        for action in ("status", "remove", "rollback", "recover", "migrate"):
            with self.subTest(action=action), self.assertRaises(staging.StagingError):
                staging.main([action, "--prefix", str(production)])

    def test_an_install_without_an_interpreter_is_refused(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            staging.main(
                ["install", "--prefix", str(self.root / "asl-workbench-staging")]
            )


if __name__ == "__main__":  # pragma: no cover - the discovered suite runs these
    unittest.main()
