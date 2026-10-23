"""`asl split check` CLI tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import cli, plans_content_hash
from tests.support_asl_cli_split import SplitCliHelpers


class SplitCheckCommandTests(SplitCliHelpers, unittest.TestCase):
    """`asl split check` CLI tests."""

    def test_check_subparser_makes_contracts_optional(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(["split", "--check", "--plan-file", "P.md"])
        self.assertTrue(parsed.check)
        self.assertIsNone(parsed.contracts_file)
        self.assertFalse(
            parser.parse_args(
                ["split", "--plan-file", "P.md", "--contracts-file", "c.json"]
            ).check
        )

    def test_split_without_contracts_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")

            code, out, _ = self._run_split(
                self._split_args(plan_file=str(plan), base_dir=str(Path(tmp) / "out"))
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("--contracts-file is required", payload["error"])

    def test_check_reports_every_finding_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._BROKEN_PLAN, encoding="utf-8")
            base = tmp_dir / "out"

            code, out, _ = self._run_split(
                self._split_args(plan_file=str(plan), check=True, base_dir=str(base))
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertTrue(payload["checked"])
            self.assertEqual(str(plan), payload["source_path"])
            self.assertEqual(
                [
                    (5, "punctuation-free-title"),
                    (9, "unsupported-title-separator"),
                    (13, "session-id-suffix"),
                ],
                [
                    (finding["line"], finding["category"])
                    for finding in payload["findings"]
                ],
            )
            self.assertEqual(
                "## Session 1: No colon", payload["findings"][0]["suggested_line"]
            )
            self.assertIsNone(payload["findings"][2]["suggested_line"])
            self.assertEqual(payload["findings"][0]["message"], payload["error"])
            self.assertFalse(base.exists())
            self.assertEqual(self._BROKEN_PLAN, plan.read_text(encoding="utf-8"))

    def test_check_clean_plan_reports_canonical_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")

            code, out, err = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["checked"])
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertEqual(2, payload["session_count"])
            self.assertNotIn("repositories", payload)
            self.assertNotIn("registry_digest", payload)

    def test_check_multi_repo_plan_reports_ordered_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._MULTI_REPO_PLAN, encoding="utf-8")

            code, out, err = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertEqual(
                ["SVC", "OAK"],
                [repo["alias"] for repo in payload["repositories"]],
            )
            self.assertEqual(64, len(payload["registry_digest"]))

    def test_split_multi_repo_plan_authors_registry_only_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._MULTI_REPO_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            contract_payload = json.loads(contracts_file.read_text(encoding="utf-8"))
            contract_payload["1"]["verify"]["checks"][0]["repo"] = "SVC"
            contract_payload["2"]["verify"]["checks"][0]["repo"] = "OAK"
            contracts_file.write_text(json.dumps(contract_payload), encoding="utf-8")

            code, out, err = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertEqual(
                ["SVC", "OAK"],
                [repo["alias"] for repo in payload["repositories"]],
            )
            output_dir = Path(payload["output_dir"])
            manifest = (output_dir / "manifest.md").read_text(encoding="utf-8")
            context = (output_dir / "context.md").read_text(encoding="utf-8")
            first_session = (output_dir / "session-1.md").read_text(encoding="utf-8")
            second_session = (output_dir / "session-2.md").read_text(encoding="utf-8")
            self.assertIn("## Repositories", manifest)
            self.assertIn("| SVC | tundra_svc | github.com/tundra/SVC |", manifest)
            self.assertNotIn("## Repositories", context)
            self.assertNotIn("| Alias | Path | Origin |", context)
            self.assertEqual(1, first_session.count("**Reads repositories**: SVC, OAK"))
            self.assertEqual(1, first_session.count("**Writes repositories**: SVC"))
            self.assertEqual(1, second_session.count("**Reads repositories**: OAK"))
            self.assertEqual(1, second_session.count("**Writes repositories**: (none)"))
            self.assertEqual(
                plans_content_hash.stable_readiness_sha256(output_dir),
                payload["readiness"]["readiness_sha256"],
            )

    def test_check_single_session_reports_not_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text("# Plan\n\n## Session 1: Only\n\nDo.\n", encoding="utf-8")

            code, out, _ = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("single-session", payload["reason"])
            self.assertEqual(["1"], payload["session_ids"])

    def test_check_missing_plan_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self._run_split(
                self._split_args(plan_file=str(Path(tmp) / "nope.md"), check=True)
            )

            self.assertEqual(1, code)
            self.assertEqual("input-error", json.loads(out)["reason"])

    def test_check_inline_stdin_never_persists_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"

            code, out, _ = self._run_split(
                self._split_args(inline_stdin=True, check=True, base_dir=str(base)),
                stdin=self._BROKEN_PLAN,
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertEqual("<stdin>", payload["source_path"])
            self.assertEqual(3, len(payload["findings"]))
            self.assertFalse(base.exists())

    def test_check_text_mode_prints_findings_and_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._BROKEN_PLAN, encoding="utf-8")

            code, _, err = self._run_split(
                self._split_args(plan_file=str(plan), check=True, json=False)
            )

            self.assertEqual(1, code)
            self.assertIn("3 malformed session heading(s)", err)
            self.assertIn("suggested: ## Session 1: No colon", err)

    def test_check_repair_round_trip_then_splits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(
                "# Plan\n\nContext.\n\n"
                "## Session 1 No colon\n\nDo one.\n\n"
                "## Session 2 / Bad sep\n\nDo two.\n",
                encoding="utf-8",
            )

            _, out, _ = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )
            lines = plan.read_text(encoding="utf-8").splitlines()
            for finding in json.loads(out)["findings"]:
                lines[finding["line"] - 1] = finding["suggested_line"]
            repaired = "\n".join(lines) + "\n"

            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            code, out, err = self._run_split(
                self._split_args(
                    inline_stdin=True,
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                ),
                stdin=repaired,
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertTrue(Path(payload["output_dir"]).is_dir())


if __name__ == "__main__":
    unittest.main()
