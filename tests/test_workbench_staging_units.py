"""Prove the staged unit, launch agent and manager never touch production.

Split from ``tests/test_workbench_staging.py``; see it for the rationale.
"""

from __future__ import annotations

import plistlib
import sys
import unittest
from pathlib import Path
from unittest import mock

from asl_workbench import lifecycle, lifecycle_attachment, runtime
from tests import workbench_staging as staging
from tests.workbench_staging_support import (
    TemporaryCase,
)


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


if __name__ == "__main__":  # pragma: no cover - the discovered suite runs these
    unittest.main()
