"""`asl register-repositories` command tests."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    cli,
    plans_content_hash,
    plans_registry,
    repository_registration,
    state_locks,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_multi_repo_workspace,
    write_plan,
)


class RegisterRepositoriesCommandTests(unittest.TestCase):
    """`asl register-repositories` command tests."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        tmp = Path(os.path.realpath(self._temp.name))
        self.workspace = tmp / "workspace"
        self.plan_dir = tmp / "plan"
        self.state_dir = tmp / "state"
        self.outside = tmp / "artifacts"
        repositories = init_multi_repo_workspace(
            self.workspace, specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.svc = repositories["SVC"]
        self.oak = repositories["OAK"]

    def _write_legacy_plan(self, *, stray_contract_path: bool = False) -> None:
        checks = [
            {"name": "svc-file", "type": "file_exists", "path": "src/new.py"},
            {
                "name": "oak-suite",
                "type": "command",
                "argv": ["true"],
                "cwd": str(self.oak),
            },
        ]
        if stray_contract_path:
            self.outside.mkdir(parents=True, exist_ok=True)
            checks.append(
                {
                    "name": "stray",
                    "type": "file_exists",
                    "path": f"{self.outside}/report.txt",
                }
            )
        write_plan(
            self.plan_dir,
            ("pending", "pending"),
            contracts_by_id={
                "1": _contract_block(
                    ", ".join(json.dumps(check) for check in checks), session="1"
                ),
                "2": _contract_block(
                    json.dumps(
                        {
                            "name": "oak-regex",
                            "type": "regex_in_file",
                            "path": f"{self.oak}/README.md",
                            "pattern": "test repo",
                        }
                    ),
                    session="2",
                ),
            },
        )

    def _register(self, *extra: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(
                [
                    "register-repositories",
                    str(self.plan_dir),
                    "--cwd",
                    str(self.svc),
                    "--state-dir",
                    str(self.state_dir),
                    *extra,
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_text_mode_prints_the_table_the_files_and_the_follow_ups(
        self,
    ) -> None:
        self._write_legacy_plan()

        code, stdout, stderr = self._register()

        self.assertEqual(0, code, stderr)
        self.assertIn("Status: applied", stdout)
        self.assertIn("| Alias | Path | Origin |", stdout)
        self.assertIn(
            "| tundra_svc | tundra_svc | github.com/tundra/SVC |",
            stdout,
        )
        for name in ("manifest.md", "session-1.md", "session-2.md"):
            self.assertIn(str(self.plan_dir / name), stdout)
        rebind = (
            f"asl prepare {self.plan_dir} --resume --rebind-workspace "
            f"--cwd {self.workspace}"
        )
        rerun = f"asl run {self.plan_dir} --cwd {self.workspace}"
        self.assertIn(rebind, stdout)
        self.assertIn(rerun, stdout)
        self.assertLess(stdout.index(rebind), stdout.index(rerun))

        registry = plans_registry.read_repository_registry(self.plan_dir)
        self.assertEqual(("tundra_OAK", "tundra_svc"), registry.aliases)

    def test_json_output_serializes_the_whole_registration_result(self) -> None:
        self._write_legacy_plan()

        code, stdout, stderr = self._register("--json")

        self.assertEqual(0, code, stderr)
        payload = json.loads(stdout)
        for field in dataclasses.fields(repository_registration.RegistrationResult):
            self.assertIn(field.name, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual("register-repositories", payload["command"])
        self.assertEqual("ok", payload["reason"])
        self.assertEqual("applied", payload["status"])
        self.assertIsNone(payload["missing_fact"])
        self.assertEqual(str(self.workspace), payload["workspace_root"])
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            [repository["alias"] for repository in payload["repositories"]],
        )
        self.assertEqual(["1", "2"], payload["sessions_changed"])
        self.assertIn(str(self.plan_dir / "manifest.md"), payload["files"])
        self.assertEqual(
            plans_content_hash.stable_readiness_sha256(self.plan_dir),
            payload["readiness"],
        )
        self.assertEqual(
            [
                f"asl prepare {self.plan_dir} --resume --rebind-workspace "
                f"--cwd {self.workspace}",
                f"asl run {self.plan_dir} --cwd {self.workspace}",
            ],
            payload["next_commands"],
        )

    def test_refusal_exits_one_and_names_the_missing_fact_on_both_surfaces(
        self,
    ) -> None:
        self._write_legacy_plan(stray_contract_path=True)

        text_code, _text_stdout, text_stderr = self._register()
        json_code, json_stdout, _json_stderr = self._register("--json")

        self.assertEqual(1, text_code)
        self.assertIn("missing fact: contract-path-outside-registry", text_stderr)
        self.assertIn("'stray'", text_stderr)

        self.assertEqual(1, json_code)
        payload = json.loads(json_stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("refused", payload["reason"])
        self.assertEqual("refused", payload["status"])
        self.assertEqual("contract-path-outside-registry", payload["missing_fact"])
        self.assertIn("'stray'", payload["detail"])
        self.assertIn("missing fact: contract-path-outside-registry", payload["error"])
        self.assertEqual([], payload["files"])
        self.assertIsNone(plans_registry.read_repository_registry(self.plan_dir))

    def test_active_lock_refuses_before_any_session_file_is_rewritten(
        self,
    ) -> None:
        self._write_legacy_plan()
        state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        code, stdout, _stderr = self._register("--json")

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("lock-contention", payload["reason"])
        self.assertIsNone(plans_registry.read_repository_registry(self.plan_dir))


if __name__ == "__main__":
    unittest.main()
