"""Status label resolution CLI tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import cli, events, plans_files
from tests.support import (
    REPO_ROOT,
    write_plan,
)


def _register_plan_in_state(state_dir: Path, plan_dir: Path) -> None:
    """Record ``plan_dir`` in a per-plan ``state.json`` under ``state_dir``.

    Mirrors the persisted metadata that ``asl sessions`` scans so a bare
    compact label routed through ``asl status`` resolves back to this plan.
    """
    canonical = plans_files.canonicalize_path(plan_dir)
    entry = state_dir / plans_files.path_sha256(canonical)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / events.STATE_FILE_NAME).write_text(
        json.dumps({"plan_dir": str(canonical)}, sort_keys=True),
        encoding="utf-8",
    )


class StatusLabelResolutionCliTests(unittest.TestCase):
    """Status label resolution CLI tests."""

    def _run_status(
        self, args: list[str], *, check: bool
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent_session_loop", "status", *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=check,
        )

    def test_compact_label_from_sessions_resolves_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plans" / "04f2f9095c86-14c7dd6b7f18"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            _register_plan_in_state(state_dir, plan_dir)

            completed = self._run_status(
                [plan_dir.name, "--state-dir", str(state_dir), "--json"],
                check=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("Manifest", payload["plan_title"])
            self.assertEqual(
                str(plans_files.canonicalize_path(plan_dir)), payload["plan_dir"]
            )

    def test_compact_label_text_output_reports_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plans" / "text-label-plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            _register_plan_in_state(state_dir, plan_dir)

            completed = self._run_status(
                [plan_dir.name, "--state-dir", str(state_dir)],
                check=True,
            )
            self.assertEqual("Plan title: Manifest", completed.stdout.splitlines()[0])
            self.assertIn(
                str(plans_files.canonicalize_path(plan_dir)), completed.stdout
            )

    def test_full_path_takes_precedence_over_state_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plans" / "precedence-plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            # Deliberately do NOT register in state: a full path must resolve
            # directly without needing any corpus entry.

            completed = self._run_status(
                [str(plan_dir), "--state-dir", str(state_dir), "--json"],
                check=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(
                str(plans_files.canonicalize_path(plan_dir)), payload["plan_dir"]
            )

    def test_relative_path_with_directory_component_bypasses_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "sub" / "relative-plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    os.path.join("sub", "relative-plan"),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=tmp_dir,
                text=True,
                capture_output=True,
                check=True,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(
                str(plans_files.canonicalize_path(plan_dir)), payload["plan_dir"]
            )

    def test_unknown_label_error_uses_plan_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()

            completed = self._run_status(
                ["no-such-label-xyz", "--state-dir", str(state_dir), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertEqual("no-such-label-xyz", payload["plan_reference"])
            self.assertNotIn("plan_dir", payload)
            self.assertIn(
                "no plan label from `asl sessions` matches 'no-such-label-xyz'",
                payload["error"],
            )

    def test_path_argument_error_does_not_mention_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-plan"

            completed = self._run_status([str(missing)], check=False)
            self.assertEqual(1, completed.returncode)
            self.assertIn("plan directory does not exist", completed.stderr)
            self.assertNotIn("asl sessions", completed.stderr)

    def test_file_argument_reports_not_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = Path(tmp) / "plan.md"
            plan_file.write_text("# Plan\n", encoding="utf-8")

            completed = self._run_status([str(plan_file)], check=False)
            self.assertEqual(1, completed.returncode)
            self.assertIn("asl status: plan path is not a directory", completed.stderr)

    def test_existing_malformed_plan_dir_retains_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "broken-plan"
            plan_dir.mkdir()

            completed = self._run_status(
                [str(plan_dir), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertEqual(str(plan_dir), payload["plan_reference"])

    def test_ambiguous_label_json_lists_sorted_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            first = tmp_dir / "a" / "dup-label"
            second = tmp_dir / "b" / "dup-label"
            write_plan(first, ("pending",))
            write_plan(second, ("pending",))
            _register_plan_in_state(state_dir, first)
            _register_plan_in_state(state_dir, second)

            completed = self._run_status(
                ["dup-label", "--state-dir", str(state_dir), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("ambiguous-plan", payload["reason"])
            self.assertEqual("dup-label", payload["plan_reference"])
            expected_candidates = sorted(
                [
                    str(plans_files.canonicalize_path(first)),
                    str(plans_files.canonicalize_path(second)),
                ]
            )
            self.assertEqual(expected_candidates, payload["details"]["candidates"])
            self.assertEqual(
                str(plans_files.canonicalize_path(state_dir)),
                payload["details"]["state_root"],
            )

    def test_ambiguous_label_text_instructs_full_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            first = tmp_dir / "a" / "dup-text"
            second = tmp_dir / "b" / "dup-text"
            write_plan(first, ("pending",))
            write_plan(second, ("pending",))
            _register_plan_in_state(state_dir, first)
            _register_plan_in_state(state_dir, second)

            completed = self._run_status(
                ["dup-text", "--state-dir", str(state_dir)],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertIn("ambiguous", completed.stderr)
            self.assertIn("full plan directory path", completed.stderr)

    def test_non_directory_state_root_reports_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state-file"
            state_file.write_text("not a directory", encoding="utf-8")

            completed = self._run_status(
                ["some-label", "--state-dir", str(state_file), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertEqual("some-label", payload["plan_reference"])

    def test_status_help_advertises_plan_dir_or_id(self) -> None:
        parser = cli.build_parser(prog="asl")
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["status", "--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("PLAN_DIR_OR_ID", help_text)
        self.assertIn(
            "compact plan label printed after 'plan' by 'asl sessions'", help_text
        )


if __name__ == "__main__":
    unittest.main()
