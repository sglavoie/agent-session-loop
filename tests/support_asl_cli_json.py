"""Shared plan/state writers for the JSON CLI test modules."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from agent_session_loop import (
    cli,
    plans_content_hash,
    plans_files,
    plans_models,
    plans_readiness,
    state,
    workbench_cli,
)
from tests.support import (
    patched_env,
    write_plan,
)

if TYPE_CHECKING:
    _Base = unittest.TestCase
else:
    _Base = object


class JsonCliHelpers(_Base):
    """Shared plan/state writers for the JSON CLI test modules."""

    def _write_readiness(
        self,
        plan_dir: Path,
        *,
        state_value: str,
        checked_at: str,
    ) -> None:
        plans_readiness.write_readiness_metadata(
            plan_dir,
            plans_models.ReadinessMetadata(
                state=state_value,
                readiness_sha256=plans_content_hash.stable_readiness_sha256(plan_dir),
                checked_at=checked_at,
            ),
        )

    def _run_plans(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["plans", *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def _run_plans_at(self, *argv: str, now: datetime) -> tuple[int, str, str]:
        """Run ``plans`` through the real parser against a pinned clock.

        Goes through ``build_parser`` rather than a hand-built namespace so the
        flags under test keep their real types and defaults, and calls the
        handler directly because that is where the command clock is injected.
        """
        args = cli.build_parser(prog="asl").parse_args(["plans", *argv])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = workbench_cli.plans_from_args(args, now=now)
        return code, stdout.getvalue(), stderr.getvalue()

    def _stale_days_verdict(
        self,
        *,
        checked_at: str,
        now: datetime,
        stale_days: str,
    ) -> bool:
        """Report whether ``--stale-days`` keeps a plan with this activity."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            data_home = tmp_dir / "data"
            share_root = data_home / "agent-plans"
            share_root.mkdir(parents=True)
            for name in ("config", "tmp", "home"):
                (tmp_dir / name).mkdir()

            plan_dir = share_root / "plan"
            write_plan(plan_dir, ("pending",))
            # No run state on disk, so effective activity falls back to the
            # readiness check stamp -- the only date this case controls.
            self._write_readiness(
                plan_dir,
                state_value="reviewed",
                checked_at=checked_at,
            )

            with patched_env(
                HOME=str(tmp_dir / "home"),
                XDG_CONFIG_HOME=str(tmp_dir / "config"),
                XDG_DATA_HOME=str(data_home),
                TMPDIR=str(tmp_dir / "tmp"),
            ):
                code, stdout, stderr = self._run_plans_at(
                    "--state-dir",
                    str(tmp_dir / "state"),
                    "--stale-days",
                    stale_days,
                    "--json",
                    now=now,
                )
            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            return json.loads(stdout)["plan_count"] == 1

    def _write_gc_run(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        run_id: str,
        days_ago: float,
        log_bytes: int,
    ) -> Path:
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / "state.json").write_text(
            json.dumps(
                {"plan_dir": str(plans_files.canonicalize_path(plan_dir))},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        run_dir = plan_state / state.RUNS_DIR_NAME / run_id
        turn_dir = run_dir / "turn-0001-session-1"
        turn_dir.mkdir(parents=True)
        (turn_dir / "stderr.log").write_bytes(b"x" * log_bytes)
        finished_at = datetime.now(UTC) - timedelta(days=days_ago)
        (turn_dir / "result.json").write_text(
            json.dumps(
                {"finished_at": finished_at.isoformat(), "status_result": "ok"},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return run_dir
