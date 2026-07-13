"""Run review harness lifecycle tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    REPO_ROOT,
    write_plan,
    write_plan_with_ids,
    install_fake_codex,
    install_fake_harness,
    install_fake_spec,
    init_git_repo,
    read_jsonl,
    _contract_block,
    _structured_review_finding,
)


class RunReviewHarnessLifecycleTests(unittest.TestCase):
    """End-to-end coverage for ``--review-harness`` lifecycle integration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        # Install fake_codex under each harness name so review lifecycle tests
        # can exercise distinct --harness / --review-harness pairs without
        # touching the production fixture file.
        self.fake_codex = install_fake_codex(self.tmp_path)
        self.fake_claude = install_fake_harness(self.tmp_path, "claude")
        self.fake_copilot = install_fake_harness(self.tmp_path, "copilot")
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(self.fake_codex)},
                    "claude": {"executable": str(self.fake_claude)},
                    "copilot": {"executable": str(self.fake_copilot)},
                }
            ),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...] = ("pending",),
        *,
        readiness: str | None = "loop-ready",
        sha_override: str | None = None,
    ) -> None:
        contracts_by_id = {
            str(index): _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{index}"}}',
                session=str(index),
            )
            for index in range(1, len(statuses) + 1)
        }
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)
        for index, status_value in enumerate(statuses, start=1):
            if status_value == "complete":
                plans.append_progress_entry(
                    self.plan_dir,
                    plans.ProgressEntry(
                        session_id=plans.parse_session_id(str(index)),
                        status="complete",
                        completed_at=f"2026-06-19T15:00:{index:02d}Z",
                        files_modified=(),
                        key_decisions=None,
                        discovered_facts=(),
                        open_questions=(),
                        blockers=None,
                    ),
                )
        if readiness is None:
            return
        sha = sha_override or plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state=readiness,
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _precreate_final_review(self) -> Path:
        """Stamp both review artifacts so the generated contract passes.

        Generated review sessions are v2 and require a consistent
        ``final-review.json`` beside ``final-review.md``.
        """
        path = self.plan_dir / runner.FINAL_REVIEW_FILENAME
        path.write_text(self._valid_final_review_body(), encoding="utf-8")
        (self.plan_dir / runner.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            self._valid_final_review_artifact_body(), encoding="utf-8"
        )
        return path

    def _valid_final_review_body(self) -> str:
        return (
            "## Findings\n"
            "- Reviewed agent_session_loop/runner.py for generated review "
            "contract behavior.\n"
            "- Checked tests/test_agent_session_loop.py for lifecycle coverage.\n"
            "- Compared docs/agent-session-loop.md for operator-facing "
            "documentation impact.\n"
            "Status: All clear, no findings\n"
        )

    def _valid_final_review_artifact_body(self) -> str:
        return json.dumps(
            {"version": 2, "outcome": "all-clear", "findings": []}
        )

    def _write_pending_review_session(self) -> plans.SessionFile:
        return runner.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )[0]

    def _run(
        self,
        *,
        mode: str = "success",
        harness: str = "codex",
        review_harness: str | None = None,
        harness_model: str | None = None,
        review_harness_model: str | None = None,
        max_sessions: int | None = None,
        allow_not_loop_ready: bool = False,
        dry_run: bool = False,
        timeout: int | None = None,
        json_mode: bool = True,
        fake_codex_sleep: str = "5",
        commit_per_session: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            harness,
            "--state-dir",
            str(self.state_dir),
        ]
        if json_mode:
            argv.append("--json")
        if review_harness is not None:
            argv.extend(["--review-harness", review_harness])
        if harness_model is not None:
            argv.extend(["--harness-model", harness_model])
        if review_harness_model is not None:
            argv.extend(["--review-harness-model", review_harness_model])
        if dry_run:
            argv.append("--dry-run")
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if max_sessions is not None:
            argv.extend(["--max-sessions", str(max_sessions)])
        if timeout is not None:
            argv.extend(["--timeout", str(timeout)])
        if commit_per_session:
            argv.append("--commit-per-session")
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": fake_codex_sleep,
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
            }
        )
        if extra_env is not None:
            env.update(extra_env)
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        payload = json.loads(completed.stdout) if json_mode else None
        return completed, payload

    def _install_mutating_fake_claude(self) -> Path:
        """Replace the claude fake harness with one that mutates the cwd repo.

        The replacement marks the first pending session complete, pre-stamps
        ``final-review.md`` so the generated contract passes, and writes
        ``mutated by review\\n`` to ``README.md`` in the cwd. The mutation
        guard should detect the README change and pause the run.
        """
        script = self.fake_claude
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import re",
                    "import sys",
                    "from pathlib import Path",
                    "",
                    "plan_dir = None",
                    "last_message = None",
                    "for index, arg in enumerate(sys.argv):",
                    "    if arg == '--add-dir' and index + 1 < len(sys.argv):",
                    "        plan_dir = Path(sys.argv[index + 1])",
                    "    if arg == '-o' and index + 1 < len(sys.argv):",
                    "        last_message = Path(sys.argv[index + 1])",
                    "if plan_dir is None:",
                    "    print('missing --add-dir', file=sys.stderr)",
                    "    sys.exit(2)",
                    "",
                    "# Mutate a tracked file inside cwd to trigger the guard.",
                    "(Path.cwd() / 'README.md').write_text(",
                    "    'mutated by review\\n', encoding='utf-8',",
                    ")",
                    "",
                    "session_re = re.compile(",
                    "    r'^session-([1-9][0-9]*(?:\\.[1-9][0-9]*)*)\\.md$'",
                    ")",
                    "def sort_key(path):",
                    "    match = session_re.match(path.name)",
                    "    if match is None:",
                    "        return ()",
                    "    return tuple(int(c) for c in match.group(1).split('.'))",
                    "pending = []",
                    "for path in sorted(plan_dir.glob('session-*.md'), key=sort_key):",
                    "    if session_re.match(path.name) is None:",
                    "        continue",
                    "    text = path.read_text(encoding='utf-8')",
                    "    if '- **Status**: pending' in text:",
                    "        pending.append(path)",
                    "if pending:",
                    "    target = pending[0]",
                    "    text = target.read_text(encoding='utf-8')",
                    "    text = text.replace(",
                    "        '- **Status**: pending',",
                    "        '- **Status**: complete',",
                    "        1,",
                    "    )",
                    "    target.write_text(text, encoding='utf-8')",
                    "",
                    "(plan_dir / 'final-review.md').write_text(",
                    "    (",
                    "        '## Findings\\n'",
                    "        '- Reviewed agent_session_loop/runner.py for mutation guard behavior.\\n'",
                    "        '- Checked tests/test_agent_session_loop.py for review lifecycle coverage.\\n'",
                    "        '- Compared docs/agent-session-loop.md for operator documentation impact.\\n'",
                    "        'Status: All clear, no findings\\n'",
                    "    ),",
                    "    encoding='utf-8',",
                    ")",
                    "(plan_dir / 'final-review.json').write_text(",
                    "    '{\"version\": 2, \"outcome\": \"all-clear\", "
                    "\"findings\": []}\\n',",
                    "    encoding='utf-8',",
                    ")",
                    "",
                    "if last_message is not None:",
                    "    last_message.parent.mkdir(parents=True, exist_ok=True)",
                    "    last_message.write_text(",
                    "        'Runner status: complete\\n', encoding='utf-8',",
                    "    )",
                    "",
                    "sys.exit(0)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def _events_path(self) -> Path:
        plan_state = self.state_dir / plans.path_sha256(
            plans.canonicalize_path(self.plan_dir)
        )
        return plan_state / events.EVENTS_FILE_NAME

    def _read_events(self) -> list[dict[str, object]]:
        return read_jsonl(self._events_path())

    def test_fresh_run_appends_and_completes_review_session(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_requested"])
        self.assertEqual("claude", payload["review_harness"])
        self.assertTrue(payload["review_completed"])
        self.assertIsNone(payload.get("review_skipped_reason"))
        review_session_id = payload["review_session_id"]
        self.assertIsNotNone(review_session_id)
        # Review session id must be a fresh top-level id beyond impl sessions.
        self.assertEqual("2", review_session_id)
        review_path = self.plan_dir / f"session-{review_session_id}.md"
        self.assertTrue(review_path.is_file())
        self.assertTrue(runner.is_generated_review_session_file(review_path))
        # Both impl and review turns ran with the right kind tag.
        turns = payload["turns"]
        self.assertEqual(2, len(turns))
        self.assertEqual(
            runner.SESSION_KIND_IMPLEMENTATION, turns[0]["session_kind"]
        )
        self.assertEqual("codex", turns[0]["harness"])
        self.assertEqual(runner.SESSION_KIND_REVIEW, turns[1]["session_kind"])
        self.assertEqual("claude", turns[1]["harness"])
        self.assertEqual(1, turns[0]["total_sessions"])
        self.assertEqual(2, turns[1]["total_sessions"])

    def test_role_specific_models_reach_only_their_matching_turns(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(
            review_harness="claude",
            harness_model="gpt-5",
            review_harness_model="claude-opus-4",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        turns = payload["turns"]
        self.assertEqual("gpt-5", turns[0]["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turns[0]["model_source"])
        self.assertEqual("claude-opus-4", turns[1]["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turns[1]["model_source"])
        self.assertEqual(
            "gpt-5", turns[0]["argv"][turns[0]["argv"].index("--model") + 1]
        )
        self.assertEqual(
            "claude-opus-4",
            turns[1]["argv"][turns[1]["argv"].index("--model") + 1],
        )

        turn_started = [
            event["payload"]
            for event in self._read_events()
            if event["name"] == "turn_started"
        ]
        self.assertEqual(
            ["gpt-5", "claude-opus-4"],
            [event["model"] for event in turn_started],
        )

    def test_omitted_role_specific_models_leave_native_defaults_unforwarded(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        turns = payload["turns"]
        self.assertNotIn("--model", turns[0]["argv"])
        self.assertNotIn("--model", turns[1]["argv"])
        self.assertNotEqual(config.MODEL_SOURCE_FLAG, turns[0]["model_source"])
        self.assertNotEqual(config.MODEL_SOURCE_FLAG, turns[1]["model_source"])

    def test_dry_run_existing_review_uses_review_harness_model(self) -> None:
        self._write_plan(("complete",), readiness="complete")
        self._write_pending_review_session()

        completed, payload = self._run(
            review_harness="claude",
            harness_model="gpt-5",
            review_harness_model="claude-opus-4",
            dry_run=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        planned = payload["planned"]
        invocation = planned["invocation"]
        self.assertEqual("claude", invocation["name"])
        self.assertEqual("claude-opus-4", planned["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, planned["model_source"])
        self.assertEqual(
            "claude-opus-4",
            invocation["argv"][invocation["argv"].index("--model") + 1],
        )

    def test_review_runs_when_plan_starts_already_complete(self) -> None:
        # Author the plan as complete from the start, with readiness=complete
        # and a matching SHA, so the normal loop-ready gate would refuse it.
        self._write_plan(("complete",), readiness="complete")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("2", payload["review_session_id"])
        # Only the review turn should have executed.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            runner.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"]
        )
        self.assertEqual("claude", payload["turns"][0]["harness"])
        # Initial readiness was 'complete'; after appending a review session
        # the readiness was switched to 'running' and then finalized to
        # 'complete' once all sessions (impl + review) ended complete.
        self.assertEqual("complete", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])

    def test_complete_plan_review_harness_repairs_progress_journal(
        self,
    ) -> None:
        # Complete plan with `readiness=complete`; delete the journal so the
        # auto-repair must rebuild it. The repair must rebuild progress.md
        # without entering the manifest-readiness phase (no SHA drift) and
        # without downgrading the `complete` readiness marker.
        self._write_plan(("complete",), readiness="complete")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()
        progress_file = plans.progress_path(self.plan_dir)
        self.assertTrue(progress_file.exists())
        progress_file.unlink()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("complete", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])
        self.assertTrue(progress_file.exists())

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(
            ["progress-journal-missing"], repair_payload["attempted"]
        )
        self.assertEqual([], repair_payload["errors"])
        # `ok` is the strict readiness-gate state (`loop-ready`); the
        # complete plan stays at `complete`, so the strict gate stays
        # False even though the run proceeds via the review-only
        # complete-start exception.
        self.assertFalse(repair_payload["ok"])
        # Only the progress repair fired; manifest-readiness was not needed.
        categories = [
            repair["category"] for repair in repair_payload["auto_repairs"]
        ]
        self.assertEqual(["progress-journal-missing"], categories)
        # Final on-disk readiness is `complete` (the lifecycle finishes
        # writing it after the review session completes).
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", metadata.state)

    def test_complete_plan_review_harness_repairs_sha_keeps_complete_readiness(
        self,
    ) -> None:
        # Complete plan with `readiness=complete` but a stale SHA. The
        # auto-repair must rewrite the manifest readiness metadata with the
        # current SHA *and* keep the state at `complete`, not downgrade to
        # `loop-ready` just to satisfy the readiness gate.
        self._write_plan(
            ("complete",), readiness="complete", sha_override="0" * 64
        )
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("complete", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(
            ["manifest-readiness"], repair_payload["attempted"]
        )
        self.assertEqual([], repair_payload["errors"])
        # `ok` is the strict readiness-gate state (`loop-ready`); the
        # complete plan stays at `complete`, so the strict gate stays
        # False even though the run proceeds via the review-only
        # complete-start exception.
        self.assertFalse(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("manifest-readiness", repair["category"])
        # The fix: target_state is `complete`, not `loop-ready`. This is the
        # whole point of the review-only complete start branch in
        # `_repair_readiness_gate`.
        self.assertEqual("complete", repair["target_state"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", metadata.state)
        self.assertNotEqual("0" * 64, metadata.readiness_sha256)

    def test_resumes_existing_pending_review_session(self) -> None:
        # Author the plan as fully complete + readiness=loop-ready first,
        # then drop in a pre-existing pending review session so we end up
        # with the impl session complete and the review session pending.
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_session = self._write_pending_review_session()
        # The readiness SHA must reflect the plan including the review session.
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        # The pre-existing review session must be reused, not replaced.
        self.assertEqual(
            review_session.session_id.value, payload["review_session_id"]
        )
        # Only the existing review session needed to run.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            runner.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"]
        )

    def test_pending_review_session_without_review_harness_pauses(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("review-harness-required", payload["pause_reason"])
        # No turns should have run -- the runner refused before launching.
        self.assertEqual([], payload["turns"])

    def test_max_sessions_skips_review_and_records_skip_reason(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            review_harness="claude",
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(
            "max-sessions", payload["review_skipped_reason"]
        )
        # Only one implementation turn ran; the loop stopped before any
        # review session could be appended.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            runner.SESSION_KIND_IMPLEMENTATION,
            payload["turns"][0]["session_kind"],
        )
        # No review session file should have been created.
        review_candidates = [
            path
            for path in self.plan_dir.glob("session-*.md")
            if runner.is_generated_review_session_file(path)
        ]
        self.assertEqual([], review_candidates)

    def test_review_session_created_event_is_emitted(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        review_session_id = payload["review_session_id"]
        events_log = self._read_events()
        created_events = [
            entry
            for entry in events_log
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        ]
        self.assertEqual(1, len(created_events))
        created_payload = created_events[0]["payload"]
        self.assertEqual(review_session_id, created_payload["session_id"])
        self.assertEqual("claude", created_payload["review_harness"])
        canonical_plan_dir = plans.canonicalize_path(self.plan_dir)
        self.assertEqual(
            str(canonical_plan_dir / f"session-{review_session_id}.md"),
            created_payload["review_session_path"],
        )
        self.assertEqual(
            str(runner.final_review_output_path(canonical_plan_dir)),
            created_payload["review_output_path"],
        )

    def test_review_session_created_event_not_emitted_when_reused(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        events_log = self._read_events()
        created_events = [
            entry
            for entry in events_log
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        ]
        self.assertEqual([], created_events)

    def test_review_mutated_worktree_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_session = self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # Mutation should be visible on disk after the pause.
        self.assertEqual(
            "mutated by review\n",
            (self.repo_root / "README.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "pending", plans.read_session_status(review_session).status
        )
        self.assertEqual(1, payload["final_snapshot"]["counts"]["pending"])
        guard = payload["turns"][0].get("review_mutation_guard")
        self.assertIsNotNone(guard)
        self.assertFalse(guard["passed"])
        self.assertEqual("review-mutated-worktree", guard["reason"])
        self.assertTrue(guard["reverted_to_pending"])

    def test_review_nonzero_exit_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(
            mode="failure", review_harness="claude"
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            runner.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"]
        )

    def test_review_timeout_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(
            mode="timeout",
            review_harness="claude",
            timeout=1,
            fake_codex_sleep="5",
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])

    def test_review_missing_final_review_file_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        # Intentionally omit _precreate_final_review() so the contract fails.

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # The runner should have reverted the review session back to pending.
        review_path = next(
            path
            for path in self.plan_dir.glob("session-*.md")
            if runner.is_generated_review_session_file(path)
        )
        review_text = review_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_review_empty_final_review_file_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        # Whitespace-only final-review.md must fail the contract.
        (self.plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
            "   \n\t\n", encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])

    def test_review_low_effort_final_review_file_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        (self.plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
            "Looks good overall.\nNo issues found.\n", encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])

    def test_legacy_review_contract_still_requires_runner_verdict(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_id = plans.next_top_level_session_id(self.plan_dir)
        legacy_contract = {
            "session": review_id.value,
            "checks": [
                {
                    "type": "command",
                    "name": "legacy-final-review-output",
                    "argv": [
                        "python3",
                        "-c",
                        (
                            "import pathlib, sys; "
                            "text = pathlib.Path('final-review.md').read_text("
                            "encoding='utf-8'); "
                            "sys.exit(0 if text.strip() else 1)"
                        ),
                    ],
                    "cwd": str(plans.canonicalize_path(self.plan_dir)),
                    "timeout_seconds": 60,
                }
            ],
        }
        (self.plan_dir / review_id.filename).write_text(
            "\n".join(
                [
                    f"# {review_id.display_label}: Final Review",
                    "",
                    runner.REVIEW_SESSION_MARKER,
                    "",
                    "## Instructions",
                    "",
                    "- Perform a critical review and write final-review.md.",
                    "- Do not modify repository files.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    "- final-review.md exists and contains review findings.",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    json.dumps(legacy_contract, indent=2),
                    "```",
                    "",
                    "## Handoff",
                    "",
                    "- **Status**: pending",
                    "- **Files modified**: (none yet)",
                    "- **Key decisions**: (none yet)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        (self.plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
            (
                "## Findings\n"
                "- Reviewed agent_session_loop/runner.py for generated review "
                "contracts.\n"
                "- Checked tests/test_agent_session_loop.py for lifecycle "
                "coverage.\n"
                "- Compared docs/agent-session-loop.md for operator-facing "
                "documentation impact.\n"
            ),
            encoding="utf-8",
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertIsNone(payload["review_verdict"])
        review_text = (self.plan_dir / review_id.filename).read_text(
            encoding="utf-8"
        )
        self.assertIn("- **Status**: pending", review_text)

    def test_text_output_prints_review_path(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, _ = self._run(
            review_harness="claude", json_mode=False
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        expected_path = runner.final_review_output_path(
            plans.canonicalize_path(self.plan_dir)
        )
        self.assertIn(f"Review: {expected_path}", completed.stdout)
        self.assertEqual(
            "Status: All clear, no findings",
            completed.stdout.splitlines()[-1],
        )
        self.assertIn("session 1/1 started", completed.stderr)
        self.assertIn("review session started (2/2)", completed.stderr)
        self.assertNotIn("session 2/2 started", completed.stderr)

    def _write_dotted_plan(
        self,
        statuses: tuple[tuple[str, str], ...],
        *,
        readiness: str | None = "loop-ready",
    ) -> None:
        contracts_by_id = {
            session_id: _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{session_id}"}}',
                session=session_id,
            )
            for session_id, _ in statuses
        }
        write_plan_with_ids(
            self.plan_dir, statuses, contracts_by_id=contracts_by_id
        )
        if readiness is None:
            return
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state=readiness,
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _rewrite_harness_config(self, mapping: dict[str, str]) -> None:
        """Overwrite the harness config with only the named harnesses."""
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.write_text(
            json.dumps(
                {name: {"executable": path} for name, path in mapping.items()}
            ),
            encoding="utf-8",
        )

    def _install_fake_spec_on_path(self) -> tuple[Path, Path, dict[str, str]]:
        fake_spec = install_fake_spec(self.tmp_path)
        log_path = self.tmp_path / "fake-spec.jsonl"
        env = {
            "PATH": str(self.tmp_path) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SPEC_LOG": str(log_path),
        }
        return fake_spec, log_path, env

    def test_fresh_run_review_writes_final_review_via_fake_harness(self) -> None:
        # Exercise the new fake-harness review mode end-to-end: the implementation
        # turn just marks its session complete, but the review turn must actually
        # write `final-review.md` so the generated contract passes. We do not
        # pre-stamp the file -- the harness fixture has to produce it.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("claude", payload["review_harness"])
        self.assertEqual(
            {
                "status_line": "Status: All clear, no findings",
                "outcome": "all-clear",
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            },
            payload["review_verdict"],
        )
        review_path = runner.final_review_output_path(
            plans.canonicalize_path(self.plan_dir)
        )
        self.assertTrue(review_path.is_file())
        self.assertEqual(
            self._valid_final_review_body(),
            review_path.read_text(encoding="utf-8"),
        )

    def test_copilot_implementation_run_completes_claude_review(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            harness="copilot",
            review_harness="claude",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("claude", payload["review_harness"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("2", payload["review_session_id"])
        self.assertEqual(
            {
                "status_line": "Status: All clear, no findings",
                "outcome": "all-clear",
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            },
            payload["review_verdict"],
        )
        turns = payload["turns"]
        self.assertEqual(["copilot", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual(
            [runner.SESSION_KIND_IMPLEMENTATION, runner.SESSION_KIND_REVIEW],
            [turn["session_kind"] for turn in turns],
        )

    def test_codex_implementation_run_completes_copilot_review(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            harness="codex",
            review_harness="copilot",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("copilot", payload["review_harness"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("2", payload["review_session_id"])
        self.assertEqual(
            {
                "status_line": "Status: All clear, no findings",
                "outcome": "all-clear",
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            },
            payload["review_verdict"],
        )
        turns = payload["turns"]
        self.assertEqual(["codex", "copilot"], [turn["harness"] for turn in turns])
        self.assertEqual(
            [runner.SESSION_KIND_IMPLEMENTATION, runner.SESSION_KIND_REVIEW],
            [turn["session_kind"] for turn in turns],
        )

    def test_review_verdict_persisted_for_counted_findings(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_body = (
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Medium issue in tests/test_agent_session_loop.py.\n"
            "- Low issue in docs/agent-session-loop.md.\n"
            "Status: 5 Findings (2 high, 2 med, 1 low)\n"
        )
        severities = ("high", "high", "med", "med", "low")
        review_json = json.dumps(
            {
                "version": 2,
                "outcome": "findings",
                "findings": [
                    _structured_review_finding(
                        finding_id=f"F{index}", severity=severity
                    )
                    for index, severity in enumerate(severities, start=1)
                ],
            }
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                "FAKE_CODEX_REVIEW_BODY": review_body,
                "FAKE_CODEX_REVIEW_JSON": review_json,
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(
            {
                "status_line": "Status: 5 Findings (2 high, 2 med, 1 low)",
                "outcome": "findings",
                "total_findings": 5,
                "findings_by_severity": {"high": 2, "med": 2, "low": 1},
            },
            payload["review_verdict"],
        )

    def _setup_complete_plan_with_pending_review(self) -> plans.SessionFile:
        """Author a complete plan carrying a pending generated review session."""
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_session = self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        return review_session

    def test_v2_review_missing_structured_artifact_pauses_run(self) -> None:
        review_session = self._setup_complete_plan_with_pending_review()
        # Valid markdown review, but no final-review.json: the generated v2
        # contract must fail closed.
        (self.plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
            self._valid_final_review_body(), encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertIsNone(payload["review_verdict"])
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_v2_review_contradictory_structured_artifact_pauses_run(
        self,
    ) -> None:
        review_session = self._setup_complete_plan_with_pending_review()
        (self.plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_agent_session_loop.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        # All-clear artifact contradicts the findings status line.
        (self.plan_dir / runner.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_v2_review_incomplete_artifact_fails_runner_deep_validation(
        self,
    ) -> None:
        # A finding whose verification check is structurally present but not a
        # valid contract check passes the standalone contract gate and must be
        # caught by the runner's shared-module validation.
        review_session = self._setup_complete_plan_with_pending_review()
        (self.plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_agent_session_loop.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        finding = _structured_review_finding(
            verification_checks=[{"type": "bogus", "name": "not-a-check"}]
        )
        (self.plan_dir / runner.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": [finding]}
            ),
            encoding="utf-8",
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertEqual(
            "malformed-review-artifact",
            payload["pause_details"]["failure_reason"],
        )
        self.assertFalse(payload["review_completed"])
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)
        self.assertIn("review verdict validation failed", review_text)

    def test_v1_review_session_markdown_only_still_completes(self) -> None:
        # Existing pending v1 review sessions (base marker, no version marker)
        # must stay Markdown-compatible: no final-review.json required.
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_id = plans.next_top_level_session_id(self.plan_dir)
        v1_contract = {
            "session": review_id.value,
            "checks": [
                {
                    "type": "command",
                    "name": "v1-final-review-output",
                    "argv": [
                        "python3",
                        "-c",
                        (
                            "import pathlib, sys; "
                            "text = pathlib.Path('final-review.md').read_text("
                            "encoding='utf-8'); "
                            "sys.exit(0 if text.strip() else 1)"
                        ),
                    ],
                    "cwd": str(plans.canonicalize_path(self.plan_dir)),
                    "timeout_seconds": 60,
                }
            ],
        }
        (self.plan_dir / review_id.filename).write_text(
            "\n".join(
                [
                    f"# {review_id.display_label}: Final Review",
                    "",
                    runner.REVIEW_SESSION_MARKER,
                    "",
                    "## Instructions",
                    "",
                    "- Perform a critical review and write final-review.md.",
                    "- Do not modify repository files.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    "- final-review.md exists and contains review findings.",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    json.dumps(v1_contract, indent=2),
                    "```",
                    "",
                    "## Handoff",
                    "",
                    "- **Status**: pending",
                    "- **Files modified**: (none yet)",
                    "- **Key decisions**: (none yet)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        (self.plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
            self._valid_final_review_body(), encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual(
            {
                "status_line": "Status: All clear, no findings",
                "outcome": "all-clear",
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            },
            payload["review_verdict"],
        )
        self.assertFalse(
            (self.plan_dir / runner.FINAL_REVIEW_ARTIFACT_FILENAME).is_file()
        )

    def test_review_harness_ignores_quoted_runner_pause_in_stderr(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                "FAKE_CODEX_STDOUT_TEXT": "Runner status: complete\n",
                "FAKE_CODEX_STDERR_TEXT": (
                    "```text\n"
                    "RUNNER_PAUSE: unsafe - plan is not loop-ready; "
                    "run `asl preflight <plan-dir> --mark-loop-ready` before "
                    "continuing\n"
                    "```\n"
                ),
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertIsNone(payload.get("action_required"))
        self.assertEqual(
            [runner.SESSION_KIND_IMPLEMENTATION, runner.SESSION_KIND_REVIEW],
            [turn["session_kind"] for turn in payload["turns"]],
        )
        for turn in payload["turns"]:
            self.assertEqual("complete", turn["status_result"])
            self.assertNotIn("runner_pause", turn)

    def test_review_harness_unavailable_pauses_run(self) -> None:
        # Configure `claude` with a non-existent path so `shutil.which` returns
        # None regardless of what the host PATH contains. The runner must
        # pause with `harness-unavailable` before launching the review turn.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        missing_claude = self.tmp_path / "definitely-not-a-real-claude"
        self._rewrite_harness_config(
            {
                "codex": str(self.fake_codex),
                "claude": str(missing_claude),
            }
        )

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("harness-unavailable", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # The implementation turn ran with `codex`; the failure must occur
        # before the review turn starts.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            runner.SESSION_KIND_IMPLEMENTATION, payload["turns"][0]["session_kind"]
        )
        # A review session was appended (so it's recorded for resume) even
        # though the review turn could not start.
        self.assertEqual("2", payload["review_session_id"])

    def test_run_rejects_same_review_harness_via_cli(self) -> None:
        # The CLI must refuse when --review-harness matches --harness.
        self._write_plan(("pending",), readiness="loop-ready")

        completed, payload = self._run(
            mode="success-with-review",
            harness="codex",
            review_harness="codex",
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn(
            "--review-harness must differ from --harness",
            payload.get("error", ""),
        )
        self.assertTrue(payload["review_requested"])
        self.assertEqual("codex", payload["review_harness"])
        self.assertIsNone(payload["review_session_id"])
        self.assertEqual(
            str(
                runner.final_review_output_path(
                    plans.canonicalize_path(self.plan_dir)
                )
            ),
            payload["review_output_path"],
        )
        self.assertFalse(payload["review_completed"])
        self.assertEqual("input-error", payload["review_skipped_reason"])

    def test_review_appends_at_top_level_id_in_dotted_plan(self) -> None:
        # Dotted-session plans must still get a single top-level review id
        # one greater than the max top-level component.
        self._write_dotted_plan(
            (("1", "pending"), ("1.1", "pending")), readiness="loop-ready"
        )
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        # The generated review session must be `session-2.md`, not `1.2`.
        self.assertEqual("2", payload["review_session_id"])
        self.assertTrue((self.plan_dir / "session-2.md").is_file())
        # The two impl sessions ran in dotted order, then the review turn.
        turns = payload["turns"]
        self.assertEqual(3, len(turns))
        self.assertEqual("1", turns[0]["session_id"])
        self.assertEqual("1.1", turns[1]["session_id"])
        self.assertEqual("2", turns[2]["session_id"])
        self.assertEqual(
            runner.SESSION_KIND_REVIEW, turns[2]["session_kind"]
        )

    def test_commit_per_session_skips_review_turn(self) -> None:
        # --commit-per-session must commit after implementation sessions but
        # never after the generated review session.
        self._write_plan(("pending",), readiness="loop-ready")
        _fake_spec, spec_log, spec_env = self._install_fake_spec_on_path()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            commit_per_session=True,
            extra_env=spec_env,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        # Exactly one auto-commit fired: for the implementation session.
        spec_records = read_jsonl(spec_log)
        self.assertEqual(1, len(spec_records))
        self.assertEqual(
            ["commit", "--yes", "--no-verify"], spec_records[0]["argv"][1:]
        )
        self.assertEqual(1, len(payload["commits"]))
        self.assertEqual("session", payload["commits"][0]["scope"])
        # The committed scope must reference the implementation session id (1),
        # not the review session id.
        self.assertEqual(1, payload["commits"][0]["session"])

    def test_run_without_review_harness_leaves_review_fields_unset(self) -> None:
        # When --review-harness is absent the run must behave exactly like
        # the pre-review-harness implementation: no review session file,
        # no review event, no review-related payload fields populated.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(mode="success")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertFalse(payload["review_requested"])
        self.assertFalse(payload["review_completed"])
        self.assertIsNone(payload.get("review_harness"))
        self.assertIsNone(payload.get("review_session_id"))
        self.assertIsNone(payload.get("review_skipped_reason"))
        # All turns are implementation, never review.
        self.assertTrue(payload["turns"])
        for turn in payload["turns"]:
            self.assertEqual(
                runner.SESSION_KIND_IMPLEMENTATION, turn["session_kind"]
            )
        # No generated review session file should exist.
        review_candidates = [
            path
            for path in self.plan_dir.glob("session-*.md")
            if runner.is_generated_review_session_file(path)
        ]
        self.assertEqual([], review_candidates)
        # No final-review.md should be written either.
        self.assertFalse(
            runner.final_review_output_path(self.plan_dir).is_file()
        )
        # No review-session-created event should have been emitted.
        review_events = [
            entry
            for entry in self._read_events()
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        ]
        self.assertEqual([], review_events)


if __name__ == "__main__":
    unittest.main()
