"""Prove the staging harness refuses production names, prefixes and commands.

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
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

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
from tests.workbench_staging_support import (
    TemporaryCase,
)

# Integration: it builds the release's own native definitions and
# restages them, so it exercises the packaging code rather than a
# description of it.
ASL_TEST_LANE = "integration"


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

    def setUp(self) -> None:
        # Every prefix here is home-relative; judge them against a home this
        # test owns rather than the developer's.
        home = Path(tempfile.mkdtemp(prefix="asl-test-home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)

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
