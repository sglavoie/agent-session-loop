"""`asl split` CLI tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import authoring, cli, harnesses, preflight
from tests.support_asl_cli_split import SplitCliHelpers


class SplitCommandTests(SplitCliHelpers, unittest.TestCase):
    """`asl split` CLI tests."""

    def test_split_subparser_is_wired_with_defaults(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(
            ["split", "--plan-file", "P.md", "--contracts-file", "c.json"]
        )
        self.assertEqual("authoring:split_from_args", parsed.handler_ref)
        self.assertIs(cli.resolve_handler(parsed), authoring.split_from_args)
        self.assertEqual("auto", parsed.skill_prefix)
        self.assertFalse(parsed.inline_stdin)
        # A source is required and the two sources are mutually exclusive.
        with self.assertRaises(SystemExit):
            parser.parse_args(["split", "--contracts-file", "c.json"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "split",
                    "--plan-file",
                    "P.md",
                    "--inline-stdin",
                    "--contracts-file",
                    "c.json",
                ]
            )

    def test_split_happy_path_produces_preflight_clean_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            base = tmp_dir / "out"

            code, out, err = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                )
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual("split", payload["command"])
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertEqual("draft", payload["readiness"]["state"])
            self.assertEqual("$", payload["skill_prefix"])
            self.assertIn(
                "session-expand", payload["next_steps"][0]["invocations"]["codex"]
            )

            output_dir = Path(payload["output_dir"])
            self.assertTrue(output_dir.is_dir())
            self.assertTrue((output_dir / "session-1.md").is_file())
            self.assertTrue((output_dir / "session-2.md").is_file())
            self.assertTrue((output_dir / "manifest.md").is_file())
            self.assertTrue((output_dir / "progress.md").is_file())
            self.assertEqual(
                sorted(
                    str(output_dir / name)
                    for name in (
                        "context.md",
                        "manifest.md",
                        "progress.md",
                        "session-1.md",
                        "session-2.md",
                    )
                ),
                sorted(payload["files"]),
            )

            report = preflight.run_core_preflight(output_dir)
            self.assertEqual((), report.red_findings)

    def test_split_single_session_reports_not_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text("# Plan\n\n## Session 1: Only\n\nDo.\n", encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1",))

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("single-session", payload["reason"])
            self.assertIn("splitting is not needed", payload["error"])

    def test_split_malformed_plan_passes_through_line_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text("# Plan\n\n## Session foo\n\nBody\n", encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1",))

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("line", payload["error"])
            self.assertIn("invalid session id", payload["error"])

    def test_split_missing_contract_reason_and_no_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1",))
            base = tmp_dir / "out"

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("missing-contract:2", payload["reason"])
            # Contracts are validated before the output dir is created.
            self.assertFalse(base.exists())

    def test_split_malformed_contract_reason_and_no_dir(self) -> None:
        # A malformed contract entry (a non-object, or one missing a required
        # field) must fail closed with a stable structured reason and no
        # traceback — never as an uncaught TypeError/KeyError — and must not
        # strand an output directory.
        valid_entry = {
            "done_when": ["Session 1 lands."],
            "verify": {
                "session": "1",
                "checks": [
                    {
                        "type": "command",
                        "argv": ["python3", "-m", "unittest", "-q"],
                        "timeout_seconds": 60,
                    }
                ],
            },
        }
        cases = (
            # A non-object entry for the first session (subscripting it would
            # otherwise raise TypeError).
            ({"1": "not-an-object", "2": "also-not"}, "invalid-contract:1"),
            # A present-but-incomplete entry (missing done_when) reached only
            # after the first session's valid contract builds cleanly.
            (
                {"1": valid_entry, "2": {"verify": {"checks": []}}},
                "invalid-contract:2",
            ),
        )
        for index, (contract_payload, expected_reason) in enumerate(cases):
            with (
                self.subTest(reason=expected_reason),
                tempfile.TemporaryDirectory() as tmp,
            ):
                tmp_dir = Path(tmp)
                plan = tmp_dir / "PLAN.md"
                plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
                contracts_file = tmp_dir / "contracts.json"
                contracts_file.write_text(
                    json.dumps(contract_payload), encoding="utf-8"
                )
                base = tmp_dir / f"out{index}"

                code, out, _ = self._run_split(
                    self._split_args(
                        plan_file=str(plan),
                        contracts_file=str(contracts_file),
                        base_dir=str(base),
                    )
                )

                self.assertEqual(1, code)
                payload = json.loads(out)
                self.assertFalse(payload["ok"])
                self.assertEqual(expected_reason, payload["reason"])
                self.assertIn("invalid contract", payload["error"])
                self.assertFalse(base.exists())

    def test_split_inline_stdin_captures_durable_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            base = tmp_dir / "out"

            code, out, err = self._run_split(
                self._split_args(
                    inline_stdin=True,
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                ),
                stdin=self._TWO_SESSION_PLAN,
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertTrue(payload["source_path"].endswith(".md"))
            self.assertIn("sources", payload["source_path"])
            self.assertTrue(Path(payload["source_path"]).is_file())

    def test_split_output_naming_is_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            base = tmp_dir / "out"
            args = self._split_args(
                plan_file=str(plan),
                contracts_file=str(contracts_file),
                base_dir=str(base),
            )

            _, out1, _ = self._run_split(args)
            _, out2, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                    force_new=True,
                )
            )

            first = Path(json.loads(out1)["output_dir"])
            second = Path(json.loads(out2)["output_dir"])
            self.assertNotEqual(first, second)
            self.assertEqual(f"{first.name}-2", second.name)

    def test_split_skill_prefix_auto_uses_detected_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))

            with mock.patch.object(
                harnesses, "detect_invoking_harness", return_value="claude"
            ):
                code, out, err = self._run_split(
                    self._split_args(
                        plan_file=str(plan),
                        contracts_file=str(contracts_file),
                        base_dir=str(tmp_dir / "out"),
                        skill_prefix="auto",
                    )
                )

            self.assertEqual(0, code, err)
            self.assertEqual("/", json.loads(out)["skill_prefix"])

    def test_split_skill_prefix_auto_undetectable_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))

            with mock.patch.object(
                harnesses, "detect_invoking_harness", return_value=None
            ):
                code, out, _ = self._run_split(
                    self._split_args(
                        plan_file=str(plan),
                        contracts_file=str(contracts_file),
                        base_dir=str(tmp_dir / "out"),
                        skill_prefix="auto",
                    )
                )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("auto-detect", payload["error"])

    def test_split_analysis_file_appends_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            analysis = tmp_dir / "analysis.md"
            analysis.write_text("## Token Budget\n\nSaved 42%.\n", encoding="utf-8")

            code, out, err = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                    analysis_file=str(analysis),
                )
            )

            self.assertEqual(0, code, err)
            output_dir = Path(json.loads(out)["output_dir"])
            manifest = (output_dir / "manifest.md").read_text(encoding="utf-8")
            self.assertIn("Saved 42%.", manifest)

    def test_split_missing_contracts_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(tmp_dir / "does-not-exist.json"),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("input-error", payload["reason"])


if __name__ == "__main__":
    unittest.main()
