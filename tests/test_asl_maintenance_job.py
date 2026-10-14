"""Launchd job, job status and command tests for storage maintenance.

Split from tests/test_asl_maintenance.py."""

from __future__ import annotations

import argparse
import io
import json
import os
import plistlib
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stdout
from typing import Any

from agent_session_loop import (
    maintenance_cli,
    maintenance_job,
    maintenance_policy,
    maintenance_run,
    retention,
)
from tests.asl_maintenance_support import (
    RUN_NEWEST,
    RUN_OLD,
    MaintenanceTestBase,
    _stamp,
)


class LaunchdJobTests(MaintenanceTestBase):
    """The scheduled job is ASL's own: bounded, absolute, and removable."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp_path / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.executable = self.tmp_path / "asl"
        self.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.calls: list[list[str]] = []

    def _launchctl(self, returncode: int = 0) -> Any:
        def fake(argv: list[str], **kwargs: Any) -> Any:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode, "", "")

        return fake

    def _install(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("executable", str(self.executable))
        kwargs.setdefault("home", self.home)
        kwargs.setdefault("state_dir", self.state_root)
        kwargs.setdefault("platform", "darwin")
        kwargs.setdefault("launchctl", self._launchctl())
        return maintenance_job.install_job(**kwargs)

    def test_the_rendered_job_is_daily_bounded_and_never_runs_at_load(self) -> None:
        document = maintenance_job.render_plist(
            str(self.executable), state_dir=self.state_root
        )
        payload = json.loads(json.dumps(plistlib.loads(document)))

        self.assertEqual(payload["Label"], maintenance_policy.JOB_LABEL)
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertFalse(payload["RunAtLoad"])
        argv = payload["ProgramArguments"]
        self.assertEqual(argv[0], str(self.executable))
        self.assertIn("--apply", argv)
        self.assertIn("--json", argv)
        self.assertIn("--max-runtime-seconds", argv)
        self.assertIn("--state-dir", argv)

    def test_installing_writes_the_definition_and_bootstraps_it(self) -> None:
        payload = self._install()

        target = maintenance_job.plist_path(self.home)
        self.assertTrue(target.is_file())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["bootstrapped"])
        self.assertEqual(payload["plist_path"], str(target))
        self.assertEqual([call[1] for call in self.calls], ["bootout", "bootstrap"])

    def test_installing_is_idempotent_and_byte_stable(self) -> None:
        self._install()
        first = maintenance_job.plist_path(self.home).read_bytes()
        self._install()

        self.assertEqual(maintenance_job.plist_path(self.home).read_bytes(), first)

    def test_no_bootstrap_writes_the_definition_without_touching_launchctl(
        self,
    ) -> None:
        payload = self._install(bootstrap=False)

        self.assertTrue(maintenance_job.plist_path(self.home).is_file())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["bootstrapped"])
        self.assertEqual(self.calls, [])

    def test_a_failed_bootstrap_is_reported_rather_than_swallowed(self) -> None:
        payload = self._install(launchctl=self._launchctl(returncode=1))

        self.assertFalse(payload["ok"])
        self.assertFalse(payload["bootstrapped"])

    def test_a_missing_executable_refuses_before_anything_is_written(self) -> None:
        with self.assertRaises(maintenance_policy.MaintenanceError):
            self._install(executable=str(self.tmp_path / "nope"))

        self.assertFalse(maintenance_job.plist_path(self.home).exists())

    def test_a_platform_without_launchd_refuses_with_an_alternative(self) -> None:
        with self.assertRaises(maintenance_policy.MaintenanceError) as caught:
            self._install(platform="linux")

        self.assertIn("maintenance run --apply", str(caught.exception))
        self.assertFalse(maintenance_job.plist_path(self.home).exists())

    def test_uninstalling_removes_the_job_and_keeps_every_artifact(self) -> None:
        self._install()
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._run(apply=True)
        manifest = maintenance_policy.manifest_path(self.state_root)
        self.assertTrue(manifest.is_file())
        self.calls.clear()

        payload = maintenance_job.uninstall_job(
            home=self.home,
            state_dir=self.state_root,
            platform="darwin",
            launchctl=self._launchctl(),
        )

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["plist_removed"])
        self.assertTrue(payload["booted_out"])
        self.assertFalse(maintenance_job.plist_path(self.home).exists())
        self.assertTrue(manifest.is_file())
        self.assertTrue(self._state_dir().is_dir())

    def test_uninstalling_a_job_that_is_not_installed_is_not_an_error(self) -> None:
        payload = maintenance_job.uninstall_job(
            home=self.home,
            state_dir=self.state_root,
            platform="darwin",
            launchctl=self._launchctl(),
        )

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["plist_removed"])


class JobStatusTests(LaunchdJobTests):
    """Install verification reports presence, last run, bytes, and failures."""

    def _status(self, **kwargs: Any) -> maintenance_job.JobStatus:
        kwargs.setdefault("home", self.home)
        kwargs.setdefault("state_dir", self.state_root)
        kwargs.setdefault("executable", str(self.executable))
        kwargs.setdefault("platform", "darwin")
        kwargs.setdefault("launchctl", self._launchctl())
        return maintenance_job.job_status(**kwargs)

    def test_status_reports_an_installed_current_and_loaded_job(self) -> None:
        self._install()

        status = self._status()

        self.assertTrue(status.plist_present)
        self.assertTrue(status.plist_current)
        self.assertTrue(status.loaded)
        self.assertEqual(status.label, maintenance_policy.JOB_LABEL)

    def test_status_reports_a_definition_that_no_longer_matches(self) -> None:
        self._install(hour=1, minute=0)

        status = self._status()

        self.assertTrue(status.plist_present)
        self.assertFalse(status.plist_current)

    def test_status_reports_an_absent_job_without_failing(self) -> None:
        status = self._status(probe=False)

        self.assertFalse(status.plist_present)
        self.assertFalse(status.loaded)
        self.assertTrue(status.ok)

    def test_status_reports_the_last_run_bytes_and_failures(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"s" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._run(apply=True)

        status = self._status(probe=False)

        self.assertEqual(status.last_run["removed_run_count"], 1)
        self.assertGreaterEqual(status.last_run["reclaimed_bytes"], 4096)
        self.assertEqual(status.last_run["failures"], [])
        self.assertTrue(status.ok)

    def test_status_fails_when_the_last_recorded_pass_failed(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        with unittest.mock.patch.object(
            retention, "remove_run_directory", return_value="I/O error"
        ):
            self._run(apply=True)

        status = self._status(probe=False)

        self.assertFalse(status.ok)
        self.assertEqual(len(status.last_run["failures"]), 1)

    def test_status_says_so_on_a_platform_that_cannot_schedule(self) -> None:
        status = self._status(platform="linux")

        self.assertFalse(status.supported)
        self.assertIn("no launchd", status.load_detail)


class CommandTests(MaintenanceTestBase):
    """The CLI surface exits on the same verdicts the library reports."""

    def _args(self, **fields: Any) -> argparse.Namespace:
        base: dict[str, Any] = {
            "state_dir": str(self.state_root),
            "json": True,
            "apply": False,
            "force": True,
            "max_age_days": retention.DEFAULT_MAX_AGE_DAYS,
            "max_runtime_seconds": maintenance_policy.DEFAULT_MAX_RUNTIME_SECONDS,
        }
        base.update(fields)
        return argparse.Namespace(**base)

    def _capture(self, handler: Any, args: argparse.Namespace) -> tuple[int, Any]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = handler(args)
        return code, json.loads(buffer.getvalue())

    def test_run_exits_zero_and_prints_json(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        code, payload = self._capture(maintenance_cli.run_from_args, self._args())

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "maintenance run")

    def test_run_exits_non_zero_on_a_strict_health_finding(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"h" * 8192}
        )
        self._write_lock(pid=os.getpid())

        with unittest.mock.patch.object(maintenance_cli, "CORPUS_BUDGET_BYTES", 1024):
            code, payload = self._capture(
                maintenance_cli.run_from_args, self._args(apply=True)
            )

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_run_renders_a_human_summary_without_json(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"t" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = maintenance_cli.run_from_args(self._args(json=False))

        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Reclaimable:", text)
        self.assertIn("--apply", text)

    def test_run_reports_an_error_as_json_rather_than_raising(self) -> None:
        with unittest.mock.patch.object(
            maintenance_run,
            "measure_corpus",
            side_effect=maintenance_policy.MaintenanceError("volume vanished"),
        ):
            code, payload = self._capture(maintenance_cli.run_from_args, self._args())

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("volume vanished", payload["error"])

    def test_status_renders_a_human_summary_without_json(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._run(apply=True)
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = maintenance_cli.status_from_args(
                self._args(json=False, executable=None, no_probe=True)
            )

        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(maintenance_policy.JOB_LABEL, text)
        self.assertIn("Failures: 0", text)

    def test_install_reports_a_refusal_as_json(self) -> None:
        code, payload = self._capture(
            maintenance_cli.install_from_args,
            self._args(
                executable=str(self.tmp_path / "absent"),
                hour=3,
                minute=30,
                no_bootstrap=True,
            ),
        )

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "maintenance install")


if __name__ == "__main__":
    unittest.main()
