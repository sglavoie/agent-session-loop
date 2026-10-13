"""Pause, review-continuation, and metrics docs vs. authoritative definitions."""

from __future__ import annotations

import importlib.util
import re
import unittest

from agent_session_loop import (
    agent_mux_projection,
    cli_trace,
    events,
    metrics,
    pause_policy,
    review_continuation,
    run_persistence,
    turn_execution,
    workbench_recovery,
)
from tests.asl_docs_support import (
    ASL_GUIDE,
    autopilot_module_sources,
    read_asl_reference,
)
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class AuthoritativePauseMetricsDocsTests(unittest.TestCase):
    docs: str

    guide: str

    justfile: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = read_asl_reference()
        cls.guide = (REPO_ROOT / ASL_GUIDE).read_text(encoding="utf-8")
        cls.justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")

    def test_recoverable_pause_docs_match_autopilot(self) -> None:
        module_path = REPO_ROOT / "skills" / "asl" / "scripts" / "asl_autopilot.py"
        spec = importlib.util.spec_from_file_location(
            "asl_autopilot_docs_regression", module_path
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        autopilot = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(autopilot)

        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        surfaces = (self.docs, skill)
        recoverable_copy = "The helper auto-retries recoverable same-session pauses"
        for surface in surfaces:
            section = surface.split(recoverable_copy, 1)[1].split("\n\n", 1)[0]
            normalized_section = " ".join(section.split())
            pause_list = re.search(r"\(([^)]*)\)", section)
            self.assertIsNotNone(pause_list)
            assert pause_list is not None
            self.assertEqual(
                autopilot.RECOVERABLE_PAUSE_REASONS,
                tuple(re.findall(r"`([^`]+)`", pause_list.group(1))),
            )
            self.assertNotIn("`nonzero-exit`", pause_list.group(1))
            self.assertIn(
                "An unclassified `nonzero-exit` pause is never retried.",
                normalized_section,
            )
            self.assertIn(
                "Only `transport-failed` backs off",
                normalized_section,
            )

    def test_inspect_pause_docs_enumerate_the_policy_set(self) -> None:
        """Every `inspect` reason is named in prose, and nothing else is.

        `user_action` is what a supervising agent branches on, so a reason that
        moves between `reply` and `inspect` without the docs moving with it
        leaves an operator reading instructions the code no longer follows.
        """
        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        marker = "Three kinds of pause are `inspect`"
        for name, surface in (
            ("docs/agent-session-loop.md", self.docs),
            ("skills/asl/SKILL.md", skill),
        ):
            with self.subTest(surface=name):
                self.assertIn(marker, surface, f"{name} never enumerates the set")
                section = surface.split(marker, 1)[1].split("\n\n", 1)[0]
                named = re.findall(r"`([^`]+)`", section)
                for reason in pause_policy.INSPECT_PAUSE_REASONS:
                    self.assertIn(
                        reason,
                        named,
                        f"{name} never names inspect reason {reason!r}",
                    )
                # A reason listed here but classified `reply` would be worse
                # than an omission: it reads as authoritative and is wrong.
                for reason in named:
                    if reason not in events.PAUSE_REASONS:
                        continue
                    self.assertIn(
                        reason,
                        pause_policy.INSPECT_PAUSE_REASONS,
                        f"{name} lists {reason!r} as inspect, but it is not",
                    )

    def test_pause_policy_version_matches_the_documented_revision(self) -> None:
        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        expected = f"`policy_version: {pause_policy.POLICY_VERSION}`"
        for name, surface in (
            ("docs/agent-session-loop.md", self.docs),
            ("skills/asl/SKILL.md", skill),
        ):
            with self.subTest(surface=name):
                self.assertIn(expected, surface)

    def test_worker_hung_docs_match_runner_and_autopilot(self) -> None:
        module_path = REPO_ROOT / "skills" / "asl" / "scripts" / "asl_autopilot.py"
        spec = importlib.util.spec_from_file_location(
            "asl_autopilot_hang_docs_regression", module_path
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        autopilot = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(autopilot)

        # The helper's escalation classes are the runner's persisted hang
        # reasons plus the helper-only idle-ceiling class.
        self.assertEqual(
            turn_execution.HANG_REASON_RESULT_RECEIVED,
            autopilot.HANG_CLASS_RESULT_RECEIVED,
        )
        self.assertEqual(
            turn_execution.HANG_REASON_ZERO_CPU, autopilot.HANG_CLASS_ZERO_CPU
        )

        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        surfaces = {
            "skills/asl/SKILL.md": skill,
            "skills/asl/scripts/asl_autopilot*.py": autopilot_module_sources(),
        }
        for name, text in surfaces.items():
            self.assertIn("worker_hung", text, f"{name} never mentions worker_hung")
            for token in (
                autopilot.HANG_CLASS_RESULT_RECEIVED,
                autopilot.HANG_CLASS_ZERO_CPU,
                autopilot.HANG_CLASS_IDLE_CEILING,
            ):
                self.assertIn(token, text, f"{name} never mentions {token!r}")
        # The pause subsection pins the non-destructive escalation contract.
        normalized_skill = " ".join(skill.split())
        self.assertIn("reason: worker_hung", skill)
        self.assertIn(
            "without killing or waiting on the `asl run` child", normalized_skill
        )
        self.assertIn("one automatic recovery attempt", normalized_skill)
        self.assertIn("never escalates to SIGKILL", normalized_skill)
        self.assertIn("additive `recovery` report", normalized_skill)
        self.assertIn("never the runner or the `asl run` process", skill)
        self.assertIn("re-attach", skill)

    def test_review_continuation_surfaces_share_documented_contract(self) -> None:
        self.assertIn("absolute cross-run cap", self.docs)
        self.assertIn(events.REVIEW_REPAIR_REOPENED_EVENT, self.docs)
        self.assertIn("`review_resume` projection", self.docs)
        self.assertIn("`review_reopen` record", self.docs)
        for action in (
            "initial-review",
            "materialize-repairs",
            "run-repairs",
            "rereview",
            "terminal",
        ):
            self.assertIn(f"`{action}`", self.docs)
        emitted_blockers = (
            "review-harness-required",
            "higher-cap-required",
            "artifact-invalid",
            *sorted(review_continuation.TERMINAL_REVIEW_DISPOSITIONS),
            # A standing environment-blocked pause becomes the blocker verbatim,
            # so every member of that policy set is part of the public blocker
            # vocabulary and has to be named here (see ASI-6).
            *pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS,
            "repair-sessions-missing",
            "repair-sessions-not-runnable",
            "unknown-disposition",
        )
        for blocker in emitted_blockers:
            self.assertIn(f"`{blocker}`", self.docs)
        self.assertIn(
            "An unrecognized recorded disposition is retained in\n"
            "`source_disposition` for diagnosis and maps to the stable\n"
            "`unknown-disposition` blocker.",
            self.docs,
        )
        for recovery_kind in workbench_recovery.RECOVERY_KINDS:
            self.assertIn(f"`{recovery_kind}`", self.docs)

    def test_review_metrics_additive_fields_are_documented(self) -> None:
        report = metrics.collect_metrics(
            state_dir=REPO_ROOT / ".missing-metrics-state",
            since_days=metrics.DEFAULT_SINCE_DAYS,
        )
        for field in (
            "repairs_materialized",
            "unique_materializations",
            "reopen_attempts",
            "successful_reopen_materializations",
            "reopen_failures",
            "reopen_attempts_by_source_disposition",
            "reopen_failures_by_result",
        ):
            self.assertIn(field, report.review_cycles)
            self.assertIn(field, self.docs)
        self.assertIn("Review materializations\nare timestamp-windowed too", self.docs)
        self.assertIn(
            "counted only when the event that carries one falls inside the\nwindow",
            self.docs,
        )
        self.assertIn("recovered reopen\nbackfills", self.docs)
        self.assertIn("not fresh attempts", self.docs)

    def test_operator_surfaces_do_not_restore_manual_review_plan_guidance(
        self,
    ) -> None:
        surfaces = {
            "README.md": (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
            ASL_GUIDE: self.guide,
            "docs/agent-session-loop.md": self.docs,
            "skills/asl/SKILL.md": (
                REPO_ROOT / "skills" / "asl" / "SKILL.md"
            ).read_text(encoding="utf-8"),
            "skills/asl/scripts/asl_autopilot*.py": autopilot_module_sources(),
            "agent_session_loop/workbench.py": (
                REPO_ROOT / "agent_session_loop" / "workbench.py"
            ).read_text(encoding="utf-8"),
        }
        unsupported_guidance = (
            "draft a fix plan",
            "rerun the helper against the generated plan directory",
            "capped at 2 generated follow-up plans",
            "opens bounded review-fix follow-up cycles",
        )
        for name, text in surfaces.items():
            for phrase in unsupported_guidance:
                self.assertNotIn(phrase, text, f"{name} restores {phrase!r}")
        self.assertIn(
            "Do **not** draft, split, or expand a\nfollow-up fix plan",
            surfaces["skills/asl/SKILL.md"],
        )

    def test_state_schema_docs_match_authoritative_versions(self) -> None:
        self.assertEqual(3, agent_mux_projection.STATE_SCHEMA_VERSION)
        self.assertEqual(2, run_persistence.COMPACT_STATE_SCHEMA_VERSION)
        self.assertEqual(2, turn_execution.RESULT_SCHEMA_VERSION)
        self.assertEqual(2, events.EVENT_SCHEMA_VERSION)
        state_files = self.docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        self.assertIn(
            f"schema version {agent_mux_projection.STATE_SCHEMA_VERSION}", state_files
        )
        self.assertIn(
            f"`version: {agent_mux_projection.STATE_SCHEMA_VERSION}`", state_files
        )

    def test_json_output_pins_windowed_metrics_contract(self) -> None:
        section = self.docs.split("## JSON Output", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`metrics --json`", section)
        self.assertIn("narrowing `--since-days` never\nraises a count", section)
        self.assertIn("skipped before its event log is read", section)
        for field in (
            "state_dirs_scanned",
            "state_dirs_active",
            "review_cycles.unique_materializations",
            "review_cycles.repairs_materialized",
        ):
            self.assertIn(field, section)

    def test_cli_invocation_metrics_fields_are_documented(self) -> None:
        report = metrics.collect_metrics(
            state_dir=REPO_ROOT / ".missing-metrics-state",
            since_days=metrics.DEFAULT_SINCE_DAYS,
        )
        section = self.docs.split("## JSON Output", 1)[1].split("\n## ", 1)[0]
        for field in report.cli_invocations:
            self.assertIn(f"`{field}`", section, f"`{field}` is undocumented")
        for field in metrics.RECENT_FAILURE_FIELDS:
            self.assertIn(f"`{field}`", self.docs, f"`{field}` is undocumented")
        self.assertIn("`cli_invocations`", self.docs)
        self.assertIn("CLI invocations", self.docs)
        self.assertIn("windowed by its start timestamp", section)
        self.assertIn(
            f"the {metrics.RECENT_FAILURE_LIMIT} most recent failing", self.docs
        )

    def test_invocation_origin_producers_and_precedence_are_documented(self) -> None:
        section = self.docs.split("### CLI invocation trace", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"`{cli_trace.ORIGIN_ENV_VAR}`", section)
        for origin in cli_trace.ORIGIN_NAMES:
            self.assertIn(f"`{origin}`", section)
        self.assertIn("marker is always applied **after**", section)
        self.assertIn(
            "cannot disguise\nautomation as an operator call",
            section,
        )
        for producer in (
            "autopilot helper's central child environment",
            "`workbench.launch_plan` marks",
            "verifier marks its `asl status --json` smoke call",
        ):
            self.assertIn(producer, section)

    def test_launcher_docs_name_their_own_invocation_origin(self) -> None:
        for heading, origin in (
            ("### Safety model", cli_trace.ORIGIN_AUTOPILOT),
            ("### Security model", cli_trace.ORIGIN_WORKBENCH),
        ):
            section = self.docs.split(heading, 1)[1].split("\n## ", 1)[0]
            self.assertIn(
                f"`{cli_trace.ORIGIN_ENV_VAR}={origin}`",
                section,
                f"{heading} never names its origin marker",
            )

    def test_state_file_docs_pin_trace_root_and_retention(self) -> None:
        section = self.docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        self.assertIn(
            f"retained for {cli_trace.RETENTION_DAYS} days",
            section,
        )
        self.assertIn("`cli_invocations.trace_root`", section)
        self.assertIn("`cli_invocations.retention_days`", section)
        self.assertIn("`asl gc` never touches these files", section)

    def test_metrics_docs_match_default_window(self) -> None:
        self.assertEqual(7, metrics.DEFAULT_SINCE_DAYS)
        self.assertIn(f"last {metrics.DEFAULT_SINCE_DAYS} days", self.docs)


if __name__ == "__main__":
    unittest.main()
