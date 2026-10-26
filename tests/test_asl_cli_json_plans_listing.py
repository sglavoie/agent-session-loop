"""JSON/text CLI tests for status, plans listing and stale filters."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import config, harnesses, plans_files
from tests.support import (
    REPO_ROOT,
    _write_state_payload,
    patched_env,
    write_plan,
)
from tests.support_asl_cli_json import JsonCliHelpers


class JsonPlansListingCliTests(JsonCliHelpers, unittest.TestCase):
    """JSON/text CLI tests for status, plans listing and stale filters."""

    def test_status_and_dry_run_json_outputs_include_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            status_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            status_payload = json.loads(status_completed.stdout)
            self.assertTrue(status_payload["ok"])
            self.assertEqual("status", status_payload["command"])
            self.assertEqual(1, status_payload["next_pending"])
            self.assertEqual("1", status_payload["next_pending_id"])
            self.assertEqual("1", status_payload["sessions"][0]["id"])
            self.assertEqual(1, status_payload["sessions"][0]["number"])
            self.assertTrue(status_payload["state_path"].endswith("/state.json"))
            self.assertTrue(status_payload["events_path"].endswith("/events.jsonl"))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            run_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--harness-model",
                    "gpt-5",
                    "--effort",
                    "high",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            run_payload = json.loads(run_completed.stdout)
            self.assertTrue(run_payload["ok"])
            self.assertEqual("run", run_payload["command"])
            self.assertEqual({}, run_payload["details"])
            self.assertEqual("dry-run", run_payload["reason"])
            self.assertIn("harness_config", run_payload)
            self.assertEqual("gpt-5", run_payload["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, run_payload["model_source"])
            self.assertEqual("high", run_payload["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, run_payload["effort_source"])
            self.assertEqual(1, run_payload["planned"]["next_pending"])
            self.assertEqual("1", run_payload["planned"]["next_pending_id"])
            self.assertEqual("1", run_payload["initial_snapshot"]["next_pending_id"])
            planned = run_payload["planned"]
            invocation = planned["invocation"]
            self.assertEqual("gpt-5", planned["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, planned["model_source"])
            self.assertEqual("high", planned["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, planned["effort_source"])
            self.assertEqual("gpt-5", invocation["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, invocation["model_source"])
            self.assertEqual("high", invocation["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, invocation["effort_source"])
            # Codex plans its turn over the app-server, so the access profile
            # and the model selection are read off the turn plan, not argv.
            self.assertEqual(harnesses.TRANSPORT_APP_SERVER, invocation["transport"])
            thread_params = invocation["turn_plan"]["thread_params"]
            self.assertEqual("danger-full-access", thread_params["sandbox"])
            self.assertNotIn("config", thread_params)
            self.assertEqual("gpt-5", thread_params["model"])
            self.assertEqual("high", invocation["turn_plan"]["turn_params"]["effort"])
            self.assertTrue(run_payload["state_path"].endswith("/state.json"))
            self.assertTrue(run_payload["events_path"].endswith("/events.jsonl"))

    def test_plans_cli_text_and_json_shapes_with_readiness_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            data_home = tmp_dir / "data"
            share_root = data_home / "agent-plans"
            share_root.mkdir(parents=True)
            (tmp_dir / "config").mkdir()
            (tmp_dir / "tmp").mkdir()
            (tmp_dir / "home").mkdir()

            reviewed = share_root / "reviewed-plan"
            draft = share_root / "draft-plan"
            write_plan(reviewed, ("pending",))
            write_plan(draft, ("pending",))
            self._write_readiness(
                reviewed,
                state_value="reviewed",
                checked_at="2026-07-01T00:00:00+00:00",
            )
            self._write_readiness(
                draft,
                state_value="draft",
                checked_at="2026-07-02T00:00:00+00:00",
            )

            with patched_env(
                HOME=str(tmp_dir / "home"),
                XDG_CONFIG_HOME=str(tmp_dir / "config"),
                XDG_DATA_HOME=str(data_home),
                TMPDIR=str(tmp_dir / "tmp"),
            ):
                text_code, text_stdout, text_stderr = self._run_plans(
                    "--readiness",
                    "reviewed",
                )
                json_code, json_stdout, json_stderr = self._run_plans(
                    "--readiness",
                    "reviewed",
                    "--json",
                )

            self.assertEqual(0, text_code, text_stderr)
            self.assertEqual("", text_stderr)
            self.assertIn(f"- {plans_files.canonicalize_path(reviewed)}", text_stdout)
            self.assertIn("  state: ready", text_stdout)
            self.assertIn("  run state: absent", text_stdout)
            self.assertIn("  readiness: reviewed", text_stdout)
            self.assertNotIn(str(plans_files.canonicalize_path(draft)), text_stdout)

            self.assertEqual(0, json_code, json_stderr)
            self.assertEqual("", json_stderr)
            payload = json.loads(json_stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("plans", payload["command"])
            self.assertEqual(
                {
                    "never_run": False,
                    "readiness": "reviewed",
                    "stale_days": None,
                },
                payload["filters"],
            )
            self.assertEqual(1, payload["plan_count"])
            self.assertEqual(
                str(plans_files.canonicalize_path(reviewed)),
                payload["plans"][0]["plan_dir"],
            )
            self.assertEqual("ready", payload["plans"][0]["primary_state"])
            self.assertEqual("reviewed", payload["plans"][0]["readiness"]["state"])

    def test_plans_cli_stale_and_never_run_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            data_home = tmp_dir / "data"
            share_root = data_home / "agent-plans"
            share_root.mkdir(parents=True)
            state_root = tmp_dir / "state"
            (tmp_dir / "config").mkdir()
            (tmp_dir / "tmp").mkdir()
            (tmp_dir / "home").mkdir()

            abandoned = share_root / "abandoned-plan"
            recent = share_root / "recent-plan"
            write_plan(abandoned, ("pending",))
            write_plan(recent, ("pending",))
            # Both timestamps are relative to the moment the test runs. Fixed
            # calendar dates encode an expiry: a "recent" plan dated far enough
            # in the past eventually drifts past the --stale-days cutoff and the
            # test starts failing on a day nobody touched the code. Anchoring to
            # now keeps the fixture's meaning -- long abandoned versus just
            # finished -- true on every future run. Cutoff arithmetic itself is
            # pinned deterministically in the injected-clock tests below.
            now = datetime.now(UTC)
            self._write_readiness(
                abandoned,
                state_value="reviewed",
                checked_at=(now - timedelta(days=400)).isoformat(),
            )
            _write_state_payload(
                plans_files.canonicalize_path(recent),
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260810T120000Z-9",
                    "run_started_at": (now - timedelta(hours=2)).isoformat(),
                    "run_finished_at": (now - timedelta(hours=1)).isoformat(),
                    "active_turn": None,
                    "active_verification": None,
                },
            )

            with patched_env(
                HOME=str(tmp_dir / "home"),
                XDG_CONFIG_HOME=str(tmp_dir / "config"),
                XDG_DATA_HOME=str(data_home),
                TMPDIR=str(tmp_dir / "tmp"),
            ):
                stale_code, stale_stdout, stale_stderr = self._run_plans(
                    "--state-dir",
                    str(state_root),
                    "--stale-days",
                    "14",
                    "--json",
                )
                never_run_code, never_run_stdout, never_run_stderr = self._run_plans(
                    "--state-dir",
                    str(state_root),
                    "--never-run",
                    "--json",
                )

            self.assertEqual(0, stale_code, stale_stderr)
            self.assertEqual("", stale_stderr)
            stale_payload = json.loads(stale_stdout)
            self.assertEqual(1, stale_payload["plan_count"])
            self.assertEqual(
                [str(plans_files.canonicalize_path(abandoned))],
                [entry["plan_dir"] for entry in stale_payload["plans"]],
            )

            self.assertEqual(0, never_run_code, never_run_stderr)
            self.assertEqual("", never_run_stderr)
            never_run_payload = json.loads(never_run_stdout)
            self.assertEqual(1, never_run_payload["plan_count"])
            self.assertEqual(
                [str(plans_files.canonicalize_path(abandoned))],
                [entry["plan_dir"] for entry in never_run_payload["plans"]],
            )

    def test_plans_stale_filter_pins_calendar_edges_against_injected_clock(
        self,
    ) -> None:
        """Cutoff, timezone, leap-day, and year-boundary cases are exact.

        Every case names both instants outright, so each verdict is a fact
        about the arithmetic rather than about the day the suite happens to
        run. ``--stale-days`` keeps a plan when its activity is *at least*
        that many days old, so the exact cutoff is stale and anything younger
        is not.
        """
        cases: tuple[tuple[str, str, str, str, bool], ...] = (
            # (label, checked_at, now, stale_days, expected_stale)
            (
                "cutoff lands exactly on the threshold",
                "2028-03-01T00:00:00+00:00",
                "2028-03-15T00:00:00+00:00",
                "14",
                True,
            ),
            (
                "cutoff misses by a microsecond",
                "2028-03-01T00:00:00.000001+00:00",
                "2028-03-15T00:00:00+00:00",
                "14",
                False,
            ),
            (
                "fractional days are honoured",
                "2028-03-01T00:00:00+00:00",
                "2028-03-01T12:00:00+00:00",
                "0.5",
                True,
            ),
            (
                "zulu suffix is the same instant as +00:00",
                "2028-03-01T00:00:00Z",
                "2028-03-15T00:00:00+00:00",
                "14",
                True,
            ),
            (
                "a naive stamp is read as UTC",
                "2028-03-01T00:00:00",
                "2028-03-15T00:00:00+00:00",
                "14",
                True,
            ),
            (
                "an eastern offset resolves to the same instant",
                "2028-03-01T05:30:00+05:30",
                "2028-03-15T00:00:00+00:00",
                "14",
                True,
            ),
            (
                "a western offset resolves to the same instant",
                "2028-02-29T19:00:00-05:00",
                "2028-03-15T00:00:00+00:00",
                "14",
                True,
            ),
            (
                "a western offset keeps a same-looking date inside the cutoff",
                "2028-03-01T00:00:00-05:00",
                "2028-03-15T00:00:00+00:00",
                "14",
                False,
            ),
            (
                "activity on a leap day ages a full fortnight",
                "2028-02-29T00:00:00+00:00",
                "2028-03-14T00:00:00+00:00",
                "14",
                True,
            ),
            (
                "activity on a leap day is not yet stale a day earlier",
                "2028-02-29T00:00:00+00:00",
                "2028-03-13T00:00:00+00:00",
                "14",
                False,
            ),
            (
                "a leap February makes the span twenty-nine days",
                "2028-02-15T00:00:00+00:00",
                "2028-03-15T00:00:00+00:00",
                "29",
                True,
            ),
            (
                "the same span in a common year is only twenty-eight",
                "2027-02-15T00:00:00+00:00",
                "2027-03-15T00:00:00+00:00",
                "29",
                False,
            ),
            (
                "a span across new year is counted in days, not years",
                "2027-12-25T00:00:00+00:00",
                "2028-01-05T00:00:00+00:00",
                "11",
                True,
            ),
            (
                "a span across new year stops one day short",
                "2027-12-25T00:00:00+00:00",
                "2028-01-05T00:00:00+00:00",
                "12",
                False,
            ),
        )

        for label, checked_at, now, stale_days, expected in cases:
            with self.subTest(label):
                self.assertEqual(
                    expected,
                    self._stale_days_verdict(
                        checked_at=checked_at,
                        now=datetime.fromisoformat(now),
                        stale_days=stale_days,
                    ),
                )

    def test_plans_stale_days_refuses_non_finite_values_in_json(self) -> None:
        """NaN slipped past ``< 0`` and infinity overflowed ``timedelta``."""
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("nan", "inf", "-inf", "-1"):
                with self.subTest(value=value):
                    code, stdout, stderr = self._run_plans(
                        "--state-dir", tmp, f"--stale-days={value}", "--json"
                    )
                    self.assertEqual(1, code, stderr)
                    payload = json.loads(stdout)
                    self.assertEqual("invalid-argument", payload["reason"])
                    self.assertIn("finite, non-negative number", payload["error"])

    def test_plans_stale_days_beyond_the_calendar_keeps_nothing(self) -> None:
        self.assertFalse(
            self._stale_days_verdict(
                checked_at="2020-01-01T00:00:00+00:00",
                now=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
                stale_days="1e12",
            )
        )

    def test_plans_stale_filter_defaults_to_the_wall_clock(self) -> None:
        """Omitting the injected clock still filters against the real now."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            data_home = tmp_dir / "data"
            share_root = data_home / "agent-plans"
            share_root.mkdir(parents=True)
            for name in ("config", "tmp", "home"):
                (tmp_dir / name).mkdir()

            stale = share_root / "stale-plan"
            fresh = share_root / "fresh-plan"
            write_plan(stale, ("pending",))
            write_plan(fresh, ("pending",))
            now = datetime.now(UTC)
            self._write_readiness(
                stale,
                state_value="reviewed",
                checked_at=(now - timedelta(days=30)).isoformat(),
            )
            self._write_readiness(
                fresh,
                state_value="reviewed",
                checked_at=(now - timedelta(minutes=5)).isoformat(),
            )

            with patched_env(
                HOME=str(tmp_dir / "home"),
                XDG_CONFIG_HOME=str(tmp_dir / "config"),
                XDG_DATA_HOME=str(data_home),
                TMPDIR=str(tmp_dir / "tmp"),
            ):
                code, stdout, stderr = self._run_plans(
                    "--state-dir",
                    str(tmp_dir / "state"),
                    "--stale-days",
                    "14",
                    "--json",
                )

            self.assertEqual(0, code, stderr)
            payload = json.loads(stdout)
            self.assertEqual(
                [str(plans_files.canonicalize_path(stale))],
                [entry["plan_dir"] for entry in payload["plans"]],
            )


if __name__ == "__main__":
    unittest.main()
