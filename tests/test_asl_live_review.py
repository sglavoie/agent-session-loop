"""Review acceptance safety and real command orchestration without model use."""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_session_loop import harness_compatibility
from tests import asl_live_redirect_reporting as entry
from tests import asl_live_review as review
from tests.asl_live_lifecycle_process import Commands

ASL_TEST_LANE = "integration"


class ReviewAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)

    def args(self, *extra: str) -> list[str]:
        return [
            "--suite",
            "review",
            "--harness",
            "codex",
            "--review-harness",
            "claude",
            "--json",
            *extra,
        ]

    def test_default_only_probes_both_versions(self) -> None:
        with (
            mock.patch.object(
                review, "disposable_workspace", side_effect=AssertionError
            ),
            mock.patch.dict(os.environ, {"ASL_LIVE_ACCEPTANCE": "0"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(0, entry.main(self.args()))
        report = json.loads(output.getvalue())
        self.assertFalse(report["complete"])
        self.assertFalse(report["model_use"])
        self.assertEqual({}, report["pair"]["verified_versions"])
        self.assertEqual({"codex", "claude"}, set(report["pair"]["harnesses"]))

    def test_invalid_pair_cannot_start_work(self) -> None:
        for argv in (
            ["--suite", "review", "--harness", "codex"],
            ["--suite", "review", "--all-harnesses", "--review-harness", "claude"],
            ["--suite", "review", "--harness", "codex", "--review-harness", "codex"],
            ["--suite", "redirect", "--harness", "codex", "--review-harness", "claude"],
        ):
            with (
                self.subTest(argv=argv),
                mock.patch.object(review, "run_review", side_effect=AssertionError),
                mock.patch("sys.stderr", new=io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                entry.main([*argv, "--live"])
            self.assertEqual(2, error.exception.code)

    def test_two_harnesses_share_launch_budget_and_restore_native_config(self) -> None:
        native = str(self.root / "native config")
        executable = self.root / "native"
        executable.write_text(
            "#!/bin/sh\n"
            '[ "$XDG_CONFIG_HOME" = ' + shlex.quote(native) + " ] || exit 9\n"
            "printf '{\"ok\":true}\\n'\n"
        )
        executable.chmod(0o700)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": native}):
            commands = Commands(self.root, deadline=30, max_harness_launches=2)
        for name in ("codex", "claude"):
            commands.bound_harness(name, str(executable))
        (self.root / "repo").mkdir()
        configured = json.loads(
            (self.root / "asl-config/agent-session-loop/harnesses.json").read_text()
        )
        for name in ("codex", "claude"):
            commands.call(name, [configured[name]["executable"], "turn"])
        with self.assertRaisesRegex(RuntimeError, "returned no JSON"):
            commands.call("exhausted", [configured["claude"]["executable"], "turn"])
        self.assertIn(
            "budget exhausted", Path(commands.calls[-1]["stderr"]).read_text()
        )
        ledger = [
            json.loads(row)
            for row in (self.root / "harness-launches.jsonl").read_text().splitlines()
        ]
        self.assertEqual(["codex", "claude"], [row["harness"] for row in ledger])

    def test_changed_sources_cannot_produce_complete_report(self) -> None:
        args = entry.build_parser().parse_args(self.args("--live"))
        with (
            mock.patch.object(
                review,
                "source_identity",
                side_effect=[{"sha256": "a"}, {"sha256": "b"}],
            ),
            mock.patch.object(review, "run_review", return_value={"status": "passed"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                1, review.review_main(args, harness="codex", opted_in=True)
            )
        self.assertFalse(json.loads(output.getvalue())["complete"])

    def test_changed_reviewer_version_invalidates_entire_pair(self) -> None:
        def probe(name: str, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                version="changed" if name == "claude" and kwargs else "initial",
                executable="/not-launched",
                status="unknown",
                blocks=False,
            )

        with (
            mock.patch.object(
                harness_compatibility, "check_harness", side_effect=probe
            ),
            mock.patch.object(review, "run_sequence", return_value={}),
        ):
            result = review.run_review(
                "codex", "claude", opted_in=True, deadline=30, keep=False
            )
        self.addCleanup(shutil.rmtree, result["artifacts"])
        self.assertEqual("failed", result["status"])
        self.assertEqual({}, result["verified_versions"])
        self.assertEqual([], result["checks"])
        self.assertIn("no pair is verified", result["detail"])

    def test_unavailable_reviewer_cannot_create_a_paid_workspace(self) -> None:
        def probe(name: str) -> SimpleNamespace:
            return SimpleNamespace(
                version="0.154.0",
                executable="/not-launched" if name == "codex" else None,
                status="unknown",
                blocks=False,
            )

        with (
            mock.patch.object(
                harness_compatibility, "check_harness", side_effect=probe
            ),
            mock.patch.object(
                review, "disposable_workspace", side_effect=AssertionError
            ),
        ):
            result = review.run_review(
                "codex", "claude", opted_in=True, deadline=30, keep=False
            )
        self.assertEqual("skipped", result["status"])
        self.assertEqual({}, result["verified_versions"])

    def install_fixtures(self) -> None:
        fixture = Path(__file__).parent / "fixtures/fake_review_harness.py"
        configured = {}
        for name in ("codex", "claude"):
            wrapper = self.root / name
            role = "reviewer" if name == "claude" else "implementation"
            wrapper.write_text(
                f"#!/bin/sh\nexport ASL_FIXTURE_ROLE={role}\n"
                f'exec {shlex.quote(sys.executable)} {shlex.quote(str(fixture))} "$@"\n'
            )
            wrapper.chmod(0o700)
            configured[name] = {"executable": str(wrapper)}
        config = self.root / "config/agent-session-loop/harnesses.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(configured))

    def test_review_repair_and_rereview_through_real_commands(self) -> None:
        self.install_fixtures()
        with (
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = entry.main(
                self.args(
                    "--live",
                    "--deadline",
                    "120",
                    "--keep-workspaces",
                    "--report",
                    str(self.root / "report.json"),
                )
            )
        report = json.loads(output.getvalue())
        result = report["pair"]
        if result["status"] == "passed":
            self.addCleanup(shutil.rmtree, result["artifacts"])
        self.assertEqual("passed", result["status"], result["detail"])
        self.assertEqual(0, code)
        self.assertEqual(report, json.loads((self.root / "report.json").read_text()))
        self.assertTrue(report["complete"])
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(
            {"codex": "0.154.0", "claude": "2.1.236"}, result["verified_versions"]
        )
        self.assertEqual(list(review.CHECKS), result["checks"])
        evidence = result["evidence"]
        self.assertFalse(evidence["seeded_failure"]["ok"])
        self.assertTrue(evidence["product_verification"]["ok"])
        self.assertEqual("all-clear", evidence["final_review"]["outcome"])
        ledger = [
            json.loads(row)
            for row in (Path(result["artifacts"]) / "harness-launches.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertLessEqual(len(ledger), review.MAX_HARNESS_LAUNCHES)
        self.assertEqual(2, sum(row["harness"] == "claude" for row in ledger))
        # A report must fail if an all-clear skips the finding or repair, if
        # a repair used the reviewer, or if durable closure/verification is absent.
        durable = evidence["review_sequence"]
        for mutation in (
            "skip-finding",
            "wrong-harness",
            "unverified",
            "no-closure",
            "active",
        ):
            changed = copy.deepcopy(durable)
            if mutation == "skip-finding":
                changed["turns"][1]["review_verdict"] = {"outcome": "all-clear"}
            elif mutation == "wrong-harness":
                changed["turns"][2]["harness"] = "claude"
            elif mutation == "unverified":
                changed["turns"][2]["verification"]["passed"] = False
            elif mutation == "no-closure":
                changed["review_cycles"][1]["closed_findings"] = []
            else:
                changed["final_run"]["active_turn"] = {"session_id": "3"}
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                review.verify_review_evidence(changed, "codex", "claude")

    def test_premature_all_clear_retains_failure_without_verifying_pair(self) -> None:
        self.check_rejected_fixture(
            "premature-all-clear", "expected independent review"
        )

    def test_replaced_verifier_cannot_pass_acceptance(self) -> None:
        self.check_rejected_fixture(
            "tamper-verifier", "changed frozen acceptance input"
        )

    def check_rejected_fixture(self, mode: str, expected: str) -> None:
        self.install_fixtures()
        with mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "ASL_REVIEW_FIXTURE_MODE": mode,
            },
        ):
            result = review.run_review(
                "codex", "claude", opted_in=True, deadline=120, keep=False
            )
        self.addCleanup(shutil.rmtree, result["artifacts"])
        self.assertEqual("failed", result["status"], result["detail"])
        self.assertEqual({}, result["verified_versions"])
        self.assertEqual([], result["checks"])
        self.assertIn(expected, result["detail"])
        root = Path(result["artifacts"])
        saved = json.loads((root / "acceptance.json").read_text())
        self.assertEqual("failed", saved["status"])
        self.assertTrue((root / "harness-launches.jsonl").is_file())
        self.assertTrue(list(root.glob("state/*/events.jsonl")))
        self.assertTrue(
            all(Path(call["stderr"]).is_file() for call in saved["commands"])
        )


if __name__ == "__main__":
    unittest.main()
