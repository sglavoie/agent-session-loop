"""SessionsFromArgsTests coverage."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import plans_files, session_overview, state
from tests.session_overview_support import (
    _active_turn_payload,
    _build_args,
    _make_plan_state_dir,
    _make_turn_artifact,
    _turn_result_payload,
)


class SessionsFromArgsTests(unittest.TestCase):
    def _make_populated_state_root(self, root: Path) -> Path:
        state_root = root / "state"
        plan_alpha = root / "plans" / "alpha-plan-dir"
        plan_beta = root / "plans" / "beta-plan-dir"
        plan_alpha.mkdir(parents=True)
        plan_beta.mkdir(parents=True)

        alpha_active = _active_turn_payload(
            run_id="alpha-run", turn_index=2, session_id="2"
        )
        _make_plan_state_dir(
            state_root,
            plan_alpha,
            lock={"pid": os.getpid()},
            state_payload={"active_turn": alpha_active},
        )
        alpha_state_dir = state_root / plans_files.path_sha256(
            plans_files.canonicalize_path(plan_alpha)
        )
        _make_turn_artifact(
            alpha_state_dir,
            run_id="alpha-run",
            turn_label="0001",
            payload=_turn_result_payload(
                plan_dir=plans_files.canonicalize_path(plan_alpha),
                run_id="alpha-run",
                turn_index=1,
                session_id="1",
                finished_at="2026-06-19T02:00:00+00:00",
            ),
        )

        _make_plan_state_dir(state_root, plan_beta)
        beta_state_dir = state_root / plans_files.path_sha256(
            plans_files.canonicalize_path(plan_beta)
        )
        _make_turn_artifact(
            beta_state_dir,
            run_id="beta-run",
            turn_label="0001",
            payload=_turn_result_payload(
                plan_dir=plans_files.canonicalize_path(plan_beta),
                run_id="beta-run",
                turn_index=1,
                session_id="1",
                finished_at="2026-06-19T01:00:00+00:00",
            ),
        )
        return state_root

    def test_json_output_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual("sessions", payload["command"])
            self.assertEqual({"running": True, "recent": True}, payload["filters"])
            self.assertEqual(session_overview.DEFAULT_RECENT_LIMIT, payload["limit"])
            self.assertEqual(
                str(plans_files.canonicalize_path(state_root)),
                payload["state_root"],
            )
            self.assertEqual(1, len(payload["running"]))
            self.assertEqual(2, len(payload["recent"]))
            # Recent rows are sorted newest finish first.
            self.assertEqual(
                "2026-06-19T02:00:00+00:00", payload["recent"][0]["finished_at"]
            )
            self.assertEqual(
                payload["counts"],
                {
                    "running": 1,
                    "recent": 2,
                    "stale_locks": 0,
                    "skipped": 0,
                },
            )

    def test_running_filter_skips_recent_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, running=True, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertEqual({"running": True, "recent": False}, payload["filters"])
            self.assertEqual([], payload["recent"])
            self.assertEqual(0, payload["counts"]["recent"])
            self.assertEqual(1, len(payload["running"]))

    def test_recent_filter_skips_running_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, recent=True, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertEqual({"running": False, "recent": True}, payload["filters"])
            self.assertEqual([], payload["running"])
            self.assertEqual(0, payload["counts"]["running"])
            self.assertEqual(2, len(payload["recent"]))

    def test_limit_clamps_recent_rows_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, limit=1, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(1, payload["limit"])
            self.assertEqual(1, len(payload["recent"]))
            self.assertEqual(1, len(payload["running"]))

    def test_text_output_uses_basename_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(_build_args(state_root))

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn(
                f"State root: {plans_files.canonicalize_path(state_root)}",
                output,
            )
            self.assertIn("Running sessions (1):", output)
            self.assertIn("Recent sessions (2):", output)
            self.assertIn("plan alpha-plan-dir", output)
            self.assertIn("plan beta-plan-dir", output)
            # Default text output omits full plan paths.
            self.assertNotIn(
                str(plans_files.canonicalize_path(root / "plans" / "alpha-plan-dir")),
                output,
            )

    def test_verbose_text_output_uses_full_canonical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, verbose=True)
                )

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            canonical_alpha = str(
                plans_files.canonicalize_path(root / "plans" / "alpha-plan-dir")
            )
            canonical_beta = str(
                plans_files.canonicalize_path(root / "plans" / "beta-plan-dir")
            )
            self.assertIn(canonical_alpha, output)
            self.assertIn(canonical_beta, output)

    def test_text_empty_state_root_renders_none_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-state-root"

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(_build_args(missing))

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn("Running sessions: none", output)
            self.assertIn("Recent sessions: none", output)

    def test_json_empty_state_root_returns_success_with_empty_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-state-root"

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(missing, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual([], payload["running"])
            self.assertEqual([], payload["recent"])
            self.assertEqual(
                payload["counts"],
                {"running": 0, "recent": 0, "stale_locks": 0, "skipped": 0},
            )

    def test_existing_file_state_root_returns_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state-file"
            state_file.write_text("", encoding="utf-8")

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_file, json_output=True)
                )

            self.assertEqual(1, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertIn("not a directory", payload["error"])

    def test_state_root_iterdir_error_returns_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()

            with mock.patch.object(
                Path, "iterdir", side_effect=PermissionError("denied")
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    exit_code = session_overview.sessions_from_args(
                        _build_args(state_root, json_output=True)
                    )

            self.assertEqual(1, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertIn("could not scan state root", payload["error"])

    def test_negative_limit_returns_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, limit=-1, json_output=True)
                )

            self.assertEqual(1, exit_code)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("--limit", payload["error"])

    def test_state_root_error_returns_nonzero_with_error_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "broken"

            with mock.patch.object(state, "state_root", side_effect=OSError("no")):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = session_overview.sessions_from_args(
                        _build_args(target, json_output=True)
                    )

            self.assertEqual(1, exit_code)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertIn("no", payload["error"])


if __name__ == "__main__":
    unittest.main()
