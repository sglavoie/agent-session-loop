"""Contract payload round trip and `asl contract-check` tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_contract,
    cli,
    contracts,
    plans,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)


class ContractPayloadRoundTripTests(unittest.TestCase):
    """`contracts.check_payload` is the inverse of check parsing."""

    def round_trip(self, payload: dict[str, Any]) -> dict[str, Any]:
        parsed = contracts._parse_check(
            payload, index=0, session_id=plans.parse_session_id("1")
        )
        return contracts.check_payload(parsed)

    def test_a_command_check_round_trips(self) -> None:
        payload = {
            "type": "command",
            "name": "suite",
            "argv": ["just", "test-all"],
            "timeout_seconds": 900.0,
            "cwd": "sub",
            "baseline_expectation": "fail",
            "on_failure_of": ["lint"],
        }

        self.assertEqual(payload, self.round_trip(payload))

    def test_a_regex_check_round_trips(self) -> None:
        payload = {
            "type": "regex_in_file",
            "name": "documented",
            "path": "docs/agent-session-loop.md",
            "pattern": "broad:",
        }

        self.assertEqual(payload, self.round_trip(payload))

    def test_defaulted_fields_are_not_re_emitted(self) -> None:
        # A payload that omits a field parses to its default, and re-emitting
        # that default would make an unchanged check look edited.
        emitted = self.round_trip(
            {"type": "file_exists", "name": "readme", "path": "README.md"}
        )

        self.assertEqual(
            {"type": "file_exists", "name": "readme", "path": "README.md"}, emitted
        )


class ContractCheckCliTests(unittest.TestCase):
    """`asl contract-check` static per-check contract diagnosis."""

    def _run(self, plan_dir: Path, cwd: Path, *extra: str) -> tuple[int, dict]:
        args = cli.build_parser().parse_args(
            ["contract-check", str(plan_dir), "--cwd", str(cwd), "--json", *extra]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.resolve_handler(args)(args)
        return code, json.loads(stdout.getvalue())

    def test_parser_wiring(self) -> None:
        args = cli.build_parser().parse_args(
            ["contract-check", "/plan", "--session", "2"]
        )
        self.assertIs(verification.contract_check_from_args, cli.resolve_handler(args))
        self.assertEqual("/plan", args.plan_dir)
        self.assertEqual("2", args.session)
        self.assertEqual(".", args.cwd)
        self.assertFalse(args.json)
        self.assertFalse(args.agent_mux)
        self.assertIsNone(args.state_dir)

    def _ok_plan(self, tmp_dir: Path) -> tuple[Path, Path]:
        plan_dir = tmp_dir / "plan"
        repo_root = tmp_dir / "repo"
        repo_root.mkdir()
        contract = _contract_block('{"type": "command", "name": "c", "argv": ["true"]}')
        write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})
        return plan_dir, repo_root

    def test_agent_mux_block_is_absent_unless_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, repo_root = self._ok_plan(Path(tmp))

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(0, code)
            self.assertNotIn("agent_mux", payload)

    def test_agent_mux_block_reports_the_contract_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, repo_root = self._ok_plan(tmp_dir)
            state_dir = tmp_dir / "state"

            code, payload = self._run(
                plan_dir,
                repo_root,
                "--agent-mux",
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(0, code)
            self.assertEqual("ok", payload["reason"])
            block = payload["agent_mux"]
            self.assertEqual(
                agent_mux_contract.contract_set_id(), block["contract_set_id"]
            )
            self.assertEqual(agent_mux_contract.corpus_digest(), block["corpus_digest"])
            # The state root is reported canonicalized, the same way every
            # other ASL surface resolves `--state-dir`.
            self.assertEqual(
                str(plans.canonicalize_path(state_dir)), block["state_root"]
            )
            self.assertEqual(
                list(agent_mux_contract.COMPONENTS),
                [component["component"] for component in block["components"]],
            )
            self.assertEqual([], block["issues"])
            self.assertEqual([], block["fixtures"]["failures"])

    def test_an_unreadable_corpus_is_a_structured_error(self) -> None:
        # The corpus is checked-in data, so a missing one is an environment
        # failure with its own reason rather than a traceback or a silent pass.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, repo_root = self._ok_plan(Path(tmp))
            with mock.patch.object(
                agent_mux_contract,
                "check_corpus",
                side_effect=agent_mux_contract.ContractError("corpus is missing"),
            ):
                code, payload = self._run(plan_dir, repo_root, "--agent-mux")

            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual("contract-set-error", payload["reason"])
            self.assertIn("corpus is missing", payload["error"])

    def test_registered_checks_resolve_static_paths_by_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            (repositories["SVC"] / "only-svc.txt").write_text(
                "hello\n", encoding="utf-8"
            )
            plan_dir = root / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "path": "only-svc.txt", '
                '"name": "selected", "repo": "SVC"}'
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract},
                specs=specs,
            )

            code, payload = self._run(plan_dir, workspace_root)

            self.assertEqual(0, code)
            row = payload["checks"][0]
            self.assertEqual("SVC:only-svc.txt", row["qualified_path"])
            self.assertEqual(str(repositories["SVC"].resolve()), row["resolved_root"])

    def test_registered_symlink_escape_is_a_structured_check_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:1]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            outside = root / "outside"
            outside.mkdir()
            (outside / "target.txt").write_text("outside\n", encoding="utf-8")
            (repositories["SVC"] / "escape").symlink_to(
                outside, target_is_directory=True
            )
            plan_dir = root / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "path": "escape/target.txt", '
                '"name": "escape", "repo": "SVC"}'
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract},
                specs=specs,
            )

            code, payload = self._run(plan_dir, workspace_root)

            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual("contract-issues", payload["reason"])
            row = payload["checks"][0]
            self.assertEqual("schema-error", row["status"])
            self.assertEqual("repository-contract", row["reason"])
            self.assertEqual("escape", row["name"])
            self.assertEqual("SVC", row["repo"])
            self.assertIn("escapes repository SVC", row["detail"])

    def test_all_ok_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("hello\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "target.txt", "name": "f"}, '
                '{"type": "command", "name": "c", "argv": ["true"]}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual(0, payload["issue_count"])
            self.assertEqual({"ok"}, {row["status"] for row in payload["checks"]})
            self.assertEqual([None, None], [row["repo"] for row in payload["checks"]])

    def test_static_contract_row_reports_repository_alias(self) -> None:
        contract = contracts.CompletionContract(
            session_id=plans.parse_session_id("1"),
            done_when=("done",),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c",
                        argv=("true",),
                        timeout_seconds=5,
                        repo="SVC",
                    ),
                )
            ),
        )
        with mock.patch.object(
            verification, "read_completion_contract", return_value=contract
        ):
            rows = verification._contract_check_rows(
                "/plan", plans.parse_session_id("1"), repo_root=Path(".")
            )

        self.assertEqual("SVC", rows[0]["repo"])

    def test_file_missing_is_factual_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "f"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual("contract-issues", payload["reason"])
            row = payload["checks"][0]
            self.assertEqual("factual-issue", row["status"])
            self.assertEqual("file-missing", row["reason"])
            self.assertEqual("1", row["session"])

    def test_regex_not_matched_is_factual_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("nothing here\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "regex_in_file", "path": "target.txt", '
                '"pattern": "WANTED", "name": "r"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("regex-not-matched", row["reason"])

    def test_command_not_runnable_is_factual_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "command", "name": "c", '
                '"argv": ["asl-contract-check-missing-xyz"]}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("command-not-runnable", row["reason"])

    def test_unversioned_python_is_factual_issue(self) -> None:
        # An unversioned ``python`` interpreter is frequently unavailable where
        # ``python3`` exists, so it stays actionable with the ``bare-python``
        # reason and a "use `python3`" detail (matching session-expand).
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "command", "name": "c", '
                '"argv": ["python", "-c", "raise SystemExit(0)"]}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("factual-issue", row["status"])
            self.assertEqual("bare-python", row["reason"])
            self.assertIn("use `python3`", row["detail"])

    def test_versioned_python3_inline_check_is_ok(self) -> None:
        # ``python3 -c`` and ``python3 <script>`` are valid inline evidence
        # checks and must not be rejected solely for lacking ``-m``.
        for argv in (
            '["python3", "-c", "raise SystemExit(0)"]',
            '["python3", "check_evidence.py"]',
            '["python3.11", "-c", "raise SystemExit(0)"]',
        ):
            with (
                self.subTest(argv=argv),
                tempfile.TemporaryDirectory() as tmp,
            ):
                tmp_dir = Path(tmp)
                plan_dir = tmp_dir / "plan"
                repo_root = tmp_dir / "repo"
                repo_root.mkdir()
                contract = _contract_block(
                    f'{{"type": "command", "name": "c", "argv": {argv}}}'
                )
                write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

                code, payload = self._run(plan_dir, repo_root)

                row = payload["checks"][0]
                self.assertNotEqual("bare-python", row["reason"])

    def test_missing_contract_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("missing-contract", row["status"])
            self.assertIsNone(row["name"])

    def test_schema_error_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            # A command check with no argv fails the contract parser.
            contract = _contract_block('{"type": "command", "name": "c"}')
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("schema-error", row["status"])

    def test_contract_check_rows_have_uniform_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            valid = _contract_block(
                '{"type": "command", "name": "ok", "argv": ["true"]}',
                session="1",
            )
            malformed = _contract_block(
                '{"type": "command", "name": "bad"}', session="3"
            )
            write_plan(
                plan_dir,
                ("pending", "pending", "pending"),
                contracts_by_id={"1": valid, "3": malformed},
            )

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            rows_by_status = {row["status"]: row for row in payload["checks"]}
            self.assertEqual(
                {"ok", "missing-contract", "schema-error"}, set(rows_by_status)
            )
            shapes = {frozenset(row) for row in payload["checks"]}
            self.assertEqual(1, len(shapes), [sorted(shape) for shape in shapes])

    def test_session_flag_narrows_to_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "ok.txt").write_text("x\n", encoding="utf-8")
            good = _contract_block(
                '{"type": "file_exists", "path": "ok.txt", "name": "f"}'
            )
            bad = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "f"}',
                session="2",
            )
            write_plan(
                plan_dir,
                ("pending", "pending"),
                contracts_by_id={"1": good, "2": bad},
            )

            # Session 1 alone is clean even though session 2 has an issue.
            code, payload = self._run(plan_dir, repo_root, "--session", "1")
            self.assertEqual(0, code)
            self.assertEqual({"1"}, {row["session"] for row in payload["checks"]})
            self.assertEqual("1", payload["session"])

    def test_unknown_session_is_invalid_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            code, payload = self._run(plan_dir, repo_root, "--session", "9")
            self.assertEqual(1, code)
            self.assertEqual("invalid-session", payload["reason"])
