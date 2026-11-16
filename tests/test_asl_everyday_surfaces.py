"""The surfaces a person meets every day: home, names, summaries, signals.

These pin the behavior that makes ``asl`` answer in the reader's terms: a bare
invocation is a home screen, a plan is reachable by a prefix or a title word,
a run ends on a summary and a pause on the failed check, a refusal's reason
reaches the trace, and the signals that used to fire constantly now say
something or stay quiet.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    assurance_cli_common,
    cli,
    cli_trace,
    cli_trace_notes,
    config,
    desktop_notify,
    events,
    formatting,
    harness_compatibility,
    harness_local_evidence,
    home,
    metrics_invocations,
    metrics_render,
    plans_files,
    plans_metadata,
    preflight_environment,
    run_inputs,
    run_outcome_summary,
    state,
    state_plan_refs,
    state_status_text,
    tail_event_messages,
)
from tests.support import write_plan

ASL_TEST_PORTABLE = True

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _titled_plan(plan_dir: Path, title: str, statuses: tuple[str, ...]) -> Path:
    write_plan(plan_dir, statuses)
    (plan_dir / "manifest.md").write_text(f"# {title}\n", encoding="utf-8")
    return plans_files.canonicalize_path(plan_dir)


def _register(state_dir: Path, plan_dir: Path, **fields: Any) -> Path:
    """Record ``plan_dir`` (and any run fields) as a plan's ``state.json``."""
    canonical = plans_files.canonicalize_path(plan_dir)
    entry = state_dir / plans_files.path_sha256(canonical)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / events.STATE_FILE_NAME).write_text(
        json.dumps({"plan_dir": str(canonical), **fields}, sort_keys=True),
        encoding="utf-8",
    )
    return entry


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = plans_files.canonicalize_path(tmp.name)
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        # Discovery must see this case's plans only, never the developer's.
        patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.tmp / "home"),
                "XDG_DATA_HOME": str(self.tmp / "data"),
                "XDG_CONFIG_HOME": str(self.tmp / "config"),
                "XDG_STATE_HOME": str(self.tmp / "xdg-state"),
                "TMPDIR": str(self.tmp / "tmp"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AGENT_PLANS_DIR", None)


class ShorthandPlanReferenceTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.alpha = _titled_plan(
            self.tmp / "plans" / "aaaa11112222-333344445555",
            "Polish the reports",
            ("pending",),
        )
        self.beta = _titled_plan(
            self.tmp / "plans" / "bbbb11112222-333344445555",
            "Polish the dashboard",
            ("pending",),
        )
        _register(self.state_dir, self.alpha)
        _register(self.state_dir, self.beta)

    def _resolve(self, raw: str) -> Path:
        with contextlib.redirect_stderr(io.StringIO()) as self.stderr:
            return state_plan_refs.resolve_plan_reference(raw, self.state_dir)

    def test_a_unique_directory_prefix_resolves_and_is_announced(self) -> None:
        self.assertEqual(self.alpha, self._resolve("aaaa"))
        self.assertIn(
            "'aaaa' resolved to plan aaaa11112222-333344445555", self.stderr.getvalue()
        )
        self.assertIn("Polish the reports", self.stderr.getvalue())

    def test_a_title_fragment_resolves_case_insensitively(self) -> None:
        self.assertEqual(self.beta, self._resolve("DASHBOARD"))

    def test_an_ambiguous_shorthand_names_each_plan_by_title(self) -> None:
        with self.assertRaises(state.AmbiguousPlanError) as caught:
            self._resolve("polish")
        text = state_plan_refs.shorthand_ambiguity_text(caught.exception)
        assert text is not None
        self.assertIn("'polish' matches 2 plans", text)
        self.assertIn("aaaa11112222-333344445555  Polish the reports", text)
        self.assertIn("bbbb11112222-333344445555  Polish the dashboard", text)

    def test_an_exact_label_still_wins_without_an_announcement(self) -> None:
        self.assertEqual(self.alpha, self._resolve(self.alpha.name))
        self.assertEqual("", self.stderr.getvalue())

    def test_nothing_matching_returns_the_argument_unresolved(self) -> None:
        resolved = self._resolve("zzzz-no-such-plan")
        self.assertFalse(resolved.exists())

    def test_default_plan_dirs_are_ignored_for_a_custom_state_root(self) -> None:
        stray = _titled_plan(
            self.tmp / "data" / "agent-plans" / "cccc11112222-333344445555",
            "Unrelated work",
            ("pending",),
        )
        self.assertNotEqual(stray, self._resolve("cccc"))
        self.assertFalse(self._resolve("cccc").exists())


class HomeScreenTests(TempDirCase):
    def _home(self, *argv: str) -> tuple[int, str]:
        args = argparse.Namespace(state_dir=str(self.state_dir), json="--json" in argv)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = home.home_from_args(args, now=NOW)
        return code, out.getvalue()

    def test_an_empty_corpus_says_so_plainly(self) -> None:
        code, text = self._home()
        self.assertEqual(0, code)
        self.assertIn("Nothing needs you.", text)
        self.assertIn("Nothing is running.", text)

    def test_a_recent_finished_plan_is_listed_by_title(self) -> None:
        plan = _titled_plan(
            self.tmp / "plans" / "dddd11112222-333344445555",
            "Ship the thing",
            ("complete",),
        )
        finished = (NOW - timedelta(hours=2)).isoformat()
        _register(
            self.state_dir,
            plan,
            ok=True,
            reason="all-complete",
            run_id="r1",
            run_started_at=finished,
            run_finished_at=finished,
        )
        code, text = self._home()
        self.assertEqual(0, code)
        self.assertIn("Finished lately (1):", text)
        self.assertIn("dddd11112222  Ship the thing", text)
        self.assertIn("1/1 sessions · 2h ago", text)

    def test_json_carries_the_same_sections(self) -> None:
        code, text = self._home("--json")
        payload = json.loads(text)
        self.assertEqual(0, code)
        self.assertEqual("home", payload["command"])
        for key in (
            "running",
            "needs_you",
            "needs_you_older",
            "ready",
            "finished",
            "finished_more",
            "assurance_open",
        ):
            self.assertIn(key, payload)

    def test_a_bare_invocation_runs_the_home_command(self) -> None:
        seen: list[argparse.Namespace] = []

        def handler(args: argparse.Namespace) -> int:
            seen.append(args)
            return 0

        with mock.patch.object(cli, "resolve_handler", return_value=handler):
            self.assertEqual(0, cli.main([]))
        self.assertEqual("home", seen[0].command)

    def test_root_help_leads_with_the_everyday_commands(self) -> None:
        text = cli.build_parser(prog="asl").format_help()
        self.assertIn("everyday commands:", text)
        self.assertLess(
            text.index("everyday commands:"), text.index("supervisor-guide")
        )


class RelativeAgeTests(unittest.TestCase):
    def test_ages_are_coarse_and_never_negative(self) -> None:
        cases = {
            timedelta(seconds=5): "just now",
            timedelta(minutes=12): "12m ago",
            timedelta(hours=3, minutes=59): "3h ago",
            timedelta(days=4): "4d ago",
            timedelta(seconds=-30): "just now",
        }
        for delta, expected in cases.items():
            with self.subTest(delta=delta):
                self.assertEqual(
                    expected, formatting.format_relative_age(NOW - delta, now=NOW)
                )
        self.assertEqual("", formatting.format_relative_age(None, now=NOW))


class StatusHeadlineTests(unittest.TestCase):
    BASE: dict[str, Any] = {
        "plan_title": "Ship the thing",
        "plan_dir": "/plans/dddd11112222-333344445555",
        "sessions": [{"status": "complete"}, {"status": "pending"}],
        "next_pending": 2,
        "next_pending_id": "2",
        "runner_state": {},
    }

    def _headline(self, **fields: Any) -> list[str]:
        return state_status_text.status_headline({**self.BASE, **fields})

    def test_an_idle_plan_names_its_next_session(self) -> None:
        self.assertEqual(
            [
                "Ship the thing",
                "  1 of 2 sessions complete · not running; next is session 2",
                "  Nothing needs you.",
            ],
            self._headline(),
        )

    def test_an_active_verification_names_the_check(self) -> None:
        lines = self._headline(
            active_verification={
                "session_id": "2",
                "current_check_index": 1,
                "check_count": 3,
                "current_check_name": "e2e",
                "elapsed_seconds": 75,
            }
        )
        self.assertIn("verifying session 2 (check 2/3 e2e, 1m15s)", lines[1])
        self.assertEqual("  Nothing needs you.", lines[2])

    def test_a_pause_says_what_it_is_waiting_for(self) -> None:
        lines = self._headline(
            runner_state={
                "paused": True,
                "reason": "blocked",
                "pause_message": "the worker asked which API to keep",
                "paused_at": "2026-11-15T10:00:00+00:00",
            }
        )
        self.assertIn("paused (blocked)", lines[1])
        self.assertEqual("  Needs you: the worker asked which API to keep", lines[2])

    def test_an_open_question_points_at_the_questions_command(self) -> None:
        lines = self._headline(waiting_for_reply={"session_id": "2"})
        self.assertIn("questions dddd11112222-333344445555", lines[2])


class RunSummaryTests(TempDirCase):
    def test_summary_names_the_plan_and_what_the_run_amounted_to(self) -> None:
        plan = _titled_plan(self.tmp / "plan", "Ship the thing", ("complete",))
        line = run_outcome_summary.summary_line(
            {
                "plan_dir": str(plan),
                "final_snapshot": {"counts": {"complete": 3, "pending": 1}},
                "duration_seconds": 754,
                "commits": [{"success": True}, {"success": True}, {"success": False}],
                "review_completed": True,
                "review_verdict": {"status_line": "Status: All clear, no findings"},
            }
        )
        self.assertEqual(
            "Summary: Ship the thing — 3 of 4 sessions complete; ran 12m34s; "
            "2 commits; review: All clear, no findings",
            line,
        )

    def test_a_failed_check_is_named_with_the_end_of_its_output(self) -> None:
        stdout = self.tmp / "check-stdout.gz"
        stdout.write_bytes(
            gzip.compress(
                b"collected 3 items\n\nFAILED test_x - assert 1 == 2\n1 failed\n"
            )
        )
        lines = run_outcome_summary.failed_check_lines(
            {
                "pause_details": {
                    "checks": [
                        {"name": "lint", "status": "pass"},
                        {
                            "name": "unit",
                            "status": "fail",
                            "detail": "command exited 1: pytest",
                            "stdout_artifact_path": str(stdout),
                            "stderr_artifact_path": str(self.tmp / "missing.gz"),
                        },
                    ]
                }
            }
        )
        self.assertEqual("Failed check unit: command exited 1: pytest", lines[0])
        self.assertIn("  out | FAILED test_x - assert 1 == 2", lines)
        self.assertIn("  out | 1 failed", lines)
        self.assertIn(f"  full out: {stdout}", lines)
        self.assertFalse(any(line.startswith("  err |") for line in lines))

    def test_a_pause_without_checks_adds_nothing(self) -> None:
        self.assertEqual(
            [], run_outcome_summary.failed_check_lines({"pause_details": {}})
        )


class TraceReasonTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {cli_trace.TRACE_ENV_VAR: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(cli_trace.ORIGIN_ENV_VAR, None)
        self.addCleanup(cli_trace_notes.reset)

    def _end_record(self, argv: list[str], handler: Any) -> dict[str, Any]:
        with mock.patch.object(cli, "resolve_handler", return_value=handler):
            cli.main(argv)
        records = cli_trace.read_corpus(config.default_state_root()).records
        return records[-1]

    def test_only_a_slug_is_accepted_as_a_reason(self) -> None:
        cli_trace_notes.reset()
        for rejected in ("Plan not found: /secret/path", "", None, "UPPER", "a b"):
            cli_trace_notes.note_reason(rejected)
            self.assertIsNone(cli_trace_notes.noted_reason(), rejected)
        cli_trace_notes.note_reason("plan-not-found")
        self.assertEqual("plan-not-found", cli_trace_notes.noted_reason())

    def test_a_failure_records_the_reason_the_command_noted(self) -> None:
        def handler(_args: argparse.Namespace) -> int:
            cli_trace_notes.note_reason("plan-not-found")
            return 1

        end = self._end_record(["tail", "/tmp/plan"], handler)
        self.assertEqual(cli_trace.OUTCOME_FAILED, end["outcome"])
        self.assertEqual("plan-not-found", end["reason"])

    def test_a_pause_is_recorded_as_paused_with_the_same_exit_code(self) -> None:
        def handler(_args: argparse.Namespace) -> int:
            cli_trace_notes.note_reason("verify-failed", paused=True)
            return 1

        end = self._end_record(["tail", "/tmp/plan"], handler)
        self.assertEqual(cli_trace.OUTCOME_PAUSED, end["outcome"])
        self.assertEqual(1, end["exit_code"])
        self.assertEqual("verify-failed", end["reason"])

    def test_a_success_records_no_reason_even_if_one_was_noted(self) -> None:
        def handler(_args: argparse.Namespace) -> int:
            cli_trace_notes.note_reason("plan-not-found")
            return 0

        end = self._end_record(["tail", "/tmp/plan"], handler)
        self.assertEqual(cli_trace.OUTCOME_OK, end["outcome"])
        self.assertIsNone(end["reason"])

    def test_a_note_does_not_leak_into_the_next_invocation(self) -> None:
        def noting(_args: argparse.Namespace) -> int:
            cli_trace_notes.note_reason("plan-not-found")
            return 1

        self._end_record(["tail", "/tmp/plan"], noting)
        end = self._end_record(["tail", "/tmp/plan"], lambda _args: 1)
        # Not the previous command's reason, and not silence either: a
        # failure that explained nothing is recorded as exactly that.
        self.assertEqual(cli_trace.REASON_UNSPECIFIED, end["reason"])


class MetricsPauseAndReasonTests(TempDirCase):
    def _write(self, rows: list[tuple[str, str, str | None]]) -> None:
        lines = []
        for index, (command, outcome, reason) in enumerate(rows):
            base = {
                "schema_version": 1,
                "invocation_id": f"id-{index}",
                "pid": 1,
                "command_path": [command],
                "options": [],
                "help_depth": None,
                "harness": "codex",
                "origin": "direct",
            }
            stamp = (NOW - timedelta(minutes=index + 1)).isoformat()
            lines.append(json.dumps({**base, "event": "start", "timestamp": stamp}))
            lines.append(
                json.dumps(
                    {
                        **base,
                        "event": "end",
                        "timestamp": stamp,
                        "exit_code": 0 if outcome == "ok" else 1,
                        "outcome": outcome,
                        "reason": reason,
                        "duration_ms": 10.0,
                        "exception_type": None,
                        "exception_site": None,
                    }
                )
            )
        path = self.state_dir / cli_trace.trace_file_name(NOW.date())
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_pauses_are_counted_apart_and_reasons_are_ranked(self) -> None:
        self._write(
            [
                ("run", "paused", "verify-failed"),
                ("run", "paused", "verify-failed"),
                ("run", "failed", "input-error"),
                ("preflight", "failed", "env-git"),
                ("preflight", "failed", "env-git"),
                ("preflight", "failed", "env-git"),
                ("status", "ok", None),
            ]
        )
        section = metrics_invocations.collect_cli_invocations(
            self.state_dir, cutoff=NOW - timedelta(days=1)
        )
        self.assertEqual(2, section["pauses"])
        self.assertEqual(4, section["nonzero_exits"])
        self.assertEqual(
            [
                {"command": "preflight", "reason": "env-git", "count": 3},
                {"command": "run", "reason": "verify-failed", "count": 2},
                {"command": "run", "reason": "input-error", "count": 1},
            ],
            section["failure_reasons"],
        )
        # A pause is the loop working as designed, so it is not a failure row.
        self.assertEqual(
            {"failed"}, {entry["outcome"] for entry in section["recent_failures"]}
        )
        self.assertEqual("env-git", section["recent_failures"][-1]["reason"])

    def test_long_durations_read_as_time_not_as_milliseconds(self) -> None:
        self.assertEqual("316 ms", metrics_render._format_duration_ms(316.4))
        self.assertEqual("9m10s", metrics_render._format_duration_ms(550_327))
        self.assertEqual("n/a", metrics_render._format_duration_ms(None))


class AssuranceNextStepTests(unittest.TestCase):
    def _row(self, issue_id: str, count: int, state: str = "open") -> dict[str, Any]:
        return {"issue_id": issue_id, "occurrence_count": count, "state": state}

    def test_the_most_seen_open_issue_is_where_to_start(self) -> None:
        lines = assurance_cli_common._next_step_lines(
            [
                self._row("ASI-1", 2),
                self._row("ASI-2", 49),
                self._row("ASI-3", 80, state="resolved"),
            ]
        )
        self.assertEqual(
            "Start with ASI-2 (seen 49 time(s)): `asl assurance show ASI-2`", lines[0]
        )
        for verb in ("resolve", "dismiss", "accept-risk"):
            self.assertIn(f"`asl assurance {verb} ASI-2`", lines[1])

    def test_a_listing_with_nothing_open_adds_no_next_step(self) -> None:
        self.assertEqual(
            [],
            assurance_cli_common._next_step_lines(
                [self._row("ASI-3", 80, state="resolved")]
            ),
        )
        self.assertEqual([], assurance_cli_common._next_step_lines([]))

    def test_an_age_is_added_only_for_a_usable_timestamp(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=3, minutes=1)).isoformat()
        self.assertEqual(" (3h ago)", assurance_cli_common._age(recent, " ({})"))
        self.assertEqual("", assurance_cli_common._age(None, " ({})"))
        self.assertEqual("", assurance_cli_common._age("not a time", " ({})"))


class StatusLocalTimeTests(unittest.TestCase):
    def test_a_stored_utc_moment_is_printed_on_the_local_clock(self) -> None:
        stamp = "2026-06-19T12:00:00+00:00"
        expected = formatting.format_local_timestamp(datetime.fromisoformat(stamp))
        self.assertEqual(expected, state_status_text._when(stamp))
        self.assertEqual(expected, state_status_text._when("2026-06-19T12:00:00Z"))

    def test_a_missing_or_unparseable_moment_is_not_invented(self) -> None:
        self.assertEqual("unknown", state_status_text._when(None))
        self.assertEqual("none", state_status_text._when("", "none"))
        self.assertEqual("soon", state_status_text._when("soon"))


class TextModeReasonTests(TempDirCase):
    """A text-mode refusal reaches the trace with the reason JSON would give."""

    def _reason_after(self, argv: list[str]) -> str | None:
        cli_trace_notes.reset()
        self.addCleanup(cli_trace_notes.reset)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(argv)
        self.assertNotEqual(0, code)
        return cli_trace_notes.noted_reason()

    def test_commands_note_their_reason_without_json(self) -> None:
        missing = str(self.tmp / "no-such-plan")
        state_dir = ["--state-dir", str(self.state_dir)]
        cases = {
            "plan-not-found": [
                ["status", missing, *state_dir],
                ["tail", missing, "--no-follow", *state_dir],
                ["progress", missing],
            ],
            "input-error": [
                ["sessions", "--limit", "-1", *state_dir],
                ["plans", "--stale-days", "-1", *state_dir],
            ],
        }
        for expected, commands in cases.items():
            for argv in commands:
                with self.subTest(argv=argv[0]):
                    self.assertEqual(expected, self._reason_after(argv))


class DirtyTreeDetailTests(unittest.TestCase):
    def test_the_dirty_paths_are_named_and_the_rest_counted(self) -> None:
        porcelain = "".join(f" M src/file{index}.py\n" for index in range(7))
        self.assertEqual(
            "commit-mode: per-session; dirty: src/file0.py, src/file1.py, "
            "src/file2.py, src/file3.py, src/file4.py (+2 more)",
            preflight_environment._dirty_detail("per-session", porcelain),
        )

    def test_an_empty_status_adds_nothing(self) -> None:
        self.assertEqual(
            "commit-mode: at-end", preflight_environment._dirty_detail("at-end", "")
        )


class PreparedRootTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan = self.tmp / "plan"
        write_plan(self.plan, ("pending",))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()
        plans_metadata.save_plan_metadata(self.plan, self.repo)

    def _bound(self, cwd_arg: str, cwd: Path) -> Path | None:
        args = argparse.Namespace(cwd=cwd_arg, plan_dir=str(self.plan))
        return run_inputs._prepared_root_for_unstated_cwd(args, cwd)

    def test_an_unstated_cwd_adopts_the_prepared_root(self) -> None:
        self.assertEqual(self.repo, self._bound(".", self.elsewhere))

    def test_a_stated_cwd_is_left_for_the_binding_check_to_refuse(self) -> None:
        self.assertIsNone(self._bound(str(self.elsewhere), self.elsewhere))

    def test_already_in_the_prepared_root_changes_nothing(self) -> None:
        self.assertIsNone(self._bound(".", self.repo))

    def test_an_unbound_plan_changes_nothing(self) -> None:
        other = self.tmp / "unbound"
        write_plan(other, ("pending",))
        args = argparse.Namespace(cwd=".", plan_dir=str(other))
        self.assertIsNone(
            run_inputs._prepared_root_for_unstated_cwd(args, self.elsewhere)
        )


class QuietTailTests(unittest.TestCase):
    def _record(self, name: str, **payload: Any) -> dict[str, Any]:
        return {"name": name, "payload": payload, "created_at": NOW.isoformat()}

    def test_bookkeeping_is_routine_and_work_is_not(self) -> None:
        routine = [
            self._record("finalization_heartbeat"),
            self._record("conversation_continuation"),
            self._record(events.AGENT_MUX_DELIVERY_EVENT, final_status="accepted"),
            self._record(events.AGENT_MUX_DELIVERY_EVENT, final_status="duplicate"),
        ]
        shown = [
            self._record(events.AGENT_MUX_DELIVERY_EVENT, final_status="failed"),
            self._record("turn_started"),
            self._record("run_finished"),
            self._record(events.RUN_PAUSED_EVENT),
        ]
        for record in routine:
            self.assertTrue(tail_event_messages.is_routine(record), record)
        for record in shown:
            self.assertFalse(tail_event_messages.is_routine(record), record)


class LocalHarnessEvidenceTests(TempDirCase):
    def _run(self, name: str, *, version: str, turns: int) -> None:
        plan = self.tmp / "plans" / name
        write_plan(plan, ("pending",))
        _register(
            self.state_dir,
            plan,
            readiness_gate={
                "findings": [
                    {
                        "category": "env-harness",
                        "detail": f"codex {version} is newer than the tested 0.1.0",
                    }
                ]
            },
            turns=[
                {"harness": "codex", "returncode": 0, "status_result": "complete"}
                for _ in range(turns)
            ]
            + [{"harness": "codex", "returncode": 1, "status_result": "blocked"}],
        )

    def test_completed_turns_on_the_exact_version_are_counted(self) -> None:
        self._run("one", version="0.9.0", turns=3)
        self._run("two", version="0.9.0", turns=2)
        self._run("other-version", version="0.8.0", turns=7)
        evidence = harness_local_evidence.completed_turns(
            "codex", "0.9.0", state_dir=self.state_dir
        )
        self.assertEqual(
            {
                "completed_turns": 5,
                "runs": 2,
                "window_days": 30,
                "proven": False,
                "harness_turns": 15,
                "in_use": True,
            },
            evidence,
        )
        self.assertIn(
            "at least 5 turn(s) across 2 run(s)",
            harness_local_evidence.describe(evidence),
        )

    def test_enough_local_turns_turn_the_warning_into_ok(self) -> None:
        self._run("busy", version="0.9.0", turns=12)
        evidence = harness_local_evidence.completed_turns(
            "codex", "0.9.0", state_dir=self.state_dir
        )
        self.assertTrue(harness_local_evidence.proven(evidence))
        unknown = harness_compatibility.Compatibility(
            harness="codex",
            status="unknown",
            version="0.9.0",
            executable="/bin/codex",
            reason="newer-than-tested",
            detail="codex 0.9.0 is newer than the tested 0.1.0",
        )
        proven_text = harness_compatibility._format_text([unknown], {"codex": evidence})
        self.assertTrue(proven_text.lstrip().startswith("ok "), proven_text)
        self.assertIn("at least 12 turn(s)", proven_text)
        # Without that proof the same version is still a warning, and the
        # release gate's exit status never looks at local runs.
        unproven_text = harness_compatibility._format_text(
            [unknown], {"codex": {**(evidence or {}), "completed_turns": 3}}
        )
        self.assertTrue(unproven_text.lstrip().startswith("warn "), unproven_text)
        self.assertEqual(3, harness_compatibility.exit_code([unknown], strict=True))
        self.assertFalse(harness_local_evidence.proven(None))

    def test_a_harness_never_run_here_is_idle_not_a_warning(self) -> None:
        self._run("codex-only", version="0.9.0", turns=3)
        evidence = harness_local_evidence.completed_turns(
            "copilot", "1.0.105", state_dir=self.state_dir
        )
        assert evidence is not None
        self.assertFalse(evidence["in_use"])
        unknown = harness_compatibility.Compatibility(
            harness="copilot",
            status="unknown",
            version="1.0.105",
            executable="/bin/copilot",
            reason="newer-than-tested",
            detail="copilot 1.0.105 is newer than the tested 1.0.80",
        )
        text = harness_compatibility._format_text([unknown], {"copilot": evidence})
        self.assertTrue(text.lstrip().startswith("idle "), text)
        self.assertIn("has not run here in the last 30 days", text)
        # In use on another version, though, an unproven one still warns.
        used = harness_local_evidence.completed_turns(
            "codex", "0.9.9", state_dir=self.state_dir
        )
        codex = harness_compatibility.Compatibility(
            harness="codex",
            status="unknown",
            version="0.9.9",
            executable="/bin/codex",
            reason="newer-than-tested",
            detail="codex 0.9.9 is newer than the tested 0.1.0",
        )
        warned = harness_compatibility._format_text([codex], {"codex": used})
        self.assertTrue(warned.lstrip().startswith("warn "), warned)
        self.assertTrue(harness_local_evidence.in_use(None))

    def test_no_evidence_is_said_rather_than_implied(self) -> None:
        evidence = harness_local_evidence.completed_turns(
            "codex", "0.9.0", state_dir=self.state_dir
        )
        self.assertEqual(0, evidence["completed_turns"] if evidence else None)
        # An empty corpus means the harness never ran here at all.
        self.assertIn("has not run here", harness_local_evidence.describe(evidence))
        # In use, but with nothing completed on this exact version.
        self._run("other", version="0.8.0", turns=2)
        evidence = harness_local_evidence.completed_turns(
            "codex", "0.9.0", state_dir=self.state_dir
        )
        self.assertIn(
            "no turn has completed", harness_local_evidence.describe(evidence)
        )
        self.assertEqual("", harness_local_evidence.describe(None))
        self.assertIsNone(
            harness_local_evidence.completed_turns(
                "codex", None, state_dir=self.state_dir
            )
        )


class DesktopNotificationTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan = _titled_plan(self.tmp / "plan", "Ship the thing", ("complete",))

    def _payload(self, **fields: Any) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan),
            "ok": True,
            "paused": False,
            "duration_seconds": 600,
            "final_snapshot": {"next_pending": None},
            **fields,
        }

    def test_a_finished_plan_is_announced_after_an_unattended_run(self) -> None:
        self.assertEqual(
            ("ASL: Ship the thing", "Finished: every session is complete."),
            desktop_notify.message_for(self._payload(), supervised=True),
        )
        self.assertIsNone(
            desktop_notify.message_for(
                self._payload(duration_seconds=3), supervised=True
            )
        )

    def test_a_pause_only_a_person_can_clear_is_announced_at_once(self) -> None:
        payload = self._payload(
            ok=False,
            paused=True,
            pause_reason="blocked",
            pause_message="which API should stay?",
            duration_seconds=2,
        )
        self.assertEqual(
            ("ASL: Ship the thing", "Needs you: which API should stay?"),
            desktop_notify.message_for(payload, supervised=True),
        )

    def test_a_recoverable_pause_under_a_supervisor_stays_silent(self) -> None:
        payload = self._payload(
            ok=False, paused=True, pause_reason="verify-failed", pause_message="x"
        )
        self.assertIsNone(desktop_notify.message_for(payload, supervised=True))
        message = desktop_notify.message_for(payload, supervised=False)
        assert message is not None
        self.assertTrue(message[1].startswith("Paused:"))

    def test_a_dry_run_is_never_announced(self) -> None:
        self.assertIsNone(
            desktop_notify.message_for(self._payload(dry_run=True), supervised=False)
        )

    def test_the_environment_switch_turns_it_off(self) -> None:
        self.assertFalse(desktop_notify.enabled({desktop_notify.ENV_VAR: "0"}))
        self.assertTrue(desktop_notify.enabled({}))
        with mock.patch.object(desktop_notify.subprocess, "Popen") as popen:
            posted = desktop_notify.notify_run_outcome(
                self._payload(), supervised=True, env={desktop_notify.ENV_VAR: "0"}
            )
        self.assertFalse(posted)
        popen.assert_not_called()

    def test_text_reaches_osascript_as_arguments_never_as_script(self) -> None:
        with (
            mock.patch.object(desktop_notify.sys, "platform", "darwin"),
            mock.patch.object(
                desktop_notify.shutil, "which", return_value="/usr/bin/osascript"
            ),
            mock.patch.object(desktop_notify.subprocess, "Popen") as popen,
        ):
            posted = desktop_notify.notify_run_outcome(
                self._payload(), supervised=True, env={}
            )
        self.assertTrue(posted)
        argv = popen.call_args.args[0]
        self.assertEqual("/usr/bin/osascript", argv[0])
        self.assertEqual(
            ["Finished: every session is complete.", "ASL: Ship the thing"], argv[-2:]
        )
        self.assertNotIn("Ship the thing", " ".join(argv[:-2]))


if __name__ == "__main__":
    unittest.main()
