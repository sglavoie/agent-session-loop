"""`asl register-repositories` over a source plan, before any split."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import authoring_parse, cli, repository_registration
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_multi_repo_workspace,
)


class RegisterRepositoriesSourcePlanTests(unittest.TestCase):
    """`asl register-repositories` over a source plan, before any split."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(os.path.realpath(self._temp.name))
        self.workspace = self.tmp / "workspace"
        repositories = init_multi_repo_workspace(
            self.workspace, specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.svc = repositories["SVC"]
        self.oak = repositories["OAK"]

    def _source_plan(self, *, stray_contract_path: bool = False) -> str:
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
            outside = self.tmp / "artifacts"
            outside.mkdir(exist_ok=True)
            checks.append(
                {
                    "name": "stray",
                    "type": "file_exists",
                    "path": f"{outside}/report.txt",
                }
            )
        return "\n".join(
            [
                "# Test Plan",
                "",
                "## Context",
                "",
                "Shared context.",
                "",
                "## Session 1: First",
                "",
                "Add the module.",
                "",
                _contract_block(
                    ", ".join(json.dumps(check) for check in checks),
                    session="1",
                ),
                "",
                "---",
                "",
                "## Session 2: Second",
                "",
                f"Read {self.oak}/README.md before editing.",
                "",
                _contract_block(
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
                "",
            ]
        )

    def _write_source_plan(self, **kwargs: bool) -> Path:
        path = self.tmp / "PLAN.md"
        path.write_text(self._source_plan(**kwargs), encoding="utf-8")
        return path

    def _register(self, *extra: str, stdin: str | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [
            "register-repositories",
            "--cwd",
            str(self.svc),
            *extra,
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is None:
                code = cli.main(argv)
            else:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _split_check(self, path: Path) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            cli.main(
                [
                    "split",
                    "--check",
                    "--plan-file",
                    str(path),
                    "--cwd",
                    str(self.svc),
                    "--json",
                ]
            )
        return json.loads(stdout.getvalue() or stderr.getvalue())

    def test_migrated_plan_file_passes_split_check_and_spares_its_input(
        self,
    ) -> None:
        source = self._write_source_plan()
        original = source.read_bytes()
        output = self.tmp / "migrated.md"

        code, stdout, stderr = self._register(
            "--plan-file", str(source), "--output", str(output)
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual(original, source.read_bytes())
        self.assertIn("Status: applied", stdout)
        self.assertIn(str(output), stdout)

        migrated = output.read_text(encoding="utf-8")
        self.assertIn("## Repositories", migrated)
        self.assertIn(
            "| tundra_svc | tundra_svc | https://github.com/tundra/SVC |",
            migrated,
        )
        parsed = authoring_parse.parse_plan_document(migrated)
        self.assertEqual(
            [["tundra_OAK", "tundra_svc"], ["tundra_OAK"]],
            [list(session.scope.reads) for session in parsed.sessions],
        )
        self.assertEqual(
            [["tundra_OAK", "tundra_svc"], ["tundra_OAK"]],
            [list(session.scope.writes) for session in parsed.sessions],
        )
        self.assertTrue(self._split_check(output)["ok"])

    def test_inline_stdin_puts_the_whole_migrated_plan_on_stdout(self) -> None:
        code, stdout, stderr = self._register(
            "--inline-stdin", stdin=self._source_plan()
        )

        self.assertEqual(0, code, stderr)
        piped = self.tmp / "piped.md"
        piped.write_text(stdout, encoding="utf-8")
        self.assertTrue(self._split_check(piped)["ok"])
        self.assertEqual(
            stdout,
            repository_registration.register_source_plan(
                self._source_plan(), cwd=self.svc
            ).markdown,
        )

    def test_json_output_carries_the_markdown_and_both_paths(self) -> None:
        source = self._write_source_plan()
        output = self.tmp / "migrated.md"

        code, stdout, stderr = self._register(
            "--plan-file", str(source), "--output", str(output), "--json"
        )

        self.assertEqual(0, code, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("applied", payload["status"])
        self.assertEqual(str(source), payload["source_path"])
        self.assertEqual(str(output), payload["output_path"])
        self.assertEqual(output.read_text(encoding="utf-8"), payload["markdown"])
        self.assertEqual(["1", "2"], payload["sessions_changed"])

    def test_refusal_emits_no_markdown_and_writes_no_output_file(self) -> None:
        source = self._write_source_plan(stray_contract_path=True)
        output = self.tmp / "migrated.md"

        code, stdout, _stderr = self._register(
            "--plan-file", str(source), "--output", str(output), "--json"
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("refused", payload["reason"])
        self.assertEqual("contract-path-outside-registry", payload["missing_fact"])
        self.assertIsNone(payload["markdown"])
        self.assertFalse(output.exists())

    def test_output_naming_the_input_is_refused_before_anything_is_read(
        self,
    ) -> None:
        source = self._write_source_plan()
        original = source.read_bytes()

        code, stdout, _stderr = self._register(
            "--plan-file", str(source), "--output", str(source), "--json"
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("never replaces its source", payload["error"])
        self.assertEqual(original, source.read_bytes())

    def test_exactly_one_source_is_required(self) -> None:
        source = self._write_source_plan()

        neither_code, neither_stdout, _err = self._register("--json")
        both_code, both_stdout, _both_err = self._register(
            str(self.tmp / "plan"), "--plan-file", str(source), "--json"
        )

        for code, stdout in (
            (neither_code, neither_stdout),
            (both_code, both_stdout),
        ):
            self.assertEqual(1, code)
            payload = json.loads(stdout)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("exactly one", payload["error"])

    def test_non_utf8_stdin_is_an_input_error_like_a_non_utf8_file(self) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(b"# Plan\n\xff\xfe\n"), "utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout), mock.patch("sys.stdin", stdin):
            code = cli.main(
                ["register-repositories", "--inline-stdin", "--cwd", str(self.svc)]
                + ["--json"]
            )

        self.assertEqual(1, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("cannot read source plan from stdin", payload["error"])


if __name__ == "__main__":
    unittest.main()
