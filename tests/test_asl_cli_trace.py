"""Privacy-safe CLI invocation trace tests.

Tracing is disabled package-wide in ``tests/__init__.py``; every case here
re-enables it inside a temporary XDG state root so no test can touch the
developer's live traces.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import cli, cli_trace, config

ASL_TEST_PORTABLE = True

ALLOWED_START_FIELDS = frozenset(
    {
        "schema_version",
        "event",
        "invocation_id",
        "timestamp",
        "pid",
        "command_path",
        "options",
        "help_depth",
        "harness",
        "origin",
    }
)
ALLOWED_END_FIELDS = ALLOWED_START_FIELDS | {
    "exit_code",
    "outcome",
    "duration_ms",
    "exception_type",
}


def _handler(code: int):
    def handler(_args: argparse.Namespace) -> int:
        return code

    return handler


class CliTraceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        patcher = mock.patch.dict(
            os.environ,
            {
                cli_trace.TRACE_ENV_VAR: "1",
                "XDG_STATE_HOME": str(self.tmp_path / "state"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The origin marker is inherited, so a suite launched by autopilot or
        # the workbench would otherwise see its launcher's origin instead of
        # `direct`. Drop it for the same reason the trace root is redirected:
        # these cases assert what the CLI records, not who ran the tests.
        # `patch.dict` restores the whole mapping, so the removal is undone too.
        os.environ.pop(cli_trace.ORIGIN_ENV_VAR, None)
        self.root = config.default_state_root()

    def corpus(self, root: Path | None = None) -> cli_trace.TraceCorpus:
        return cli_trace.read_corpus(self.root if root is None else root)

    def records(self, root: Path | None = None) -> list[dict[str, Any]]:
        return self.corpus(root).records

    def run_main(self, argv: list[str], handler) -> int:
        with mock.patch.object(cli, "resolve_handler", return_value=handler):
            return cli.main(argv)

    def today_file(self, root: Path | None = None) -> Path:
        base = self.root if root is None else root
        return base / cli_trace.trace_file_name(datetime.now(UTC).date())

    def assert_pair(self, records: list[dict[str, Any]]) -> tuple[dict, dict]:
        self.assertEqual(2, len(records), records)
        start, end = records
        self.assertEqual(cli_trace.EVENT_START, start["event"])
        self.assertEqual(cli_trace.EVENT_END, end["event"])
        self.assertEqual(start["invocation_id"], end["invocation_id"])
        self.assertLessEqual(set(start), ALLOWED_START_FIELDS)
        self.assertLessEqual(set(end), ALLOWED_END_FIELDS)
        return start, end


class SuccessfulInvocationTests(CliTraceTestCase):
    def test_successful_invocation_writes_a_start_and_end_pair(self) -> None:
        self.assertEqual(0, self.run_main(["tail", "/tmp/plan"], _handler(0)))

        start, end = self.assert_pair(self.records())

        self.assertEqual(cli_trace.SCHEMA_VERSION, start["schema_version"])
        self.assertEqual(os.getpid(), start["pid"])
        self.assertEqual(["tail"], start["command_path"])
        self.assertEqual([], start["options"])
        self.assertIsNone(start["help_depth"])
        self.assertEqual(cli_trace.ORIGIN_DIRECT, start["origin"])
        self.assertEqual(0, end["exit_code"])
        self.assertEqual(cli_trace.OUTCOME_OK, end["outcome"])
        self.assertIsNone(end["exception_type"])
        self.assertGreaterEqual(end["duration_ms"], 0.0)

    def test_recognized_option_names_are_recorded_without_their_values(self) -> None:
        self.run_main(
            ["tail", "/tmp/plan", "--no-follow", "--lines", "42"], _handler(0)
        )

        start, _ = self.assert_pair(self.records())

        self.assertEqual(["--lines", "--no-follow"], start["options"])

    def test_nested_command_path_records_every_recognized_level(self) -> None:
        self.run_main(["bridge", "serve", "--plan-root", "/tmp/plans"], _handler(0))

        start, _ = self.assert_pair(self.records())

        self.assertEqual(["bridge", "serve"], start["command_path"])
        self.assertEqual(["--plan-root"], start["options"])

    def test_trace_file_is_daily_and_owner_only(self) -> None:
        self.run_main(["tail", "/tmp/plan"], _handler(0))

        path = self.today_file()

        self.assertTrue(path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_successful_parse_traces_under_its_effective_state_dir(self) -> None:
        override = self.tmp_path / "override"

        self.run_main(["tail", "/tmp/plan", "--state-dir", str(override)], _handler(0))

        self.assertEqual([], self.records())
        start, _ = self.assert_pair(self.records(override))
        self.assertEqual(["tail"], start["command_path"])


class FailureOutcomeTests(CliTraceTestCase):
    def test_handler_failure_records_a_failed_outcome(self) -> None:
        self.assertEqual(3, self.run_main(["tail", "/tmp/plan"], _handler(3)))

        _, end = self.assert_pair(self.records())

        self.assertEqual(3, end["exit_code"])
        self.assertEqual(cli_trace.OUTCOME_FAILED, end["outcome"])

    def test_usage_failure_records_a_pair_under_the_default_state_root(self) -> None:
        override = self.tmp_path / "override"

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["tail", "/tmp/plan", "--state-dir", str(override), "--bogus"])

        self.assertEqual(2, raised.exception.code)
        # Parsing died before a namespace existed, so there is no effective
        # `--state-dir` to honor.
        self.assertEqual([], self.records(override))
        start, end = self.assert_pair(self.records())
        self.assertEqual(["tail"], start["command_path"])
        self.assertEqual(cli_trace.OUTCOME_USAGE, end["outcome"])
        self.assertEqual(2, end["exit_code"])
        self.assertIsNone(start["help_depth"])

    def test_interrupted_invocation_records_the_interrupted_exit(self) -> None:
        def interrupted(_args: argparse.Namespace) -> int:
            raise KeyboardInterrupt

        self.assertEqual(130, self.run_main(["tail", "/tmp/plan"], interrupted))

        _, end = self.assert_pair(self.records())

        self.assertEqual(130, end["exit_code"])
        self.assertEqual(cli_trace.OUTCOME_INTERRUPTED, end["outcome"])
        self.assertIsNone(end["exception_type"])

    def test_exception_records_only_the_class_name(self) -> None:
        def exploding(_args: argparse.Namespace) -> int:
            raise RuntimeError("token sk-live-should-never-be-traced")

        with self.assertRaises(RuntimeError):
            self.run_main(["tail", "/tmp/plan"], exploding)

        _, end = self.assert_pair(self.records())

        self.assertEqual("RuntimeError", end["exception_type"])
        self.assertEqual(cli_trace.OUTCOME_EXCEPTION, end["outcome"])
        self.assertIsNone(end["exit_code"])
        self.assertNotIn("sk-live", self.today_file().read_text(encoding="utf-8"))

    def test_handler_system_exit_is_traced_and_still_propagates(self) -> None:
        def exiting(_args: argparse.Namespace) -> int:
            raise SystemExit(4)

        with self.assertRaises(SystemExit) as raised:
            self.run_main(["tail", "/tmp/plan"], exiting)

        self.assertEqual(4, raised.exception.code)
        _, end = self.assert_pair(self.records())
        self.assertEqual(4, end["exit_code"])
        self.assertEqual(cli_trace.OUTCOME_FAILED, end["outcome"])


class HelpDepthTests(CliTraceTestCase):
    def _help_records(self, argv: list[str]) -> tuple[dict, dict]:
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(argv)
        self.assertEqual(0, raised.exception.code)
        return self.assert_pair(self.records())

    def test_root_help_depth(self) -> None:
        start, end = self._help_records(["--help"])

        self.assertEqual(cli_trace.HELP_ROOT, start["help_depth"])
        self.assertEqual([], start["command_path"])
        self.assertEqual(cli_trace.OUTCOME_HELP, end["outcome"])
        self.assertEqual(0, end["exit_code"])

    def test_group_help_depth(self) -> None:
        start, _ = self._help_records(["bridge", "--help"])

        self.assertEqual(cli_trace.HELP_GROUP, start["help_depth"])
        self.assertEqual(["bridge"], start["command_path"])

    def test_leaf_help_depth(self) -> None:
        start, _ = self._help_records(["tail", "--help"])

        self.assertEqual(cli_trace.HELP_LEAF, start["help_depth"])
        self.assertEqual(["tail"], start["command_path"])


class RedactionTests(CliTraceTestCase):
    def test_no_value_path_or_unknown_token_reaches_the_trace(self) -> None:
        secret_root = self.tmp_path / "s3cret-state"

        self.run_main(
            ["tail", "/Users/someone/secret-plan", "--state-dir", str(secret_root)],
            _handler(0),
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["tail", "/Users/someone/secret-plan", "--not-an-option"])

        traced = self.today_file(secret_root).read_text(encoding="utf-8")
        usage = self.today_file().read_text(encoding="utf-8")

        for written in (traced, usage):
            self.assertNotIn("/Users/someone/secret-plan", written)
            self.assertNotIn("s3cret-state", written)
            self.assertNotIn("--not-an-option", written)
            self.assertIn('"command_path":["tail"]', written)
        self.assertIn('"options":["--state-dir"]', traced)

    def test_option_value_that_spells_a_subcommand_is_not_promoted(self) -> None:
        parser = argparse.ArgumentParser(prog="asl")
        parser.add_argument("--state-dir")
        sub = parser.add_subparsers(dest="command")
        sub.add_parser("metrics")
        sub.add_parser("tail")

        shape = cli_trace.classify(parser, ["--state-dir", "metrics", "tail"])

        self.assertEqual(("tail",), shape.command_path)
        self.assertEqual(("--state-dir",), shape.options)

    def test_classification_ignores_unknown_and_post_separator_tokens(self) -> None:
        parser = argparse.ArgumentParser(prog="asl")
        sub = parser.add_subparsers(dest="command")
        leaf = sub.add_parser("run")
        leaf.add_argument("--json", action="store_true")
        leaf.add_argument("--harness")

        shape = cli_trace.classify(
            parser, ["run", "--unknown", "--harness=codex", "--", "--json"]
        )

        self.assertEqual(("run",), shape.command_path)
        self.assertEqual(("--harness",), shape.options)


class HarnessAndOriginTests(CliTraceTestCase):
    def test_allowlisted_origin_is_recorded_and_unknown_becomes_other(self) -> None:
        self.assertEqual(
            "autopilot",
            cli_trace.origin({cli_trace.ORIGIN_ENV_VAR: "autopilot"}),
        )
        self.assertEqual(
            cli_trace.ORIGIN_OTHER,
            cli_trace.origin({cli_trace.ORIGIN_ENV_VAR: "/opt/some/path"}),
        )
        self.assertEqual(cli_trace.ORIGIN_DIRECT, cli_trace.origin({}))

    def test_start_records_the_detected_harness_and_origin(self) -> None:
        parser = cli.build_parser(prog="asl")

        invocation = cli_trace.start(
            parser,
            ["tail", "/tmp/plan"],
            env={"CLAUDECODE": "1", cli_trace.ORIGIN_ENV_VAR: "workbench"},
        )
        invocation.finish(exit_code=0, outcome=cli_trace.OUTCOME_OK)

        start, _ = self.assert_pair(self.records())

        self.assertEqual("claude", start["harness"])
        self.assertEqual("workbench", start["origin"])

    def test_ambiguous_harness_detection_records_null(self) -> None:
        parser = cli.build_parser(prog="asl")

        cli_trace.start(
            parser, ["tail", "/tmp/plan"], env={"CLAUDECODE": "1", "CODEX_HOME": "/x"}
        )

        self.assertIsNone(self.records()[0]["harness"])

    def test_every_named_origin_constant_is_allowlisted(self) -> None:
        for name in (
            cli_trace.ORIGIN_DIRECT,
            cli_trace.ORIGIN_AUTOPILOT,
            cli_trace.ORIGIN_WORKBENCH,
            cli_trace.ORIGIN_INSTALL_VERIFY,
        ):
            self.assertIn(name, cli_trace.ORIGIN_NAMES)
        self.assertNotIn(cli_trace.ORIGIN_OTHER, cli_trace.ORIGIN_NAMES)

    def test_child_env_marks_the_origin_and_preserves_every_other_entry(self) -> None:
        child = cli_trace.child_env(
            {"PATH": "/bin", "HOME": "/home/x"}, cli_trace.ORIGIN_WORKBENCH
        )

        self.assertEqual("workbench", child[cli_trace.ORIGIN_ENV_VAR])
        self.assertEqual("/bin", child["PATH"])
        self.assertEqual("/home/x", child["HOME"])

    def test_child_env_overrides_an_inherited_or_supplied_origin(self) -> None:
        base = {cli_trace.ORIGIN_ENV_VAR: cli_trace.ORIGIN_DIRECT}

        child = cli_trace.child_env(base, cli_trace.ORIGIN_AUTOPILOT)

        self.assertEqual("autopilot", child[cli_trace.ORIGIN_ENV_VAR])
        self.assertEqual(cli_trace.ORIGIN_DIRECT, base[cli_trace.ORIGIN_ENV_VAR])

    def test_child_env_records_the_launcher_origin_end_to_end(self) -> None:
        parser = cli.build_parser(prog="asl")
        child = cli_trace.child_env(
            {cli_trace.ORIGIN_ENV_VAR: "direct", cli_trace.TRACE_ENV_VAR: "1"},
            cli_trace.ORIGIN_INSTALL_VERIFY,
        )

        cli_trace.start(parser, ["status", "/tmp/plan"], env=child)

        self.assertEqual("install-verify", self.records()[0]["origin"])


class CurrentInvocationTests(CliTraceTestCase):
    def test_the_current_id_matches_the_record_this_process_wrote(self) -> None:
        parser = cli.build_parser(prog="asl")

        invocation = cli_trace.start(parser, ["tail", "/tmp/plan"])

        self.assertEqual(invocation.invocation_id, cli_trace.current_invocation_id())
        self.assertEqual(
            self.records()[0]["invocation_id"], cli_trace.current_invocation_id()
        )

    def test_an_untraced_process_leaves_the_current_id_unchanged(self) -> None:
        parser = cli.build_parser(prog="asl")
        cli_trace.start(parser, ["tail", "/tmp/plan"])
        traced_id = cli_trace.current_invocation_id()

        with mock.patch.dict(os.environ, {cli_trace.TRACE_ENV_VAR: "0"}):
            disabled = cli_trace.start(parser, ["tail", "/tmp/plan"])

        self.assertEqual("", disabled.invocation_id)
        self.assertEqual(traced_id, cli_trace.current_invocation_id())


class OptOutTests(CliTraceTestCase):
    def test_opt_out_writes_nothing_and_preserves_the_exit_code(self) -> None:
        with mock.patch.dict(os.environ, {cli_trace.TRACE_ENV_VAR: "0"}):
            self.assertEqual(7, self.run_main(["tail", "/tmp/plan"], _handler(7)))

        self.assertFalse(self.root.exists())

    def test_opt_out_also_suppresses_help_and_usage_pairs(self) -> None:
        with (
            mock.patch.dict(os.environ, {cli_trace.TRACE_ENV_VAR: "0"}),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cli.main(["--help"])

        self.assertFalse(self.root.exists())

    def test_only_the_documented_falsy_values_disable_tracing(self) -> None:
        for value in ("0", "false", "NO", " off "):
            with self.subTest(value=value):
                self.assertFalse(cli_trace.enabled({cli_trace.TRACE_ENV_VAR: value}))
        for value in ("1", "true", "", "yes-please"):
            with self.subTest(value=value):
                self.assertTrue(cli_trace.enabled({cli_trace.TRACE_ENV_VAR: value}))
        self.assertTrue(cli_trace.enabled({}))


class DurabilityTests(CliTraceTestCase):
    def test_concurrent_writers_never_interleave_a_record(self) -> None:
        parser = cli.build_parser(prog="asl")

        def write_pair(_index: int) -> None:
            invocation = cli_trace.start(parser, ["tail", "/tmp/plan"])
            invocation.finish(exit_code=0, outcome=cli_trace.OUTCOME_OK)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_pair, range(24)))

        lines = self.today_file().read_text(encoding="utf-8").splitlines()

        self.assertEqual(48, len(lines))
        for line in lines:
            json.loads(line)
        self.assertEqual(
            24, len({record["invocation_id"] for record in self.records()})
        )

    def test_an_unmatched_start_stays_readable_and_does_not_block_later_records(
        self,
    ) -> None:
        parser = cli.build_parser(prog="asl")
        cli_trace.start(parser, ["tail", "/tmp/plan"])

        self.run_main(["metrics"], _handler(0))

        records = self.records()

        self.assertEqual(
            [cli_trace.EVENT_START, cli_trace.EVENT_START, cli_trace.EVENT_END],
            [record["event"] for record in records],
        )
        self.assertEqual(2, len({record["invocation_id"] for record in records}))

    def test_malformed_lines_are_skipped_and_never_rewritten(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.today_file()
        path.write_text('not json\n{"event": "start"\n[1, 2, 3]\n\n', encoding="utf-8")

        self.run_main(["tail", "/tmp/plan"], _handler(0))

        corpus = self.corpus()
        self.assertEqual(2, len(corpus.records))
        self.assertEqual(3, corpus.malformed_lines)
        self.assertTrue(path.read_text(encoding="utf-8").startswith("not json\n"))

    def test_a_write_failure_never_changes_the_command_outcome(self) -> None:
        with mock.patch.object(cli_trace, "_append", side_effect=OSError("read-only")):
            self.assertEqual(5, self.run_main(["tail", "/tmp/plan"], _handler(5)))

        self.assertEqual([], self.records())

    def test_a_classification_failure_never_changes_the_command_outcome(self) -> None:
        with mock.patch.object(cli_trace, "classify", side_effect=RuntimeError("boom")):
            self.assertEqual(0, self.run_main(["tail", "/tmp/plan"], _handler(0)))

        self.assertEqual([], self.records())

    def test_an_unreadable_trace_file_is_skipped_by_the_reader(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.today_file().write_bytes(b"\xff\xfe not utf-8\n")

        self.assertEqual([], self.records())


class RetentionTests(CliTraceTestCase):
    def test_files_older_than_the_retention_window_are_pruned(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date()
        expired = self.root / cli_trace.trace_file_name(
            today - timedelta(days=cli_trace.RETENTION_DAYS + 1)
        )
        kept = self.root / cli_trace.trace_file_name(today - timedelta(days=1))
        unrelated = self.root / f"{cli_trace.FILE_PREFIX}not-a-date.jsonl"
        for path in (expired, kept, unrelated):
            path.write_text("", encoding="utf-8")

        self.run_main(["tail", "/tmp/plan"], _handler(0))

        self.assertFalse(expired.exists())
        self.assertTrue(kept.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.today_file().exists())


if __name__ == "__main__":
    unittest.main()
