"""Dry-run mailbox activation, compact-state and gc CLI tests."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    cli,
    mailbox,
    plans,
    retention,
    run_prompts,
    state,
)
from tests.support import (
    REPO_ROOT,
    write_plan,
)
from tests.support_asl_cli_json import JsonCliHelpers


class JsonStateMaintenanceCliTests(JsonCliHelpers, unittest.TestCase):
    """Dry-run mailbox activation, compact-state and gc CLI tests."""

    def test_dry_run_reports_mailbox_activation_and_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan with spaces"
            state_dir = tmp_dir / "state with spaces"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(
                plan_dir,
                "Prefer the smaller implementation.",
                state_dir=state_dir,
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            base_argv = [
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
            ]

            active_result = subprocess.run(
                base_argv,
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            disabled_result = subprocess.run(
                [*base_argv, "--no-mailbox"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            active = json.loads(active_result.stdout)
            disabled = json.loads(disabled_result.stdout)
            self.assertTrue(active["planned"]["mailbox_enabled"])
            self.assertEqual(
                "mailbox-present",
                active["planned"]["mailbox_activation"]["reason"],
            )
            self.assertFalse(active["run_config"]["no_mailbox"])
            self.assertFalse(active["planned"]["no_mailbox"])
            self.assertIn(
                run_prompts.MAILBOX_PROTOCOL_HEADING,
                active["planned"]["prompt"],
            )
            self.assertFalse(disabled["planned"]["mailbox_enabled"])
            self.assertEqual(
                "disabled-by-flag",
                disabled["planned"]["mailbox_activation"]["reason"],
            )
            self.assertTrue(disabled["run_config"]["no_mailbox"])
            self.assertTrue(disabled["planned"]["no_mailbox"])
            self.assertEqual(
                run_prompts.build_session_prompt("codex", plan_dir),
                disabled["planned"]["prompt"],
            )

    def test_compact_state_cli_dry_run_then_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True, exist_ok=True)
            big = "X" * 40000
            turn = {
                "session_id": "1",
                "turn_index": 1,
                "before": {"counts": {"pending": 1}, "sessions": [{"id": "1"}]},
                "verification": {
                    "checks": [
                        {"name": "c1", "status": "fail", "stdout": big, "stderr": ""}
                    ]
                },
            }
            state_path = plan_state / "state.json"
            state_path.write_text(
                json.dumps({"run_id": "r", "turns": [turn]}, sort_keys=True),
                encoding="utf-8",
            )
            before = state_path.read_bytes()

            def _run_compact(*extra: str) -> dict:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "compact-state",
                        str(plan_dir),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        *extra,
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                return json.loads(completed.stdout)

            dry = _run_compact()
            self.assertTrue(dry["ok"])
            self.assertEqual("compact-state", dry["command"])
            self.assertFalse(dry["applied"])
            self.assertGreater(dry["projected_bytes_reclaimed"], 0)
            self.assertEqual(0, dry["actual_bytes_reclaimed"])
            self.assertEqual(before, state_path.read_bytes())

            applied = _run_compact("--apply")
            self.assertTrue(applied["applied"])
            self.assertGreater(applied["actual_bytes_reclaimed"], 0)
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["version"])
            self.assertNotIn("stdout", payload["turns"][0]["verification"]["checks"][0])

    def test_gc_cli_dry_run_then_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            old = self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260401T000000000000Z-11",
                days_ago=90,
                log_bytes=4096,
            )
            newest = self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260803T000000000000Z-22",
                days_ago=1,
                log_bytes=16,
            )

            def _run_gc(*extra: str) -> dict:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "gc",
                        str(plan_dir),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        *extra,
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                return json.loads(completed.stdout)

            dry = _run_gc("--dry-run")
            self.assertTrue(dry["ok"])
            self.assertEqual("gc", dry["command"])
            self.assertEqual("plan", dry["scope"])
            self.assertFalse(dry["applied"])
            self.assertEqual(1, dry["reclaimable_run_count"])
            self.assertGreaterEqual(dry["reclaimable_bytes"], 4096)
            self.assertEqual(0, dry["removed_run_count"])
            self.assertEqual(0, dry["reclaimed_bytes"])
            self.assertEqual(
                1, dry["retained_by_reason"][retention.RETAIN_NEWEST_RUN]["run_count"]
            )
            self.assertEqual(
                dry["reclaimable_bytes"], dry["plans"][0]["reclaimable_bytes"]
            )
            self.assertTrue(old.is_dir())

            applied = _run_gc("--apply")
            self.assertTrue(applied["applied"])
            self.assertEqual(1, applied["removed_run_count"])
            self.assertEqual(dry["reclaimable_bytes"], applied["reclaimed_bytes"])
            self.assertEqual(0, applied["failed_removal_count"])
            self.assertFalse(old.exists())
            self.assertTrue(newest.is_dir())

    def test_gc_cli_corpus_scope_aggregates_and_refuses_locked_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            sweepable = tmp_dir / "sweepable"
            locked = tmp_dir / "locked"
            write_plan(sweepable, ("pending",))
            write_plan(locked, ("pending",))
            for plan_dir in (sweepable, locked):
                self._write_gc_run(
                    plan_dir=plan_dir,
                    state_dir=state_dir,
                    run_id="20260401T000000000000Z-11",
                    days_ago=90,
                    log_bytes=2048,
                )
                self._write_gc_run(
                    plan_dir=plan_dir,
                    state_dir=state_dir,
                    run_id="20260803T000000000000Z-22",
                    days_ago=1,
                    log_bytes=16,
                )
            locked_state = state.plan_state_dir(locked, state_dir)
            (locked_state / state.LOCK_FILE_NAME).write_text(
                json.dumps(
                    {
                        "plan_dir": str(plans.canonicalize_path(locked)),
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "pid": 4_242_424,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            args = cli.build_parser().parse_args(
                ["gc", "--state-dir", str(state_dir), "--json"]
            )
            handler = cli.resolve_handler(args)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = handler(args)

            self.assertIs(retention.gc_from_args, handler)
            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())
            self.assertEqual("corpus", payload["scope"])
            self.assertFalse(payload["applied"])
            self.assertEqual(2, payload["plan_count"])
            self.assertEqual(1, payload["reclaimable_run_count"])
            self.assertEqual(
                2,
                payload["retained_by_reason"][retention.RETAIN_PLAN_LOCKED][
                    "run_count"
                ],
            )
            by_plan = {
                entry["plan_dir"]: entry["reclaimable_bytes"]
                for entry in payload["plans"]
            }
            self.assertEqual(0, by_plan[str(plans.canonicalize_path(locked))])
            self.assertEqual(
                payload["reclaimable_bytes"],
                by_plan[str(plans.canonicalize_path(sweepable))],
            )
            self.assertTrue(
                (
                    locked_state / state.RUNS_DIR_NAME / "20260401T000000000000Z-11"
                ).is_dir()
            )

    def test_gc_cli_preserves_a_dead_local_lock_in_both_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            plan_dir = tmp_dir / "dead-locked"
            write_plan(plan_dir, ("pending",))
            old = self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260401T000000000000Z-11",
                days_ago=90,
                log_bytes=2048,
            )
            self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260803T000000000000Z-22",
                days_ago=1,
                log_bytes=16,
            )
            # A pid this host can prove dead: the lock every other command
            # reclaims on sight, and the one `gc` has promised not to touch.
            lock_path = state.plan_state_dir(plan_dir, state_dir) / state.LOCK_FILE_NAME
            lock_path.write_text(
                json.dumps(
                    {
                        "plan_dir": str(plans.canonicalize_path(plan_dir)),
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "pid": 4_242_424,
                        "hostname": socket.gethostname(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            lock_bytes = lock_path.read_bytes()

            def _gc(*argv: str) -> dict:
                args = cli.build_parser().parse_args(
                    ["gc", *argv, "--state-dir", str(state_dir), "--json"]
                )
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(0, cli.resolve_handler(args)(args))
                return json.loads(stdout.getvalue())

            def _decisions(payload: dict) -> list:
                entry = next(
                    plan
                    for plan in payload["plans"]
                    if plan["plan_dir"] == str(plans.canonicalize_path(plan_dir))
                )
                return [
                    (run["run_id"], run["reclaimable"], run["reason"])
                    for run in entry["runs"]
                ]

            corpus = _gc()
            self.assertEqual(lock_bytes, lock_path.read_bytes())
            plan = _gc(str(plan_dir))
            self.assertEqual(lock_bytes, lock_path.read_bytes())

            self.assertEqual(_decisions(corpus), _decisions(plan))
            self.assertEqual(
                {reason for _id, _reclaimable, reason in _decisions(plan)},
                {retention.RETAIN_PLAN_LOCKED},
            )
            self.assertEqual(0, plan["reclaimable_run_count"])

            applied = _gc(str(plan_dir), "--apply")
            self.assertEqual(0, applied["removed_run_count"])
            self.assertEqual(lock_bytes, lock_path.read_bytes())
            self.assertTrue(old.is_dir())

    def test_gc_rejects_apply_together_with_dry_run(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["gc", "--apply", "--dry-run"])

        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
