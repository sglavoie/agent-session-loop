"""Run review harness lifecycle tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import (
    ExitStack,
)
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    mailbox,
    pause_policy,
    plans,
    review,
    review_continuation,
    review_cycle,
    review_delta,
    review_guard,
    review_repair,
    review_session_body,
    review_session_contract,
    review_transitions,
    run_coordination,
    run_loop,
    run_persistence,
    run_selection,
    runner,
    session_verification,
    state,
    turn_execution,
    verification_transitions,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    _structured_review_finding,
    git_stdout,
    init_git_repo,
    install_fake_codex,
    install_fake_harness,
    patched_env,
    read_jsonl,
    write_completed_review_plan,
    write_plan,
    write_plan_with_ids,
)

#: Backstop for an ``agent_session_loop run`` subprocess, matching the sibling
#: loop-test modules. It is a hang guard, not a performance assertion: a run
#: that finishes in a second on an idle machine can take far longer than a
#: tight bound while the rest of the sharded suite competes for the CPU.
LOOP_RUN_TIMEOUT_SECONDS = 120


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
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
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
        path = self.plan_dir / review.FINAL_REVIEW_FILENAME
        path.write_text(self._valid_final_review_body(), encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
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
        return json.dumps({"version": 2, "outcome": "all-clear", "findings": []})

    def _write_pending_review_session(self) -> plans.SessionFile:
        return review_session_body.ensure_generated_review_session(
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
        max_review_fix_cycles: int | None = None,
        allow_not_loop_ready: bool = False,
        dry_run: bool = False,
        timeout: int | None = None,
        json_mode: bool = True,
        fake_codex_sleep: str = "5",
        commit_per_session: bool = False,
        review_guard_exclude: tuple[str, ...] = (),
        review_guard_include_ignored: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
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
        if max_review_fix_cycles is not None:
            argv.extend(["--max-review-fix-cycles", str(max_review_fix_cycles)])
        if timeout is not None:
            argv.extend(["--timeout", str(timeout)])
        if commit_per_session:
            argv.append("--commit-per-session")
        for exclude in review_guard_exclude:
            argv.extend(["--review-guard-exclude", exclude])
        if review_guard_include_ignored:
            argv.append("--review-guard-include-ignored")
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": fake_codex_sleep,
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
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
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        payload = json.loads(completed.stdout) if json_mode else None
        return completed, payload

    def _install_mutating_fake_claude(
        self,
        mutate_paths: tuple[str, ...] = ("README.md",),
        *,
        invalid_first_attempt: bool = False,
    ) -> Path:
        """Replace the claude fake harness with one that mutates the cwd repo.

        The replacement marks the first pending session complete, pre-stamps
        ``final-review.md`` so the generated contract passes, and writes
        ``mutated by review\\n`` to every path in ``mutate_paths`` (relative to
        the cwd). The mutation guard should detect those changes and pause the
        run unless every path is excluded.

        ``invalid_first_attempt`` leaves the worktree alone on the first review
        turn and emits an artifact that fails deep validation, so the mutation
        only lands on the in-run retry turn.
        """
        invalid_artifact = (
            json.dumps(
                {
                    "version": 2,
                    "outcome": "findings",
                    "findings": [
                        _structured_review_finding(
                            verification_checks=[
                                {"type": "bogus", "name": "not-a-check"}
                            ]
                        )
                    ],
                }
            )
            + "\n"
        )
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
                    "retry_notes = sum(",
                    "    path.read_text(encoding='utf-8').count(",
                    "        'review verdict validation failed'",
                    "    )",
                    "    for path in plan_dir.glob('session-*.md')",
                    ")",
                    f"emit_invalid = {invalid_first_attempt!r} and retry_notes == 0",
                    "",
                    "# Mutate files inside cwd to trigger the guard.",
                    "if not emit_invalid:",
                    f"    for rel in {tuple(mutate_paths)!r}:",
                    "        mutated = Path.cwd() / rel",
                    "        mutated.parent.mkdir(parents=True, exist_ok=True)",
                    "        mutated.write_text('mutated by review\\n', "
                    "encoding='utf-8')",
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
                    "if emit_invalid:",
                    "    review_body = (",
                    "        '## Findings\\n'",
                    "        '- High issue in agent_session_loop/runner.py.\\n'",
                    "        '- Context in tests/test_agent_session_loop.py.\\n'",
                    "        '- Context in docs/agent-session-loop.md.\\n'",
                    "        'Status: 1 Findings (1 high, 0 med, 0 low)\\n'",
                    "    )",
                    f"    review_json = {invalid_artifact!r}",
                    "else:",
                    "    review_body = (",
                    "        '## Findings\\n'",
                    "        '- Reviewed agent_session_loop/runner.py for mutation "
                    "guard behavior.\\n'",
                    "        '- Checked tests/test_agent_session_loop.py for review "
                    "lifecycle coverage.\\n'",
                    "        '- Compared docs/agent-session-loop.md for operator "
                    "documentation impact.\\n'",
                    "        'Status: All clear, no findings\\n'",
                    "    )",
                    "    review_json = (",
                    '        \'{"version": 2, "outcome": "all-clear", '
                    '"findings": []}\\n\'',
                    "    )",
                    "(plan_dir / 'final-review.md').write_text(",
                    "    review_body, encoding='utf-8',",
                    ")",
                    "(plan_dir / 'final-review.json').write_text(",
                    "    review_json, encoding='utf-8',",
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

    def _read_events(self) -> list[dict[str, Any]]:
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
        self.assertTrue(
            review_session_contract.is_generated_review_session_file(review_path)
        )
        # Both impl and review turns ran with the right kind tag.
        turns = payload["turns"]
        self.assertEqual(2, len(turns))
        self.assertEqual(plans.SESSION_KIND_IMPLEMENTATION, turns[0]["session_kind"])
        self.assertEqual("codex", turns[0]["harness"])
        self.assertEqual(plans.SESSION_KIND_REVIEW, turns[1]["session_kind"])
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
        self.assertEqual(plans.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"])
        self.assertEqual("claude", payload["turns"][0]["harness"])
        # Initial readiness was 'complete'; after appending a review session
        # the readiness was switched to 'running' and then finalized to
        # 'complete' once all sessions (impl + review) ended complete.
        self.assertEqual("complete", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])

    def test_completed_review_handoff_plan_artifacts_are_normalized(self) -> None:
        self._write_plan(("complete",), readiness=None)
        review_path = self._write_pending_review_session().path
        review_path.write_text(
            review_path.read_text(encoding="utf-8")
            .replace("- **Status**: pending", "- **Status**: complete")
            .replace(
                "- **Files modified**: (none yet)",
                "- **Files modified**: final-review.md, final-review.json",
            ),
            encoding="utf-8",
        )
        self._precreate_final_review()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        self.assertEqual([str(review_path)], payload["normalized_review_handoffs"])
        self.assertIn(
            "- **Files modified**: (none)",
            review_path.read_text(encoding="utf-8"),
        )

    def test_fresh_review_handoff_plan_artifacts_are_normalized(self) -> None:
        self._write_plan(("complete",), readiness="complete")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(
            review_harness="claude",
            extra_env={
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {"2": ["final-review.md", "final-review.json"]}
                )
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        review_path = self.plan_dir / "session-2.md"
        self.assertEqual(
            [str(plans.canonicalize_path(review_path))],
            payload["normalized_review_handoffs"],
        )
        self.assertIn(
            "- **Files modified**: (none)",
            review_path.read_text(encoding="utf-8"),
        )

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
        self.assertEqual(["progress-journal-missing"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        # `ok` is the strict readiness-gate state (`loop-ready`); the
        # complete plan stays at `complete`, so the strict gate stays
        # False even though the run proceeds via the review-only
        # complete-start exception.
        self.assertFalse(repair_payload["ok"])
        # Only the progress repair fired; manifest-readiness was not needed.
        categories = [repair["category"] for repair in repair_payload["auto_repairs"]]
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
        self._write_plan(("complete",), readiness="complete", sha_override="0" * 64)
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
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
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
        # `readiness_gate._repair_readiness_gate`.
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
        self.assertEqual(review_session.session_id.value, payload["review_session_id"])
        # Only the existing review session needed to run.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(plans.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"])

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
        self.assertEqual("max-sessions", payload["review_skipped_reason"])
        # Only one implementation turn ran; the loop stopped before any
        # review session could be appended.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            plans.SESSION_KIND_IMPLEMENTATION,
            payload["turns"][0]["session_kind"],
        )
        # No review session file should have been created.
        review_candidates = [
            path
            for path in self.plan_dir.glob("session-*.md")
            if review_session_contract.is_generated_review_session_file(path)
        ]
        self.assertEqual([], review_candidates)

    def test_review_that_ran_is_attested_even_when_the_plan_breaks(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_BREAK_PLAN": "1"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        # The review turn ran to completion; only the commit that records it
        # was never reached.
        self.assertEqual("2", payload["review_session_executed"])
        self.assertFalse(payload["review_completed"])
        self.assertIsNone(payload["review_skipped_reason"])
        verdict = payload["review_verdict"]
        self.assertEqual("all-clear", verdict["outcome"])
        self.assertEqual(0, verdict["total_findings"])
        self.assertIsNotNone(payload["review_output_sha256"])
        self.assertIsNotNone(payload["review_artifact_sha256"])

    def test_review_that_never_ran_is_still_reported_as_skipped(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(review_harness="claude", max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIsNone(payload["review_session_executed"])
        self.assertEqual("max-sessions", payload["review_skipped_reason"])
        self.assertIsNone(payload["review_verdict"])

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
            str(review_session_contract.final_review_output_path(canonical_plan_dir)),
            created_payload["review_output_path"],
        )

    def test_review_scope_event_records_the_full_scope_fallback(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        events_log = self._read_events()
        scope_events = [
            entry
            for entry in events_log
            if entry.get("name") == events.REVIEW_SCOPE_EVENT
        ]
        self.assertEqual(1, len(scope_events))
        scope_payload = scope_events[0]["payload"]
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, scope_payload["scope"])
        self.assertIsNone(scope_payload["base_sha"])
        self.assertEqual("no-prior-repair-cycle", scope_payload["fallback_reason"])
        created_payload = next(
            entry["payload"]
            for entry in events_log
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        )
        self.assertEqual(
            review_session_body.REVIEW_SCOPE_FULL, created_payload["review_scope"]
        )
        self.assertIsNone(created_payload["review_base_sha"])
        review_body = (
            self.plan_dir / f"session-{payload['review_session_id']}.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("## Delta Review Scope", review_body)

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

    def test_appending_the_review_session_regates_the_grown_plan(self) -> None:
        # The audit's complaint: a reused readiness report saying every session
        # was complete and no turns were required authorized a turn on a session
        # that report never saw. The re-gate is what makes the recorded
        # authority describe the plan that actually ran.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        gate = payload["readiness_gate_reopened"]
        self.assertEqual(events.REVIEW_SESSION_CREATED_EVENT, gate["trigger"])
        self.assertTrue(gate["ok"])
        # The appended review session is the one pending session the startup
        # gate could not have counted.
        self.assertEqual(1, gate["pending_sessions"])
        self.assertEqual(2, gate["total_sessions"])
        # No repair children were materialized, so naming any here would be a
        # different growth than the one that happened.
        self.assertEqual([], gate["repair_session_ids"])

    def test_a_reused_review_session_is_not_regated(self) -> None:
        # It was authorized when it was created; re-gating it would restate an
        # authority that already exists and blur which growth was licensed.
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertNotIn("readiness_gate_reopened", payload)

    def test_resumed_delta_review_is_rescoped_for_a_late_untracked_file(
        self,
    ) -> None:
        """A crash-resumed delta review must inspect files added since."""
        finding = _structured_review_finding()
        write_completed_review_plan(
            self.plan_dir,
            [finding],
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}'
                )
            },
        )
        base = git_stdout(self.repo_root, "rev-parse", "HEAD")
        events.append_event(
            self._events_path(),
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                    "review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                    "fingerprints": [f"finding:{finding['id']}"],
                    "repo_head": base,
                    "worktree_dirty": False,
                    "dirty_paths": [],
                },
            ),
        )
        delta, fallback_reason = review_delta._compute_review_delta_context(
            event_path=self._events_path(),
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
        )
        self.assertIsNone(fallback_reason)
        # The prior run wrote its delta review against a clean tree, then died
        # before executing it.
        review_session, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude", delta=delta
        )
        self.assertTrue(created)
        self.assertIn(
            f"No files changed since `{base}`",
            review_session.path.read_text(encoding="utf-8"),
        )
        late = self.repo_root / "src" / "late.py"
        late.parent.mkdir(parents=True, exist_ok=True)
        late.write_text("late\n", encoding="utf-8")
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(review_session.session_id.value, payload["review_session_id"])
        self.assertEqual(1, len(payload["turns"]))
        resumed_body = review_session.path.read_text(encoding="utf-8")
        self.assertIn("  - `src/late.py`", resumed_body)
        flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
        self.assertIn(f"git diff {flags} {base}", resumed_body)
        scope_events = [
            entry["payload"]
            for entry in self._read_events()
            if entry["name"] == events.REVIEW_SCOPE_EVENT
        ]
        self.assertEqual(1, len(scope_events))
        self.assertEqual(
            review_session_body.REVIEW_SCOPE_DELTA, scope_events[0]["scope"]
        )
        self.assertEqual(base, scope_events[0]["base_sha"])
        self.assertIsNone(scope_events[0]["fallback_reason"])
        self.assertIs(True, scope_events[0]["refreshed"])

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
        self.assertEqual("pending", plans.read_session_status(review_session).status)
        self.assertEqual(1, payload["final_snapshot"]["counts"]["pending"])
        guard = payload["turns"][0].get("review_mutation_guard")
        self.assertIsNotNone(guard)
        self.assertFalse(guard["passed"])
        self.assertEqual("review-mutated-worktree", guard["reason"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual([], guard["excluded"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])
        self.assertFalse(guard["changed_paths"]["head_changed"])
        self.assertEqual(
            guard["changed_paths"], payload["pause_details"]["changed_paths"]
        )
        self.assertEqual([], payload["pause_details"]["excluded"])
        self.assertIn("README.md", payload["pause_message"])

    def test_review_guard_exclude_flag_allows_excluded_mutation(self) -> None:
        self._write_plan(("complete",), readiness=None)
        review_session = self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(
            review_harness="claude", review_guard_exclude=("README.md",)
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual("complete", plans.read_session_status(review_session).status)
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(["README.md"], guard["excluded"])

    def test_review_guard_exclude_env_var_allows_excluded_mutation(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(
            review_harness="claude",
            extra_env={review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR: "README.md"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(
            ["README.md"], payload["turns"][0]["review_mutation_guard"]["excluded"]
        )

    def test_review_guard_exclude_merges_env_var_and_flag(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        # The tracked README is excluded by the flag; the untracked file under
        # notes/ only by the env var, so the run completes only if both merge.
        self._install_mutating_fake_claude(("README.md", "notes/scratch.md"))

        completed, payload = self._run(
            review_harness="claude",
            review_guard_exclude=("README.md",),
            extra_env={review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR: "notes"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(
            ["README.md", "notes"],
            payload["turns"][0]["review_mutation_guard"]["excluded"],
        )

    def test_review_guard_exclude_still_trips_on_other_paths(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(
            review_harness="claude", review_guard_exclude=("notes",)
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertEqual(["notes"], guard["excluded"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])

    def _commit_gitignore(self, text: str) -> None:
        (self.repo_root / ".gitignore").write_text(text, encoding="utf-8")
        for argv in (
            ["git", "add", ".gitignore"],
            ["git", "commit", "-m", "ignore build caches"],
        ):
            subprocess.run(argv, cwd=self.repo_root, capture_output=True, check=True)

    def _write_pycache_review_plan(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._commit_gitignore("__pycache__/\n")
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude(("agent_session_loop/__pycache__/x.pyc",))

    def test_review_turn_writing_a_gitignored_cache_still_passes(self) -> None:
        self._write_pycache_review_plan()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertTrue(
            (self.repo_root / "agent_session_loop/__pycache__/x.pyc").exists()
        )
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["passed"])
        self.assertFalse(guard["include_ignored"])

    def test_include_ignored_flag_trips_the_guard_on_a_cache_write(self) -> None:
        self._write_pycache_review_plan()

        completed, payload = self._run(
            review_harness="claude", review_guard_include_ignored=True
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["include_ignored"])
        self.assertEqual(
            ["agent_session_loop/__pycache__/x.pyc"],
            guard["changed_paths"]["untracked_added"],
        )

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

        completed, payload = self._run(mode="failure", review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(plans.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"])

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
            if review_session_contract.is_generated_review_session_file(path)
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
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
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
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
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
                    review.REVIEW_SESSION_MARKER,
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
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
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
        review_text = (self.plan_dir / review_id.filename).read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_text_output_prints_review_path(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, _ = self._run(review_harness="claude", json_mode=False)

        self.assertEqual(0, completed.returncode, completed.stderr)
        expected_path = review_session_contract.final_review_output_path(
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
        write_plan_with_ids(self.plan_dir, statuses, contracts_by_id=contracts_by_id)
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
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.write_text(
            json.dumps({name: {"executable": path} for name, path in mapping.items()}),
            encoding="utf-8",
        )

    def _commit_log_env(self) -> tuple[Path, dict[str, str]]:
        """Record every ``asl commit`` message turn this run drives.

        The commit subprocess resolves its harness from the same
        ``XDG_CONFIG_HOME`` the run already points at the fake fixtures, so
        opting into the log is all a commit-observing test needs.
        """
        log_path = self.tmp_path / "fake-commit.jsonl"
        return log_path, {"FAKE_CODEX_COMMIT_LOG": str(log_path)}

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
        review_path = review_session_contract.final_review_output_path(
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
            [plans.SESSION_KIND_IMPLEMENTATION, plans.SESSION_KIND_REVIEW],
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
            [plans.SESSION_KIND_IMPLEMENTATION, plans.SESSION_KIND_REVIEW],
            [turn["session_kind"] for turn in turns],
        )

    def test_review_verdict_persisted_for_counted_findings(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        for argv in (["git", "add", "README.md"], ["git", "commit", "-m", "seed"]):
            subprocess.run(
                argv,
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
        _commit_log, commit_env = self._commit_log_env()
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
                **commit_env,
                "FAKE_CODEX_REVIEW_BODY": review_body,
                "FAKE_CODEX_REVIEW_JSON": review_json,
            },
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
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
        # The findings the run could not close keep it from claiming success.
        self.assertFalse(payload["ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        self.assertEqual(5, payload["action_required"]["unresolved_findings"])

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

    def _setup_review_with_broad_gate_waiting_for_repair(
        self,
    ) -> plans.SessionFile:
        """Create a broad gate that fails until the first repair child exists."""
        repair_path = self.plan_dir / "session-2.1.md"
        check = {
            "type": "command",
            "name": "repair-child-exists",
            "review_replay": True,
            "argv": [
                sys.executable,
                "-c",
                (
                    "import pathlib,sys; "
                    "sys.exit(0 if pathlib.Path("
                    f"{str(repair_path)!r}).is_file() else 1)"
                ),
            ],
        }
        write_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={"1": _contract_block(json.dumps(check), session="1")},
        )
        plans.append_progress_entry(
            self.plan_dir,
            plans.ProgressEntry(
                session_id=plans.parse_session_id("1"),
                status="complete",
                completed_at="2026-06-19T15:00:01Z",
                files_modified=(),
                key_decisions=None,
                discovered_facts=(),
                open_questions=(),
                blockers=None,
            ),
        )
        review_session = self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        return review_session

    def test_findings_defer_broad_failure_until_repair_and_all_clear(self) -> None:
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        review_turns = [
            turn
            for turn in payload["turns"]
            if turn["session_kind"] == plans.SESSION_KIND_REVIEW
        ]
        self.assertEqual(2, len(review_turns))
        first_verification = review_turns[0]["verification"]
        self.assertFalse(first_verification["passed"])
        self.assertEqual(
            verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON,
            first_verification["acceptance"]["reason"],
        )
        self.assertEqual(
            ["broad:1:repair-child-exists"],
            [failure["name"] for failure in first_verification["deferred_failures"]],
        )
        finished = [
            record["payload"]
            for record in self._read_events()
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
            and record["payload"].get("acceptance")
        ]
        self.assertEqual(1, len(finished))
        self.assertFalse(finished[0]["passed"])
        self.assertEqual([], payload["commits"])
        self.assertFalse(commit_log.exists())

    def test_a_stopped_run_cannot_leave_a_deferred_broad_failure_green(self) -> None:
        # ASI-133 reproducer. The review defers its failing broad gate against
        # a repair loop, and the stop is exactly what keeps that loop from ever
        # running. The ending is legitimate; the success is not. Before terminal
        # adjudication this exited 0 reporting ``stopped`` with the plan's own
        # required gate still failing and nothing naming it.
        self._setup_review_with_broad_gate_waiting_for_repair()
        self._queue_stop_after_current()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        adjudication = payload["terminal_adjudication"]
        self.assertTrue(adjudication["claimed_ok"])
        self.assertEqual("stopped", adjudication["claimed_reason"])
        unresolved = payload["pause_details"]["deferred_unresolved"]
        self.assertEqual(
            ["broad:1:repair-child-exists"],
            sorted(check for entry in unresolved for check in entry["checks"]),
        )
        self.assertEqual(["2"], [entry["session_id"] for entry in unresolved])
        self.assertEqual(
            pause_policy.USER_ACTION_INSPECT,
            payload["action_required"]["user_action"],
        )

    def test_a_resumed_run_that_redeems_nothing_keeps_the_same_blocker(self) -> None:
        # The blocker lives in the append-only event log, so a resumed run
        # re-derives it instead of inheriting a forgotten one — and stopping
        # again redeems nothing, so the same deferral blocks the same way.
        self._setup_review_with_broad_gate_waiting_for_repair()
        self._queue_stop_after_current()
        first_completed, first = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)

        self._queue_stop_after_current()
        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        self.assertEqual(
            first["pause_details"]["deferred_unresolved"][0]["checks"],
            payload["pause_details"]["deferred_unresolved"][0]["checks"],
        )

    def test_a_resumed_run_clears_the_blocker_once_a_review_comes_back_clear(
        self,
    ) -> None:
        # The redeeming half: the resumed run executes the repair the deferral
        # was promised against, the next review cycle comes back all-clear, and
        # only then may the plan report itself complete.
        self._setup_review_with_broad_gate_waiting_for_repair()
        self._queue_stop_after_current()
        blocked_completed, blocked = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )
        self.assertEqual(1, blocked_completed.returncode, blocked_completed.stderr)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            blocked["pause_reason"],
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "all-clear"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertFalse(payload["terminal_adjudication"]["blocked"])
        self.assertEqual("all-clear", payload["review_cycles"][-1]["disposition"])

    def test_an_exhausted_repair_budget_cannot_strand_the_deferral_either(
        self,
    ) -> None:
        # ASI-133's second shape, observed on 20260825T152058432989Z-6755: the
        # run refuses success on its own (``review-findings-unresolved``), so
        # nothing here is dishonest — but the deferral it opened is still owed
        # and a plain failure leaves nobody to redeem it. Adjudicating the
        # failure path turns that into a blocker a resume re-evaluates.
        self._setup_review_with_broad_gate_waiting_for_repair()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            max_review_fix_cycles=1,
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings,findings"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        adjudication = payload["terminal_adjudication"]
        self.assertFalse(adjudication["claimed_ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            adjudication["claimed_reason"],
        )
        # The narrower reason the review path recorded survives the pause.
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            payload["action_required"]["reason"],
        )
        self.assertEqual(
            ["broad:1:repair-child-exists"],
            sorted(
                check
                for entry in payload["pause_details"]["deferred_unresolved"]
                for check in entry["checks"]
            ),
        )

    def test_generated_repair_commits_under_null_ordinary_mode(self) -> None:
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_REPAIR_MUTATION_PATH": "repair.txt",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIsNone(payload["commit_mode"])
        self.assertEqual(
            run_selection.REPAIR_COMMIT_MODE_PER_CHILD, payload["repair_commit_mode"]
        )
        self.assertEqual(1, len(read_jsonl(commit_log)))
        self.assertEqual(
            ["2.1"], [commit["session_id"] for commit in payload["commits"]]
        )
        self.assertEqual(
            "",
            git_stdout(self.repo_root, "status", "--porcelain"),
        )
        self.assertEqual(
            "repair.txt",
            git_stdout(self.repo_root, "show", "--format=", "--name-only", "HEAD"),
        )

    def test_generated_repair_skips_runner_commit_after_worker_commit(self) -> None:
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_REPAIR_MUTATION_PATH": "repair.txt",
                "FAKE_CODEX_REPAIR_COMMIT": "1",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], payload["commits"])
        self.assertFalse(commit_log.exists())
        repair_turn = next(
            turn for turn in payload["turns"] if turn["session_id"] == "2.1"
        )
        self.assertEqual("no-changes", repair_turn["repair_commit"]["result"])
        changed = repair_turn["workspace_write_guard"]["changed"]
        self.assertTrue(next(iter(changed.values()))["head_changed"])

    def test_generated_repair_dirty_start_pauses_with_path_evidence(self) -> None:
        # The review turn leaves a guard-excluded file behind, so the repair
        # child inherits a dirty worktree it did not author. The preflight has
        # to name that path and stop before the child is ever dispatched.
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            review_guard_exclude=("dirty.txt",),
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_REVIEW_MUTATION_PATH": "dirty.txt",
            },
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual("2.1", payload["pause_details"]["session_id"])
        self.assertEqual(
            run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            payload["pause_details"]["repair_commit_mode"],
        )
        dirty = payload["pause_details"]["dirty_repositories"]
        self.assertEqual(["dirty.txt"], dirty[0]["dirty_paths"])
        self.assertEqual(["2"], [turn["session_id"] for turn in payload["turns"]])
        self.assertFalse(commit_log.exists())
        self.assertEqual(
            "pending",
            plans.read_session_status(
                plans.SessionFile(
                    plans.parse_session_id("2.1"),
                    self.plan_dir / "session-2.1.md",
                )
            ).status,
        )
        # The one payload has to agree with itself: no answer clears a dirty
        # worktree, so the pause asks for an inspection and names the fix, and
        # the continuation refuses the child it just declined to dispatch
        # instead of reporting it eligible (ASI-114 / ASI-6).
        action = payload["action_required"]
        self.assertEqual("commit-preflight", action["reason"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, action["user_action"])
        self.assertTrue(action["remediation"])
        resume = payload["review_resume"]
        self.assertEqual("run-repairs", resume["action"])
        self.assertFalse(resume["eligible"])
        self.assertEqual("commit-preflight", resume["blocker"])
        self.assertEqual(["2.1"], resume["pending_repair_session_ids"])

    def test_all_clear_cannot_defer_a_broad_failure(self) -> None:
        review_session = self._setup_review_with_broad_gate_waiting_for_repair()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "all-clear"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("verify-failed", payload["pause_reason"])
        verification_payload = payload["turns"][0]["verification"]
        self.assertFalse(verification_payload["passed"])
        self.assertNotIn("acceptance", verification_payload)
        self.assertEqual("pending", plans.read_session_status(review_session).status)

    def test_v2_review_missing_structured_artifact_pauses_run(self) -> None:
        review_session = self._setup_complete_plan_with_pending_review()
        # Valid markdown review, but no final-review.json: the generated v2
        # contract must fail closed.
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            self._valid_final_review_body(), encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # The turn ran, so the run attests it — but attestation reads the
        # verdict under the same artifact requirement the run enforced, so a
        # verdict already refused as invalid is never projected back.
        self.assertEqual(review_session.id, payload["review_session_executed"])
        self.assertIsNone(payload["review_verdict"])
        self.assertIsNone(payload["review_artifact_sha256"])
        self.assertNotIn("acceptance", payload["turns"][0]["verification"])
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_v2_review_contradictory_structured_artifact_pauses_run(
        self,
    ) -> None:
        review_session = self._setup_complete_plan_with_pending_review()
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_agent_session_loop.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        # All-clear artifact contradicts the findings status line.
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
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
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
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
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
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

    def _review_verdict_retries(self) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in self._read_events()
            if record.get("name") == events.REVIEW_VERDICT_RETRY_EVENT
        ]

    def test_invalid_review_verdict_retries_in_run_then_completes(self) -> None:
        review_session = self._setup_complete_plan_with_pending_review()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-artifact,all-clear"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("all-clear", payload["review_verdict"]["outcome"])
        retries = self._review_verdict_retries()
        self.assertEqual(1, len(retries))
        self.assertEqual(1, retries[0]["attempt"])
        self.assertEqual(
            review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS,
            retries[0]["max_attempts"],
        )
        self.assertEqual("malformed-review-artifact", retries[0]["failure_reason"])
        self.assertTrue(str(retries[0]["fingerprint"]).startswith("review-verdict:"))
        self.assertIn("bogus", retries[0]["error"])
        # The retry costs a turn, not a human relaunch.
        self.assertEqual(2, len(payload["turns"]))
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", review_text)
        self.assertIn("review verdict validation failed", review_text)

    def test_persistently_invalid_review_verdict_pauses_when_exhausted(
        self,
    ) -> None:
        review_session = self._setup_complete_plan_with_pending_review()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                "FAKE_CODEX_REVIEW_OUTCOMES": (
                    "invalid-artifact,invalid-artifact-2,invalid-artifact-3"
                )
            },
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertTrue(details["review_verdict_repair_exhausted"])
        self.assertEqual(3, details["review_verdict_repair_attempt"])
        self.assertEqual(
            review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS,
            details["review_verdict_repair_max_attempts"],
        )
        self.assertEqual("malformed-review-artifact", details["failure_reason"])
        retries = self._review_verdict_retries()
        self.assertEqual([1, 2], [record["attempt"] for record in retries])
        # Distinct validation errors must not collapse into one fingerprint.
        self.assertEqual(2, len({record["fingerprint"] for record in retries}))
        self.assertEqual(3, len(payload["turns"]))
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_repeated_review_verdict_error_pauses_on_second_failure(
        self,
    ) -> None:
        # A clamped outcome list replays the same defect, so the second
        # rejection carries a fingerprint the runner has already seen.
        self._setup_complete_plan_with_pending_review()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-artifact"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertTrue(details["review_verdict_repair_exhausted"])
        self.assertEqual(2, details["review_verdict_repair_attempt"])
        retries = self._review_verdict_retries()
        self.assertEqual(1, len(retries))
        self.assertEqual(
            retries[0]["fingerprint"], details["review_verdict_fingerprint"]
        )
        self.assertEqual(2, len(payload["turns"]))

    def test_review_verdict_retry_still_trips_mutation_guard(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_session = self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude(invalid_first_attempt=True)

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(1, len(self._review_verdict_retries()))
        guard = payload["turns"][1]["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])
        self.assertEqual("pending", plans.read_session_status(review_session).status)

    def test_stop_after_current_converts_review_verdict_retry_to_pause(
        self,
    ) -> None:
        self._setup_complete_plan_with_pending_review()
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-artifact,all-clear"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertFalse(details["review_verdict_repair_exhausted"])
        self.assertEqual(
            "stop-after-current", details["review_verdict_repair_declined"]
        )
        self.assertEqual([], self._review_verdict_retries())
        self.assertEqual(1, len(payload["turns"]))

    def _write_reviewed_plan(self, findings: list[dict[str, Any]]) -> None:
        """Author a finished plan whose completed review carries ``findings``."""
        write_completed_review_plan(
            self.plan_dir,
            findings,
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="complete",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _queue_stop_after_current(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )

    def _generated_review_sessions(self) -> list[Path]:
        return [
            path
            for path in sorted(self.plan_dir.glob("session-*.md"))
            if review_session_contract.is_generated_review_session_file(path)
        ]

    def test_stop_after_current_prevents_the_generated_review(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual("stopped", payload["review_skipped_reason"])
        self.assertEqual("initial-review", payload["review_resume"]["action"])
        self.assertEqual(
            [plans.SESSION_KIND_IMPLEMENTATION],
            [turn["session_kind"] for turn in payload["turns"]],
        )
        self.assertEqual([], self._generated_review_sessions())

        resumed_completed, resumed = self._run(review_harness="claude")

        self.assertEqual(0, resumed_completed.returncode, resumed_completed.stderr)
        self.assertTrue(resumed["review_completed"])
        self.assertEqual("2", resumed["review_session_id"])

    def test_stop_after_current_suppresses_repair_materialization(
        self,
    ) -> None:
        self._write_reviewed_plan([_structured_review_finding()])
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([], payload["turns"])
        self.assertFalse((self.plan_dir / "session-2.1.md").is_file())

    def test_stop_after_current_during_review_still_materializes_repairs(
        self,
    ) -> None:
        # Accepted asymmetry: a stop landing *during* the review turn does not
        # unwind the cycle the turn just completed, so the repair children are
        # written; the loop then stops before executing any of them.
        self._setup_complete_plan_with_pending_review()
        self._queue_stop_after_current()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(1, len(payload["turns"]))
        repair = self.plan_dir / "session-2.1.md"
        self.assertTrue(repair.is_file())
        self.assertEqual(
            "pending",
            plans.read_session_status(
                plans.SessionFile(plans.parse_session_id("2.1"), repair)
            ).status,
        )

    def test_stop_after_current_keeps_a_terminal_plan_all_complete(
        self,
    ) -> None:
        self._write_reviewed_plan([])
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual([], payload["turns"])

    def test_stop_after_current_reports_a_capped_plan_unresolved(self) -> None:
        self._write_reviewed_plan([_structured_review_finding()])
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude", max_review_fix_cycles=0)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        self.assertEqual("higher-cap-required", payload["review_resume"]["blocker"])
        self.assertEqual([], payload["turns"])
        self.assertFalse((self.plan_dir / "session-2.1.md").is_file())

    def test_review_verdict_retry_preserves_note_across_rescope(self) -> None:
        finding = _structured_review_finding()
        write_completed_review_plan(
            self.plan_dir,
            [finding],
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}'
                )
            },
        )
        base = git_stdout(self.repo_root, "rev-parse", "HEAD")
        events.append_event(
            self._events_path(),
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                    "review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                    "fingerprints": [f"finding:{finding['id']}"],
                    "repo_head": base,
                    "worktree_dirty": False,
                    "dirty_paths": [],
                },
            ),
        )
        delta, fallback_reason = review_delta._compute_review_delta_context(
            event_path=self._events_path(),
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
        )
        self.assertIsNone(fallback_reason)
        review_session, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude", delta=delta
        )
        self.assertTrue(created)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            # Cycle 1's review already completed, so it consumes the first
            # scripted outcome; this delta review starts at the second.
            extra_env={
                "FAKE_CODEX_REVIEW_OUTCOMES": ("all-clear,invalid-artifact,all-clear")
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(1, len(self._review_verdict_retries()))
        resumed_body = review_session.path.read_text(encoding="utf-8")
        self.assertIn("## Delta Review Scope", resumed_body)
        self.assertIn("review verdict validation failed", resumed_body)

    def test_v1_pending_review_is_refreshed_before_execution(self) -> None:
        # Existing pending v1 review sessions are upgraded to the current
        # generated contract before the harness sees them.
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
                    review.REVIEW_SESSION_MARKER,
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
        # The first attempt writes a status line whose severity counts do not
        # add up. The refreshed contract rejects it immediately.
        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-markdown,all-clear"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual("verify-failed", payload["pause_reason"])
        refreshed = (self.plan_dir / review_id.filename).read_text(encoding="utf-8")
        self.assertIn(review.review_session_version_marker(), refreshed)
        contract = contracts.parse_completion_contract(refreshed, session_id=review_id)
        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
            ],
            [check.name for check in contract.verify.checks],
        )
        self.assertIn("- **Status**: pending", refreshed)

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
            [plans.SESSION_KIND_IMPLEMENTATION, plans.SESSION_KIND_REVIEW],
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
            plans.SESSION_KIND_IMPLEMENTATION, payload["turns"][0]["session_kind"]
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
                review_session_contract.final_review_output_path(
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
        self.assertEqual(plans.SESSION_KIND_REVIEW, turns[2]["session_kind"])

    def test_commit_per_session_skips_review_turn(self) -> None:
        # --commit-per-session must commit after implementation sessions but
        # never after the generated review session.
        self._write_plan(("pending",), readiness="loop-ready")
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            commit_per_session=True,
            extra_env={
                # The implementation turn has to leave a real change behind:
                # `asl commit` refuses a clean worktree. Rewriting the same
                # bytes on the review turn is a no-op, so the review guard
                # still sees an untouched worktree afterwards.
                **commit_env,
                "FAKE_CODEX_MUTATION_PATH": "implemented.txt",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        # Exactly one auto-commit fired: for the implementation session.
        commit_records = read_jsonl(commit_log)
        self.assertEqual(1, len(commit_records))
        self.assertEqual(str(self.repo_root.resolve()), commit_records[0]["cwd"])
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
            self.assertEqual(plans.SESSION_KIND_IMPLEMENTATION, turn["session_kind"])
        # No generated review session file should exist.
        review_candidates = [
            path
            for path in self.plan_dir.glob("session-*.md")
            if review_session_contract.is_generated_review_session_file(path)
        ]
        self.assertEqual([], review_candidates)
        # No final-review.md should be written either.
        self.assertFalse(
            review_session_contract.final_review_output_path(self.plan_dir).is_file()
        )
        # No review-session-created event should have been emitted.
        review_events = [
            entry
            for entry in self._read_events()
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        ]
        self.assertEqual([], review_events)


class RevertSnapshotCacheIntegrationTests(unittest.TestCase):
    """A status revert must leave the run-owned snapshot cache usable.

    The revert rewrites the session file, so a re-read that bypasses the cache
    strands it on stale stamps and forces the review-continuation projection at
    loop exit to inspect the whole plan a second time.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        fake_codex = install_fake_codex(self.tmp_path)
        fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(fake_codex)},
                    "claude": {"executable": str(fake_claude)},
                }
            ),
            encoding="utf-8",
        )

    def _run_locked_counting_inspections(
        self,
    ) -> tuple[dict[str, Any], list[Path], list[int]]:
        """Drive one `_run_locked` turn, counting every `inspect_plan` call.

        Returns the run payload, the plan directories inspected in order, and
        the running count observed right after each post-revert re-inspection.
        """

        def complete_first_pending(plan_dir: Path) -> None:
            path = plan_dir / "session-1.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            complete_first_pending(Path(str(kwargs["plan_dir"])))
            return harnesses.HarnessResult(
                name=name,
                argv=[name],
                returncode=0,
                stdout="",
                stderr="",
            )

        inspected: list[Path] = []
        real_inspect_plan = plans.inspect_plan

        def counting_inspect_plan(plan_dir):
            inspected.append(plans.canonicalize_path(plan_dir))
            return real_inspect_plan(plan_dir)

        counts_after_revert: list[int] = []
        real_reinspect = session_verification._reinspect_after_revert

        def recording_reinspect(*args, **kwargs):
            snapshot = real_reinspect(*args, **kwargs)
            counts_after_revert.append(len(inspected))
            return snapshot

        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            review_harness="claude",
            review_harness_model=None,
            state_dir=str(self.state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                PYTHONPATH=str(REPO_ROOT)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            ),
            mock.patch.object(plans, "inspect_plan", counting_inspect_plan),
            mock.patch.object(harnesses, "run_harness", side_effect=fake_run_harness),
            mock.patch.object(
                session_verification, "_reinspect_after_revert", recording_reinspect
            ),
        ):
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                _exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-revert-cache",
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        return payload, inspected, counts_after_revert

    def test_verification_revert_leaves_final_review_projection_cached(
        self,
    ) -> None:
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "MISSING.md", '
                    '"name": "missing-file"}',
                    session="1",
                )
            },
        )

        payload, inspected, counts_after_revert = (
            self._run_locked_counting_inspections()
        )

        self.assertEqual("verify-failed", payload["pause_reason"])
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        # The final continuation projection ran at loop exit.
        self.assertIsNotNone(payload["review_resume"])
        # Exactly one revert re-inspection, and nothing inspected the plan
        # again afterwards: the projection's `plan_cache.get()` was a hit.
        self.assertEqual(1, len(counts_after_revert))
        self.assertEqual(counts_after_revert[0], len(inspected))


class ReviewGuardUnavailableRunTests(unittest.TestCase):
    """A guard that cannot see the worktree must stop the run, not wave it on."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        fake_codex = install_fake_codex(self.tmp_path)
        fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(fake_codex)},
                    "claude": {"executable": str(fake_claude)},
                }
            ),
            encoding="utf-8",
        )
        write_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        self.review_session = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )[0]

    _UNAVAILABLE = {
        "git": True,
        "unavailable": {
            "reason": "git-command-failed",
            "argv": ["ls-files", "--others", "--exclude-standard"],
            "returncode": 128,
            "stderr": "fatal: broken index",
        },
    }
    _CLEAN = {
        "git": True,
        "head": "0" * 40,
        "diff_sha256": hashlib.sha256(b"").hexdigest(),
        "tracked_changed": [],
        "tracked_patches": [],
        "untracked": [],
    }

    def _run_locked(
        self, fingerprints: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[str]]:
        """Drive one run with `fingerprints` returned by successive captures."""
        harness_calls: list[str] = []

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            harness_calls.append(name)
            path = Path(str(kwargs["plan_dir"])) / self.review_session.path.name
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )
            return harnesses.HarnessResult(
                name=name, argv=[name], returncode=0, stdout="", stderr=""
            )

        args = cli.build_parser().parse_args(
            [
                "run",
                str(self.plan_dir),
                "--cwd",
                str(self.repo_root),
                "--harness",
                "codex",
                "--review-harness",
                "claude",
                "--state-dir",
                str(self.state_dir),
                "--allow-not-loop-ready",
                # A completing run would otherwise inherit `claude` as its audit
                # role and add a harness call these assertions do not describe.
                "--no-supervisor-audit",
            ]
        )
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                PYTHONPATH=str(REPO_ROOT)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            ),
            mock.patch.object(harnesses, "run_harness", side_effect=fake_run_harness),
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                side_effect=fingerprints,
            ),
        ):
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                _exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-guard-unavailable",
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        return payload, harness_calls

    def test_unavailable_before_capture_pauses_without_spending_the_turn(
        self,
    ) -> None:
        payload, harness_calls = self._run_locked([self._UNAVAILABLE])

        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, payload["pause_reason"]
        )
        self.assertEqual([], harness_calls)
        self.assertEqual([], payload["turns"])
        self.assertEqual(
            "pending",
            plans.read_session_status(self.review_session).status,
        )
        guard = payload["pause_details"]["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("before", guard["phase"])
        self.assertEqual("git-command-failed", guard["unavailable"]["reason"])

    def test_unavailable_after_capture_reverts_the_review_to_pending(
        self,
    ) -> None:
        payload, harness_calls = self._run_locked([self._CLEAN, self._UNAVAILABLE])

        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, payload["pause_reason"]
        )
        self.assertEqual(["claude"], harness_calls)
        self.assertFalse(payload["review_completed"])
        self.assertEqual(
            "pending",
            plans.read_session_status(self.review_session).status,
        )
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("after", guard["phase"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual(guard, payload["pause_details"]["review_mutation_guard"])
        self.assertIn("git-command-failed", payload["pause_message"])

    def test_clean_captures_still_complete_the_review(self) -> None:
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- Reviewed agent_session_loop/runner.py for guard behavior.\n"
            "- Checked tests/test_asl_review_lifecycle.py for coverage.\n"
            "- Compared docs/agent-session-loop.md for operator impact.\n"
            "Status: All clear, no findings\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )

        payload, harness_calls = self._run_locked([self._CLEAN, self._CLEAN])

        self.assertFalse(payload["paused"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual(["claude"], harness_calls)
        self.assertTrue(payload["turns"][0]["review_mutation_guard"]["passed"])


class ReviewGuardFailedTurnOutcomeTests(unittest.TestCase):
    """A failed review turn still has to answer for what it did to the worktree.

    Every unsuccessful outcome used to pause before the post-turn fingerprint
    was captured, so a review that mutated the repository and then timed out
    was relaunched by the supervisor against its own mutation.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        fake_codex = install_fake_codex(self.tmp_path)
        fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(fake_codex)},
                    "claude": {"executable": str(fake_claude)},
                }
            ),
            encoding="utf-8",
        )
        write_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        self.review_session = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )[0]

    def _run_locked(
        self,
        *,
        returncode: int = 0,
        timed_out: bool = False,
        interrupted: bool = False,
        last_message: str = "Runner status: complete\n",
        mutate: bool = True,
        unreadable_plan: bool = False,
        forbid_mailbox_wait: bool = False,
    ) -> dict[str, Any]:
        """Drive one review turn whose harness fails the way `kwargs` describe."""

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            if mutate:
                (self.repo_root / "README.md").write_text(
                    "mutated by review\n", encoding="utf-8"
                )
            plan_dir = Path(str(kwargs["plan_dir"]))
            path = plan_dir / self.review_session.path.name
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )
            if unreadable_plan:
                (plan_dir / "context.md").unlink()
            return harnesses.HarnessResult(
                name=name,
                argv=[name],
                returncode=returncode,
                stdout="",
                stderr="",
                last_message=last_message,
                timed_out=timed_out,
                interrupted=interrupted,
            )

        args = cli.build_parser().parse_args(
            [
                "run",
                str(self.plan_dir),
                "--cwd",
                str(self.repo_root),
                "--harness",
                "codex",
                "--review-harness",
                "claude",
                "--state-dir",
                str(self.state_dir),
                "--allow-not-loop-ready",
            ]
        )
        with ExitStack() as stack:
            stack.enter_context(
                patched_env(
                    XDG_CONFIG_HOME=str(self.config_home),
                    PYTHONPATH=str(REPO_ROOT)
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    harnesses, "run_harness", side_effect=fake_run_harness
                )
            )
            if forbid_mailbox_wait:
                mailbox.write_lease(
                    self.plan_dir,
                    str(uuid.uuid4()),
                    state_dir=str(self.state_dir),
                )
                stack.enter_context(
                    mock.patch.object(
                        run_coordination,
                        "prepare_mailbox_reply_wait",
                        side_effect=AssertionError(
                            "the mailbox reply wait must not be reachable "
                            "after the review mutation guard trips"
                        ),
                    )
                )
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                _exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-guard-turn-outcome",
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        return payload

    def _assert_guard_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Assert the run stopped on the guard and returned the durable record."""
        self.assertTrue(payload["paused"])
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(
            "pending", plans.read_session_status(self.review_session).status
        )
        turn = payload["turns"][0]
        guard = turn["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("review-mutated-worktree", guard["reason"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])
        # The evidence has to survive the process, not just the payload.
        persisted = json.loads(
            Path(str(turn["result_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(guard, persisted["review_mutation_guard"])
        return turn

    def _assert_supervisor_will_not_relaunch(
        self, payload: dict[str, Any], *, suppressed_reason: str
    ) -> None:
        """Assert the pause the supervisor sees is not the retryable one."""
        policy = payload["pause_policy"]
        self.assertFalse(policy["recoverable"])
        self.assertEqual(0, policy["max_retries"])
        self.assertNotIn(
            "review-mutated-worktree", pause_policy.RECOVERABLE_PAUSE_REASONS
        )
        # Without the guard the run would have surfaced this reason instead,
        # and the supervisor would have relaunched onto the mutated tree.
        self.assertIn(suppressed_reason, pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_timed_out_review_turn_trips_the_guard_instead_of_retrying(
        self,
    ) -> None:
        payload = self._run_locked(returncode=124, timed_out=True)

        turn = self._assert_guard_pause(payload)
        self.assertTrue(turn["timed_out"])
        self._assert_supervisor_will_not_relaunch(payload, suppressed_reason="timeout")

    def test_nonzero_exit_review_turn_trips_the_guard(self) -> None:
        payload = self._run_locked(returncode=3)

        turn = self._assert_guard_pause(payload)
        self.assertEqual(3, turn["returncode"])

    def test_runner_pause_review_turn_trips_the_guard_before_any_reply_wait(
        self,
    ) -> None:
        payload = self._run_locked(
            last_message="Runner status: paused\nRUNNER_PAUSE: blocked - needs a "
            "decision\n",
            forbid_mailbox_wait=True,
        )

        self._assert_guard_pause(payload)
        self.assertIsNone(payload.get("waiting_for_reply"))

    def test_interrupted_review_turn_trips_the_guard(self) -> None:
        payload = self._run_locked(
            returncode=turn_execution.INTERRUPTED_EXIT, interrupted=True
        )

        self._assert_guard_pause(payload)

    def test_unreadable_plan_after_a_mutating_review_turn_trips_the_guard(
        self,
    ) -> None:
        payload = self._run_locked(unreadable_plan=True)

        turn = self._assert_guard_pause(payload)
        self.assertFalse(turn["timed_out"])

    def test_a_clean_failed_review_turn_keeps_its_own_pause_reason(self) -> None:
        payload = self._run_locked(returncode=124, timed_out=True, mutate=False)

        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertTrue(payload["turns"][0]["review_mutation_guard"]["passed"])


class ReviewMutationGuardFingerprintTests(unittest.TestCase):
    """Direct coverage for the guard's allowlist and per-path evidence."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        self.plan_dir.mkdir()
        init_git_repo(self.repo_root)

    def _fingerprint(
        self, *exclude: str, include_ignored: bool = False
    ) -> dict[str, Any]:
        return review_guard._capture_review_repo_fingerprint(
            self.repo_root,
            self.plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )

    def _write(self, rel: str, text: str) -> None:
        path = self.repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit_all(self, message: str) -> None:
        for argv in (["git", "add", "-A"], ["git", "commit", "-m", message]):
            subprocess.run(argv, cwd=self.repo_root, capture_output=True, check=True)

    def _stage(self, rel: str) -> None:
        subprocess.run(
            ["git", "add", rel],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def test_excluded_tracked_change_leaves_fingerprint_unchanged(self) -> None:
        before_excluded = self._fingerprint("README.md")
        before_plain = self._fingerprint()

        self._write("README.md", "mutated\n")

        self.assertEqual(before_excluded, self._fingerprint("README.md"))
        self.assertNotEqual(before_plain, self._fingerprint())

    def test_excluded_untracked_file_is_not_fingerprinted(self) -> None:
        before = self._fingerprint("scratch.txt")

        self._write("scratch.txt", "ignored\n")

        after = self._fingerprint("scratch.txt")
        self.assertEqual(before, after)
        self.assertEqual([], after["untracked"])
        self.assertIn(
            "scratch.txt",
            [entry[0] for entry in self._fingerprint()["untracked"]],
        )

    def test_directory_prefix_excludes_tracked_and_untracked_children(self) -> None:
        self._write("claude/settings.json", "{}\n")
        subprocess.run(
            ["git", "add", "claude/settings.json"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add settings"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        before = self._fingerprint("claude")

        self._write("claude/settings.json", '{"mutated": true}\n')
        self._write("claude/cache/session.json", "{}\n")

        self.assertEqual(before, self._fingerprint("claude"))
        # A sibling directory sharing the prefix is still guarded.
        self._write("claude-extra/other.json", "{}\n")
        self.assertNotEqual(before, self._fingerprint("claude"))

    def test_glob_metacharacters_in_an_exclusion_are_matched_literally(
        self,
    ) -> None:
        cases = (
            ("dir[12]", ("dir1", "dir2")),
            ("q?mark.txt", ("qxmark.txt",)),
            ("star*name.txt", ("starANYname.txt",)),
        )
        for literal, siblings in cases:
            for rel in (literal, *siblings):
                self._write(rel, "one\n")
        self._commit_all("add wildcard-named files")
        for literal, siblings in cases:
            for rel in (literal, *siblings):
                self._write(rel, "two\n")

        for literal, siblings in cases:
            with self.subTest(exclusion=literal):
                tracked = self._fingerprint(literal)["tracked_changed"]
                self.assertNotIn(literal, tracked)
                # As a git pattern the exclusion would swallow these too.
                for sibling in siblings:
                    self.assertIn(sibling, tracked)

    def test_star_exclusion_does_not_erase_the_tracked_diff(self) -> None:
        before = self._fingerprint("*")

        self._write("README.md", "mutated\n")
        after = self._fingerprint("*")

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])

    def test_one_exclusion_selects_the_same_tracked_and_untracked_subtree(
        self,
    ) -> None:
        self._write("logs[1]/kept.txt", "one\n")
        self._write("logs1/kept.txt", "one\n")
        self._commit_all("add bracket-named directories")
        before = self._fingerprint("logs[1]")

        self._write("logs[1]/kept.txt", "two\n")
        self._write("logs[1]/scratch.txt", "new\n")

        self.assertEqual(before, self._fingerprint("logs[1]"))

        self._write("logs1/kept.txt", "two\n")
        self._write("logs1/scratch.txt", "new\n")
        after = self._fingerprint("logs[1]")

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["logs1/kept.txt"], changed["tracked"])
        self.assertEqual(["logs1/scratch.txt"], changed["untracked_added"])

    def test_untracked_paths_stay_repo_root_relative_from_a_subdirectory(
        self,
    ) -> None:
        self._write("nested/kept.txt", "one\n")
        self._commit_all("add nested tree")
        self._write("nested/scratch.txt", "new\n")
        nested = self.repo_root / "nested"

        fingerprint = review_guard._capture_review_repo_fingerprint(
            nested, self.plan_dir
        )

        self.assertIsNone(review_guard._review_guard_unavailable_detail(fingerprint))
        self.assertEqual(
            ["nested/scratch.txt"],
            [entry[0] for entry in fingerprint["untracked"]],
        )
        excluded = review_guard._capture_review_repo_fingerprint(
            nested, self.plan_dir, exclude=("nested",)
        )
        self.assertEqual([], excluded["untracked"])
        self.assertEqual([], excluded["tracked_changed"])

    def test_nested_cwd_still_fingerprints_the_whole_repository(self) -> None:
        self._write("inside/tracked.txt", "one\n")
        self._write("outside/tracked.txt", "one\n")
        self._commit_all("add both subtrees")
        # ``diff.relative`` narrows a diff to cwd *and* makes its paths
        # cwd-relative, so it is the sharpest test of the repo-root anchoring.
        subprocess.run(
            ["git", "config", "diff.relative", "true"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        nested = self.repo_root / "inside"
        before = review_guard._capture_review_repo_fingerprint(nested, self.plan_dir)

        self._write("inside/tracked.txt", "two\n")
        self._write("outside/tracked.txt", "two\n")
        self._write("inside/scratch.txt", "new\n")
        self._write("outside/scratch.txt", "new\n")
        after = review_guard._capture_review_repo_fingerprint(nested, self.plan_dir)

        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(
            ["inside/tracked.txt", "outside/tracked.txt"], changed["tracked"]
        )
        self.assertEqual(
            ["inside/scratch.txt", "outside/scratch.txt"],
            changed["untracked_added"],
        )

    def test_ignored_file_change_is_invisible_until_include_ignored(self) -> None:
        self._write(".gitignore", "ignored/\n")
        self._write("ignored/cache.txt", "one\n")
        self._commit_all("ignore a directory")
        before = self._fingerprint()
        strict_before = self._fingerprint(include_ignored=True)

        self._write("ignored/cache.txt", "two\n")

        self.assertEqual(before, self._fingerprint())
        strict_after = self._fingerprint(include_ignored=True)
        self.assertEqual(
            ["ignored/cache.txt"],
            review_guard._review_guard_changed_paths(strict_before, strict_after)[
                "untracked_changed"
            ],
        )

    def test_ignored_symlink_retarget_is_invisible_until_include_ignored(
        self,
    ) -> None:
        self._write(".gitignore", "*.link\n")
        self._commit_all("ignore link files")
        self._write("target-a.txt", "a\n")
        self._write("target-b.txt", "b\n")
        link = self.repo_root / "cache.link"
        link.symlink_to("target-a.txt")
        before = self._fingerprint()
        strict_before = self._fingerprint(include_ignored=True)

        link.unlink()
        link.symlink_to("target-b.txt")

        self.assertEqual(before, self._fingerprint())
        strict_after = self._fingerprint(include_ignored=True)
        self.assertIn(
            ["cache.link", "symlink"],
            [entry[:2] for entry in strict_after["untracked"]],
        )
        self.assertEqual(
            ["cache.link"],
            review_guard._review_guard_changed_paths(strict_before, strict_after)[
                "untracked_changed"
            ],
        )

    def test_self_ignoring_untracked_gitignore_cannot_hide_review_mutation(
        self,
    ) -> None:
        # Git applies a new untracked `.gitignore` before answering the guard's
        # scans, and this one matches itself as well as the file written beside
        # it: deferring to git's ignore rules would let a review turn write the
        # very rule that blinds the guard.
        before = self._fingerprint()

        self._write("generated/.gitignore", "*\n")
        self._write("generated/payload.py", "LEAKED = True\n")
        after = self._fingerprint()

        self.assertEqual("", git_stdout(self.repo_root, "status", "--short"))
        self.assertEqual(
            "",
            git_stdout(self.repo_root, "ls-files", "--others", "--exclude-standard"),
        )
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["generated/.gitignore"], changed["worktree_ignore"])
        self.assertEqual(
            ": generated/.gitignore",
            review_guard._review_guard_changed_summary(changed),
        )
        # The rule the turn wrote is evidence; the payload it hides is the
        # ordinary ignored content the default scan still defers to git about.
        self.assertEqual(
            ["generated/.gitignore"],
            [entry[0] for entry in after["worktree_ignore"]],
        )

    def test_untracked_worktree_ignore_edit_and_deletion_move_the_evidence(
        self,
    ) -> None:
        self._write("generated/.gitignore", "*\n")
        before = self._fingerprint()
        self.assertEqual(
            ["generated/.gitignore"],
            [entry[0] for entry in before["worktree_ignore"]],
        )

        strict_before = self._fingerprint(include_ignored=True)

        self._write("generated/.gitignore", "*\n!keep.txt\n")
        edited = self._fingerprint()

        self.assertEqual(
            ["generated/.gitignore"],
            review_guard._review_guard_changed_paths(before, edited)["worktree_ignore"],
        )
        # The strict scan lists the same file under `untracked`, so the summary
        # would otherwise name it twice.
        self.assertEqual(
            ": generated/.gitignore",
            review_guard._review_guard_changed_summary(
                review_guard._review_guard_changed_paths(
                    strict_before, self._fingerprint(include_ignored=True)
                )
            ),
        )

        (self.repo_root / "generated" / ".gitignore").unlink()
        deleted = self._fingerprint()

        self.assertEqual([], deleted["worktree_ignore"])
        self.assertEqual(
            ["generated/.gitignore"],
            review_guard._review_guard_changed_paths(edited, deleted)[
                "worktree_ignore"
            ],
        )

    def test_ignored_gitignore_symlink_stays_out_of_worktree_ignore_evidence(
        self,
    ) -> None:
        # Git opens an ignore file with `O_NOFOLLOW`, so a symlinked
        # `.gitignore` contributes no rules however it is retargeted — but the
        # negative command-line pattern re-lists it anyway, and fingerprinting
        # the link text would discard a review turn over a mutation git never
        # acted on.
        self._write(".gitignore", "generated/.gitignore\ngenerated/rules-*\n")
        self._commit_all("ignore the generated ignore source and its targets")
        self._write("generated/rules-a", "hidden-a.txt\n")
        self._write("generated/rules-b", "hidden-b.txt\n")
        link = self.repo_root / "generated" / ".gitignore"
        link.symlink_to("rules-a")
        before = self._fingerprint()

        link.unlink()
        link.symlink_to("rules-b")
        after = self._fingerprint()

        self.assertEqual("", git_stdout(self.repo_root, "status", "--short"))
        self.assertEqual(
            "",
            git_stdout(self.repo_root, "ls-files", "--others", "--exclude-standard"),
        )
        self.assertEqual([], after["untracked"])
        self.assertEqual([], after["worktree_ignore"])
        self.assertEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([], changed["worktree_ignore"])
        self.assertEqual("", review_guard._review_guard_changed_summary(changed))

    def test_gitignore_git_cannot_apply_stays_out_of_the_evidence(self) -> None:
        # Git never descends into an ignored directory, so a `.gitignore`
        # inside one is a file whose rules git never applies — and the tool
        # caches that churn every review turn write exactly that. Fingerprinting
        # it would revive the false positives the default scan exists to avoid.
        self._write(".gitignore", "cache/\n")
        self._commit_all("ignore a cache directory")
        before = self._fingerprint()

        self._write("cache/.gitignore", "*\n")
        self._write("cache/blob.bin", "one\n")

        self.assertEqual([], before["worktree_ignore"])
        self.assertEqual(before, self._fingerprint())

    def _info_exclude(self) -> Path:
        return self.repo_root / ".git" / "info" / "exclude"

    def _write_info_exclude(self, text: str) -> None:
        path = self._info_exclude()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _ignore_config(self, fingerprint: dict[str, Any]) -> dict[str, list[str]]:
        entries = fingerprint["ignore_config"]
        assert isinstance(entries, list)
        return {str(rel): [str(kind), str(sha)] for _role, rel, kind, sha in entries}

    def test_git_info_exclude_mutation_trips_the_guard(self) -> None:
        self._write_info_exclude("*.tmp\n")
        before = self._fingerprint()

        self._write_info_exclude("*.tmp\nsecret.txt\n")
        after = self._fingerprint()

        # No diff and no untracked scan reaches inside the git directory, so a
        # turn could rewrite this to hide the path it wrote (as it could the
        # excludes file `core.excludesFile` names).
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/info/exclude"], changed["ignore_config"])
        self.assertEqual(
            ": .git/info/exclude",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_absent_git_info_exclude_still_trips_on_creation(self) -> None:
        self._info_exclude().unlink(missing_ok=True)
        before = self._fingerprint()

        self.assertEqual(
            ["absent", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)[".git/info/exclude"],
        )

        self._write_info_exclude("secret.txt\n")
        after = self._fingerprint()

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/info/exclude"], changed["ignore_config"])

    def test_git_info_exclude_resolves_from_a_nested_cwd(self) -> None:
        # ``rev-parse --git-path`` answers relative to the directory git ran in,
        # so a nested cwd reports ``../.git/info/exclude`` and joining that
        # against the repository root would fingerprint a path that never
        # exists — always absent, never moving.
        self._write_info_exclude("*.tmp\n")
        nested = self.repo_root / "nested"
        nested.mkdir()

        fingerprint = review_guard._capture_review_repo_fingerprint(
            nested, self.plan_dir
        )

        self.assertEqual(
            "file", self._ignore_config(fingerprint)[".git/info/exclude"][0]
        )
        self.assertEqual(
            self._fingerprint()["ignore_config"], fingerprint["ignore_config"]
        )

    def _symlink_info_exclude(self, target: str) -> Path:
        link = self._info_exclude()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        return link.parent / target

    def test_git_info_exclude_symlink_target_edit_trips_the_guard(self) -> None:
        # Git follows a symlinked exclusion source, so evidence that stops at
        # the link text goes quiet when only the target's rules change.
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        before = self._fingerprint()
        self.assertEqual("symlink", self._ignore_config(before)[".git/info/exclude"][0])

        target.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        # The link itself never moved and git hides the file its new rule names.
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["target:.git/info/rules"], changed["ignore_config"])
        self.assertEqual(
            ": target:.git/info/rules",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_dangling_symlinked_source_moves_when_its_target_appears(
        self,
    ) -> None:
        target = self._symlink_info_exclude("rules")
        before = self._fingerprint()

        self.assertEqual(
            ["absent", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)["target:.git/info/rules"],
        )

        target.write_text("secret.txt\n", encoding="utf-8")
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["target:.git/info/rules"], changed["ignore_config"])

    def test_replacing_a_symlinked_source_with_its_target_trips_the_guard(
        self,
    ) -> None:
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        before = self._fingerprint()

        link = self._info_exclude()
        link.unlink()
        link.write_text("*.tmp\n", encoding="utf-8")
        after = self._fingerprint()

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(
            [".git/info/exclude", "target:.git/info/rules"],
            changed["ignore_config"],
        )

    def test_symlink_cycle_in_an_ignore_source_fails_the_guard_closed(
        self,
    ) -> None:
        self._symlink_info_exclude("exclude")

        fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        assert detail is not None
        self.assertEqual("ignore-config-target-unresolved", detail["reason"])
        self.assertNotIn("ignore_config", fingerprint)

    def test_unreadable_symlinked_ignore_target_fails_the_guard_closed(
        self,
    ) -> None:
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        real_read_bytes = Path.read_bytes

        def fake_read_bytes(self_path: Path) -> bytes:
            if self_path.name == "rules":
                raise OSError(errno.EIO, "input/output error")
            return real_read_bytes(self_path)

        with mock.patch.object(Path, "read_bytes", fake_read_bytes):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        assert detail is not None
        self.assertEqual("ignore-config-target-path-unreadable", detail["reason"])
        self.assertEqual("target:.git/info/rules", detail["path"])

    def _set_excludes_file(self, value: str) -> None:
        subprocess.run(
            ["git", "config", "core.excludesFile", value],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def test_core_excludes_file_cannot_hide_a_review_mutation(self) -> None:
        # `--exclude-standard` honours `core.excludesFile` too, so a turn can
        # repoint it at a file it writes under `.git` and every worktree scan
        # goes quiet about the path the new rule hides.
        before = self._fingerprint()

        selected = self.repo_root / ".git" / "review-excludes"
        selected.write_text("secret.txt\n", encoding="utf-8")
        self._set_excludes_file(".git/review-excludes")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertIn("config:core.excludesFile", changed["ignore_config"])
        self.assertIn(".git/review-excludes", changed["ignore_config"])
        self.assertIn(
            "config:core.excludesFile",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_core_excludes_file_trailing_newline_cannot_hide_a_review_mutation(
        self,
    ) -> None:
        # A newline terminates git's own output record but is also a legal
        # pathname byte, so trimming it collapses two different settings — and
        # the two different files they select — onto one fingerprint.
        selected = self.repo_root / ".git" / "review-excludes"
        selected.write_text("*.tmp\n", encoding="utf-8")
        self._set_excludes_file(".git/review-excludes")
        before = self._fingerprint()

        (self.repo_root / ".git" / "review-excludes\n").write_text(
            "secret.txt\n", encoding="utf-8"
        )
        self._set_excludes_file(".git/review-excludes\n")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        # Git honours the newline-terminated pathname, so every worktree scan
        # goes quiet about the file its rule hides.
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertIn("config:core.excludesFile", changed["ignore_config"])
        self.assertIn(".git/review-excludes\n", changed["ignore_config"])
        self.assertIn(
            "config:core.excludesFile",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_effective_excludes_file_preserves_every_pathname_byte(self) -> None:
        # Reading git's answer as text would rewrite `\r` and `\r\n` to `\n`,
        # mapping three distinct settings — and three distinct files — onto one
        # marker, which is the newline evasion one byte over.
        markers = set()
        for value in (".git/x\rex", ".git/x\r\nex", ".git/x\nex"):
            self._set_excludes_file(value)
            marker, selected = review_guard._effective_excludes_file(
                self.repo_root, self.repo_root
            )
            markers.add(marker)
            self.assertEqual(self.repo_root / value, selected)
        self.assertEqual(3, len(markers))

    def test_selected_excludes_file_edit_trips_the_guard(self) -> None:
        # Leaving the setting alone and editing the file it already selects is
        # the same evasion one step later.
        selected = self.repo_root / ".git" / "review-excludes"
        selected.write_text("*.tmp\n", encoding="utf-8")
        self._set_excludes_file(".git/review-excludes")
        before = self._fingerprint()

        selected.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        self.assertEqual(before["untracked"], after["untracked"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/review-excludes"], changed["ignore_config"])

    def _symlink_selected_excludes_file(self, target: str) -> Path:
        link = self.repo_root / ".git" / "review-excludes"
        link.symlink_to(target)
        self._set_excludes_file(".git/review-excludes")
        return link.parent / target

    def test_selected_excludes_file_symlink_target_edit_trips_the_guard(
        self,
    ) -> None:
        # The same evasion through the other source: git reads the target of
        # the link `core.excludesFile` selects, so the link text alone is not
        # evidence of the rules in force.
        target = self._symlink_selected_excludes_file("review-rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        before = self._fingerprint()
        self.assertEqual(
            "symlink", self._ignore_config(before)[".git/review-excludes"][0]
        )

        target.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["target:.git/review-rules"], changed["ignore_config"])
        self.assertIn(
            "target:.git/review-rules",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_stable_symlinked_ignore_sources_leave_the_fingerprint_still(
        self,
    ) -> None:
        # Following the link must not make an untouched configuration look like
        # movement, which would pause every review turn on a linked source.
        info_target = self._symlink_info_exclude("rules")
        info_target.write_text("*.tmp\n", encoding="utf-8")
        selected_target = self._symlink_selected_excludes_file("review-rules")
        selected_target.write_text("*.log\n", encoding="utf-8")

        before = self._fingerprint()

        self.assertEqual(before, self._fingerprint())
        entries = self._ignore_config(before)
        self.assertEqual("file", entries["target:.git/info/rules"][0])
        self.assertEqual("file", entries["target:.git/review-rules"][0])

    def test_ignore_target_label_cannot_collide_with_source_path(self) -> None:
        # `target:` is a legal repo-relative pathname prefix, so an excludes
        # file at `target:.git/info/rules` spells exactly the label a symlinked
        # `.git/info/exclude` resolving to `.git/info/rules` produces. Keyed by
        # name alone the source entry displaced the target entry, and a
        # target-only mutation moved the fingerprint while naming nothing.
        collide = "target:.git/info/rules"
        self._write(collide, "*.log\n")
        self._set_excludes_file(collide)
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")

        before = self._fingerprint()
        colliding = [
            list(entry) for entry in before["ignore_config"] if entry[1] == collide
        ]
        self.assertEqual(
            ["source", "target"], sorted(str(entry[0]) for entry in colliding)
        )
        # Two different files under one label: whichever the name-keyed map kept
        # made the other's content unobservable.
        self.assertNotEqual(colliding[0][2:], colliding[1][2:])

        target.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        # The link text, the setting, and the file the setting selects are all
        # untouched, and git hides the file the new rule names.
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([collide], changed["ignore_config"])
        self.assertIn(collide, review_guard._review_guard_changed_summary(changed))

    def test_dev_null_excludes_file_leaves_the_fingerprint_still(self) -> None:
        self._set_excludes_file("/dev/null")

        before = self._fingerprint()

        self.assertEqual(
            ["unsupported", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)["/dev/null"],
        )
        self.assertEqual(before, self._fingerprint())

    def test_symlinked_dev_null_excludes_file_fails_the_guard_closed(
        self,
    ) -> None:
        # A link is never how `core.excludesFile=/dev/null` switches the global
        # excludes off, so an unsupported target is a state the evidence cannot
        # describe rather than a configuration to tolerate.
        self._symlink_selected_excludes_file("/dev/null")

        fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        assert detail is not None
        self.assertEqual("excludes-file-target-node-unsupported", detail["reason"])
        self.assertEqual("target:/dev/null", detail["path"])

    def test_absent_excludes_file_is_fingerprinted_by_its_own_kind(
        self,
    ) -> None:
        self._set_excludes_file(".git/review-excludes")
        before = self._fingerprint()

        self.assertEqual(
            ["absent", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)[".git/review-excludes"],
        )

        (self.repo_root / ".git" / "review-excludes").write_text(
            "secret.txt\n", encoding="utf-8"
        )
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/review-excludes"], changed["ignore_config"])

    def test_unset_excludes_file_falls_back_to_the_xdg_default(self) -> None:
        xdg = self.tmp_path / "xdg"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False):
            self.assertEqual(
                xdg / "git" / "ignore", review_guard._default_excludes_file()
            )
            os.environ["XDG_CONFIG_HOME"] = ""
            os.environ["HOME"] = str(self.tmp_path / "home")
            self.assertEqual(
                self.tmp_path / "home" / ".config" / "git" / "ignore",
                review_guard._default_excludes_file(),
            )

    def test_non_excluded_change_still_trips_the_guard(self) -> None:
        before = self._fingerprint("claude")

        self._write("README.md", "mutated\n")
        after = self._fingerprint("claude")

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])

    def test_changed_paths_reports_tracked_and_untracked_movement(self) -> None:
        self._write("kept.txt", "kept\n")
        self._write("removed.txt", "removed\n")
        before = self._fingerprint()

        self._write("README.md", "mutated\n")
        self._write("kept.txt", "changed\n")
        (self.repo_root / "removed.txt").unlink()
        self._write("added.txt", "added\n")
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(
            {
                "tracked": ["README.md"],
                "untracked_added": ["added.txt"],
                "untracked_removed": ["removed.txt"],
                "untracked_changed": ["kept.txt"],
                "worktree_ignore": [],
                "ignore_config": [],
                "head_changed": False,
                "head_before": before["head"],
                "head_after": after["head"],
                "refs_added": [],
                "refs_removed": [],
                "refs_changed": [],
            },
            changed,
        )
        self.assertEqual(
            ": README.md, added.txt, kept.txt (+1 more)",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_untracked_symlink_is_fingerprinted_as_a_node(self) -> None:
        self._write("payload.txt", "hi\n")
        (self.repo_root / "link").symlink_to("payload.txt")
        before = self._fingerprint()

        self.assertIn(
            ["link", "symlink", hashlib.sha256(b"payload.txt").hexdigest()],
            before["untracked"],
        )

        # Following the link would have hashed the target's bytes, so replacing
        # the link with a regular file holding those bytes compared equal.
        (self.repo_root / "link").unlink()
        self._write("link", "hi\n")
        after = self._fingerprint()

        self.assertNotEqual(before, after)
        self.assertEqual(
            ["link"],
            review_guard._review_guard_changed_paths(before, after)[
                "untracked_changed"
            ],
        )

    def test_retargeted_dangling_symlink_trips_the_guard(self) -> None:
        (self.repo_root / "link").symlink_to("missing-a")
        before = self._fingerprint()

        (self.repo_root / "link").unlink()
        (self.repo_root / "link").symlink_to("missing-b")
        after = self._fingerprint()

        # Reading through the link fails for both targets, so omitting the path
        # left the mutation invisible in both fingerprints.
        self.assertNotEqual(before, after)
        self.assertEqual(
            ["link"],
            review_guard._review_guard_changed_paths(before, after)[
                "untracked_changed"
            ],
        )

    def test_renamed_tracked_file_names_both_sides(self) -> None:
        subprocess.run(
            ["git", "mv", "README.md", "DOCS.md"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

        fingerprint = self._fingerprint()

        # Rename detection would report the destination only, hiding the source
        # from the dirty-path evidence a later rereview scopes against.
        self.assertEqual(["DOCS.md", "README.md"], fingerprint["tracked_changed"])
        self.assertEqual(
            ["DOCS.md", "README.md"],
            sorted(entry[0] for entry in fingerprint["tracked_patches"]),
        )

    def test_unborn_head_fingerprints_staged_content_against_empty_tree(
        self,
    ) -> None:
        unborn = self.tmp_path / "unborn"
        unborn.mkdir()
        subprocess.run(["git", "init"], cwd=unborn, capture_output=True, check=True)
        (unborn / "tracked.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=unborn,
            capture_output=True,
            check=True,
        )

        before = review_guard._capture_review_repo_fingerprint(unborn, self.plan_dir)
        self.assertIsNone(review_guard._review_guard_unavailable_detail(before))
        self.assertEqual("", before["head"])
        self.assertEqual(["tracked.txt"], before["tracked_changed"])

        (unborn / "tracked.txt").write_text("two\n", encoding="utf-8")
        after = review_guard._capture_review_repo_fingerprint(unborn, self.plan_dir)

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["tracked.txt"], changed["tracked"])
        # No commit exists, so no base ref can be recorded for delta scoping.
        self.assertIsNone(review_guard._review_repo_state(after))

    def test_staged_change_masked_by_a_restored_file_trips_the_guard(
        self,
    ) -> None:
        before = self._fingerprint()

        self._write("README.md", "staged\n")
        self._stage("README.md")
        self._write("README.md", "test repo\n")
        after = self._fingerprint()

        # The working file is byte-identical to HEAD again, so every piece of
        # evidence but the index diff compares equal across the turn.
        self.assertEqual([], after["tracked_changed"])
        self.assertEqual(before["diff_sha256"], after["diff_sha256"])
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual(["README.md"], after["index_changed"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])
        self.assertEqual(
            ": README.md", review_guard._review_guard_changed_summary(changed)
        )

    def test_an_excluded_path_is_dropped_from_the_index_evidence_too(
        self,
    ) -> None:
        before = self._fingerprint("README.md")

        self._write("README.md", "staged\n")
        self._stage("README.md")
        self._write("README.md", "test repo\n")

        self.assertEqual(before, self._fingerprint("README.md"))

    def _git_config(self, key: str, value: str) -> None:
        subprocess.run(
            ["git", "config", key, value],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def _commit_constant_textconv_driver(self) -> None:
        """Commit a diff driver that renders every content identically."""
        self._write(".gitattributes", "*.txt diff=const\n")
        self._write("masked.txt", "one\n")
        self._commit_all("add a constant text converter")
        self._git_config("diff.const.textconv", "printf CONST")

    def test_a_textconv_driver_cannot_mask_a_worktree_mutation(self) -> None:
        self._commit_constant_textconv_driver()

        self._write("masked.txt", "two\n")
        before = self._fingerprint()
        self._write("masked.txt", "three\n")
        after = self._fingerprint()

        # Git renders no patch at all for a path whose converted text did not
        # move, and the path is already dirty in both captures, so the name
        # lists cannot tell the two contents apart either.
        self.assertEqual("", self._git("diff", "HEAD", "--", "masked.txt"))
        self.assertEqual(["masked.txt"], before["tracked_changed"])
        self.assertEqual(before["tracked_changed"], after["tracked_changed"])

        self.assertNotEqual(before["diff_sha256"], after["diff_sha256"])
        self.assertNotEqual(before["tracked_patches"], after["tracked_patches"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["masked.txt"], changed["tracked"])

    def test_a_textconv_driver_cannot_mask_a_staged_mutation(self) -> None:
        self._commit_constant_textconv_driver()

        self._write("masked.txt", "two\n")
        self._stage("masked.txt")
        before = self._fingerprint()
        self._write("masked.txt", "three\n")
        self._stage("masked.txt")
        after = self._fingerprint()

        self.assertEqual("", self._git("diff", "--cached", "HEAD", "--", "masked.txt"))
        self.assertEqual(["masked.txt"], before["index_changed"])
        self.assertEqual(before["index_changed"], after["index_changed"])

        self.assertNotEqual(before["index_diff_sha256"], after["index_diff_sha256"])
        self.assertNotEqual(before["index_patches"], after["index_patches"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["masked.txt"], changed["tracked"])

    def test_an_external_diff_driver_cannot_mask_a_mutation(self) -> None:
        # Lives outside the repository so the driver itself is not evidence.
        driver = self.tmp_path / "constant-diff.sh"
        driver.write_text("#!/bin/sh\necho CONSTANT\n", encoding="utf-8")
        driver.chmod(0o755)
        self._git_config("diff.external", str(driver))

        self._write("README.md", "two\n")
        before = self._fingerprint()
        self._write("README.md", "three\n")
        after = self._fingerprint()

        self.assertEqual("CONSTANT\n", self._git("diff", "HEAD", "--", "README.md"))
        self.assertNotEqual(before["diff_sha256"], after["diff_sha256"])
        self.assertNotEqual(before["tracked_patches"], after["tracked_patches"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])

    def test_a_named_path_git_renders_no_patch_for_fails_closed(self) -> None:
        self._write("README.md", "mutated\n")
        real_run_git = review_guard._run_git

        def fake_run_git(cwd, args):
            result = real_run_git(cwd, args)
            if tuple(args[:3]) == ("diff", "HEAD", "--no-renames"):
                return subprocess.CompletedProcess(
                    args=result.args, returncode=0, stdout="", stderr=""
                )
            return result

        with mock.patch.object(review_guard, "_run_git", fake_run_git):
            fingerprint = self._fingerprint()

        # A named path with no patch carries no content, so pairing short would
        # make any two of its contents compare equal.
        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("tracked-patch-missing", detail["reason"])
        self.assertNotIn("tracked_patches", fingerprint)

    def test_a_typechanged_path_pairs_both_of_its_patches(self) -> None:
        before = self._fingerprint()

        (self.repo_root / "README.md").unlink()
        (self.repo_root / "README.md").symlink_to("target-a")
        after = self._fingerprint()

        # Git cannot express a change of type as one patch, so it renders the
        # old type's deletion and the new type's creation as two sections for
        # the one name entry every other status claims exactly one of.
        self.assertEqual(
            2, self._git("diff", "HEAD", "--", "README.md").count("diff --git ")
        )
        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        self.assertEqual(["README.md"], after["tracked_changed"])
        self.assertEqual(
            ["README.md"], [entry[0] for entry in after["tracked_patches"]]
        )
        self.assertEqual(
            ["README.md"],
            review_guard._review_guard_changed_paths(before, after)["tracked"],
        )

    def test_a_staged_typechange_pairs_both_of_its_patches(self) -> None:
        before = self._fingerprint()

        (self.repo_root / "README.md").unlink()
        (self.repo_root / "README.md").symlink_to("target-a")
        self._stage("README.md")
        after = self._fingerprint()

        self.assertEqual(
            2,
            self._git("diff", "--cached", "HEAD", "--", "README.md").count(
                "diff --git "
            ),
        )
        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        self.assertEqual(["README.md"], after["index_changed"])
        self.assertEqual(["README.md"], [entry[0] for entry in after["index_patches"]])
        self.assertEqual(
            ["README.md"],
            review_guard._review_guard_changed_paths(before, after)["tracked"],
        )

    def test_a_typechange_does_not_misattribute_later_patches(self) -> None:
        self._write("a.txt", "one\n")
        self._write("b.txt", "one\n")
        self._commit_all("add two files")

        (self.repo_root / "a.txt").unlink()
        (self.repo_root / "a.txt").symlink_to("target")
        before = self._fingerprint()

        self._write("b.txt", "two\n")
        after = self._fingerprint()

        # Consuming one section for the typechange would have paired `b.txt`
        # with the second half of `a.txt`'s patch, so its own mutation would
        # not have moved its hash.
        self.assertEqual(["a.txt", "b.txt"], after["tracked_changed"])
        self.assertEqual(
            ["a.txt", "b.txt"],
            [entry[0] for entry in after["tracked_patches"]],
        )
        self.assertEqual(
            ["b.txt"],
            review_guard._review_guard_changed_paths(before, after)["tracked"],
        )

    def _flag_index_entry(self, rel: str, flag: str) -> None:
        subprocess.run(
            ["git", "update-index", flag, rel],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def test_index_flagged_file_mutation_trips_the_guard(self) -> None:
        for flag, rel in (
            ("--assume-unchanged", "assumed.txt"),
            ("--skip-worktree", "skipped.txt"),
        ):
            with self.subTest(flag=flag):
                self._write(rel, "one\n")
                self._commit_all(f"add {rel}")
                self._flag_index_entry(rel, flag)
                before = self._fingerprint()

                self._write(rel, "two\n")
                after = self._fingerprint()

                # Git honours the flag by never comparing the entry against the
                # working tree, so every diff-derived key compares equal across
                # a mutation git itself refuses to look at.
                self.assertEqual([], after["tracked_changed"])
                self.assertEqual([], after["index_changed"])
                self.assertEqual(before["diff_sha256"], after["diff_sha256"])
                self.assertEqual(
                    before["index_diff_sha256"], after["index_diff_sha256"]
                )
                self.assertEqual(before["untracked"], after["untracked"])

                self.assertNotEqual(before, after)
                changed = review_guard._review_guard_changed_paths(before, after)
                self.assertEqual([rel], changed["tracked"])
                self.assertEqual(
                    f": {rel}", review_guard._review_guard_changed_summary(changed)
                )

    def test_deleted_skip_worktree_file_trips_the_guard(self) -> None:
        self._write("sparse.txt", "one\n")
        self._commit_all("add sparse.txt")
        self._flag_index_entry("sparse.txt", "--skip-worktree")
        before = self._fingerprint()

        # Removing the file is the ordinary skip-worktree state, so it is
        # recorded rather than failing the capture closed — but it is still a
        # repository mutation the reviewer's tree did not have.
        (self.repo_root / "sparse.txt").unlink()
        after = self._fingerprint()

        self.assertEqual("", self._git_status_porcelain())
        self.assertEqual(["sparse.txt", "absent"], after["index_flagged"][0][:2])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["sparse.txt"], changed["tracked"])

    def test_an_excluded_index_flagged_path_is_not_fingerprinted(self) -> None:
        self._write("vendor/pinned.txt", "one\n")
        self._commit_all("add vendor tree")
        self._flag_index_entry("vendor/pinned.txt", "--skip-worktree")
        before = self._fingerprint("vendor")

        self._write("vendor/pinned.txt", "two\n")

        self.assertEqual([], before["index_flagged"])
        self.assertEqual(before, self._fingerprint("vendor"))
        self.assertEqual(
            ["vendor/pinned.txt"],
            [entry[0] for entry in self._fingerprint()["index_flagged"]],
        )

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def _git_status_porcelain(self) -> str:
        return self._git("status", "--porcelain").strip()

    def test_failed_git_probe_yields_an_unavailable_fingerprint(self) -> None:
        failing = {
            "diff": ("diff", "HEAD"),
            "name-status": ("diff", "HEAD", "--name-status"),
            "index-diff": ("diff", "--cached", "HEAD", "--no-renames"),
            "index-name-status": ("diff", "--cached", "HEAD", "--name-status"),
            "untracked": ("ls-files", "--others"),
        }
        real_run_git = review_guard._run_git
        for label, prefix in failing.items():
            with self.subTest(probe=label):

                def fake_run_git(cwd, args, _prefix=prefix):
                    if tuple(args[: len(_prefix)]) == _prefix:
                        return subprocess.CompletedProcess(
                            args=["git", *args],
                            returncode=128,
                            stdout="",
                            stderr="fatal: broken index\n",
                        )
                    return real_run_git(cwd, args)

                with mock.patch.object(review_guard, "_run_git", fake_run_git):
                    fingerprint = self._fingerprint()

                detail = review_guard._review_guard_unavailable_detail(fingerprint)
                self.assertIsNotNone(detail)
                self.assertEqual("git-command-failed", detail["reason"])
                self.assertEqual(128, detail["returncode"])
                self.assertEqual("fatal: broken index", detail["stderr"])
                self.assertEqual(list(prefix), detail["argv"][: len(prefix)])
                # The failure must not masquerade as clean evidence.
                self.assertNotIn("diff_sha256", fingerprint)
                self.assertNotIn("tracked_changed", fingerprint)
                self.assertNotIn("untracked", fingerprint)

    def test_untracked_read_error_yields_an_unavailable_fingerprint(self) -> None:
        self._write("scratch.txt", "readable\n")
        real_read_bytes = Path.read_bytes

        def fake_read_bytes(self_path: Path) -> bytes:
            if self_path.name == "scratch.txt":
                raise OSError(errno.EIO, "input/output error")
            return real_read_bytes(self_path)

        with mock.patch.object(Path, "read_bytes", fake_read_bytes):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("untracked-path-unreadable", detail["reason"])
        self.assertEqual("scratch.txt", detail["path"])
        self.assertIn("input/output error", detail["error"])
        # A dropped path must not leave the rest of the capture usable.
        self.assertNotIn("untracked", fingerprint)
        self.assertNotIn("diff_sha256", fingerprint)

    def test_untracked_path_git_reports_but_that_vanished_fails_closed(
        self,
    ) -> None:
        real_run_git = review_guard._run_git

        def fake_run_git(cwd, args):
            if tuple(args[:2]) == ("ls-files", "--others"):
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=0,
                    stdout="vanished.txt\n",
                    stderr="",
                )
            return real_run_git(cwd, args)

        with mock.patch.object(review_guard, "_run_git", fake_run_git):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("untracked-path-unreadable", detail["reason"])
        self.assertEqual("vanished.txt", detail["path"])

    def test_unrunnable_git_yields_an_unavailable_fingerprint(self) -> None:
        def fake_run_git(cwd, args):
            raise runner.RunError("git executable not found on PATH")

        with mock.patch.object(review_guard, "_run_git", fake_run_git):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("git-unrunnable", detail["reason"])
        self.assertIn("git executable not found", detail["error"])

    def test_non_repository_is_not_treated_as_unavailable(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir()

        # The ceiling keeps the answer independent of whatever encloses the
        # temporary directory on the machine running the suite.
        with patched_env(GIT_CEILING_DIRECTORIES=str(self.tmp_path)):
            fingerprint = review_guard._capture_review_repo_fingerprint(
                outside, self.plan_dir
            )

        self.assertEqual({"git": False}, fingerprint)
        self.assertIsNone(review_guard._review_guard_unavailable_detail(fingerprint))

    def test_changed_paths_reports_a_moved_head(self) -> None:
        before = self._fingerprint()
        self._write("README.md", "committed\n")
        subprocess.run(
            ["git", "commit", "-am", "second"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertTrue(changed["head_changed"])
        self.assertEqual("", review_guard._review_guard_changed_summary(changed))


class ReviewDeltaScopeTests(unittest.TestCase):
    """Scope selection for a re-review that follows a repair cycle."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        init_git_repo(self.repo_root)
        self.event_path = self.tmp_path / "events.jsonl"
        self.finding = _structured_review_finding()
        write_completed_review_plan(self.plan_dir, [self.finding])

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _commit(self, rel: str, text: str) -> str:
        path = self.repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._git("add", rel)
        self._git("commit", "-m", f"write {rel}")
        return self._git("rev-parse", "HEAD")

    def _record_repair_cycle(self, **overrides: object) -> None:
        record: dict[str, Any] = {
            "cycle": 1,
            "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            "review_session_id": "2",
            "repair_session_ids": ["2.1"],
            "fingerprints": ["finding:F1"],
            "worktree_dirty": False,
            "dirty_paths": [],
        }
        record.update(overrides)
        events.append_event(
            self.event_path, events.Event(events.REVIEW_CYCLE_EVENT, record)
        )

    def _record_repair_progress(self, *files: str) -> None:
        plans.append_progress_entry(
            self.plan_dir,
            plans.ProgressEntry(
                session_id=plans.parse_session_id("2.1"),
                status="complete",
                completed_at="2026-01-01T00:00:00Z",
                files_modified=files,
            ),
        )

    def _compute(
        self, cwd: Path | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        return review_delta._compute_review_delta_context(
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            cwd=self.repo_root if cwd is None else cwd,
        )

    def _scope_diff_commands(self, scope: str) -> list[str]:
        """Return the `git` commands the rendered scope tells a reviewer to run."""
        return [span for span in scope.split("`")[1::2] if span.startswith("git ")]

    def _scope_section(self, delta: dict[str, Any]) -> str:
        """Return the `### Scope` text the reviewer is actually told to read."""
        body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
            delta=delta,
        )
        self.assertIn("## Delta Review Scope", body)
        return body.split("### Scope", 1)[1]

    def test_first_review_without_repair_history_is_full_scope(self) -> None:
        self.assertEqual((None, "no-prior-repair-cycle"), self._compute())

    def test_unloadable_prior_findings_fall_back_to_full_scope(self) -> None:
        self._record_repair_cycle(repo_head=self._git("rev-parse", "HEAD"))
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

        self.assertEqual((None, "prior-findings-unavailable"), self._compute())

    def test_committed_repairs_scope_review_to_the_recorded_base(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base, worktree_dirty=False)
        self._commit("src/repair.py", "repaired\n")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(base, delta["base_sha"])
        self.assertEqual(["src/repair.py"], delta["changed_files"])
        self.assertEqual([self.finding], delta["prior_findings"])

        scope = self._scope_section(delta)
        flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
        self.assertEqual(
            [
                f"git diff {flags} {base}",
                f"git diff --cached {flags} {base}",
            ],
            self._scope_diff_commands(scope),
        )
        self.assertIn("src/repair.py", scope)

    def test_scope_covers_repairs_the_handoff_never_declared(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        # Uncommitted repair work: one edit to a tracked file and one brand new
        # file, of which the handoff declared only the first.
        (self.repo_root / "src" / "base.py").write_text("repaired\n", encoding="utf-8")
        (self.repo_root / "src" / "collateral.py").write_text(
            "collateral\n", encoding="utf-8"
        )
        self._record_repair_progress("src/base.py")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/base.py", "src/collateral.py"], delta["changed_files"])
        self.assertIn("src/collateral.py", self._scope_section(delta))

    def test_ignored_repair_file_stays_out_of_scope(self) -> None:
        base = self._commit(".gitignore", "generated/\n")
        self._record_repair_cycle(repo_head=base)
        generated = self.repo_root / "generated"
        generated.mkdir()
        (generated / "artifact.txt").write_text("repaired\n", encoding="utf-8")
        src = self.repo_root / "src"
        src.mkdir()
        (src / "repair.py").write_text("repaired\n", encoding="utf-8")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/repair.py"], delta["changed_files"])
        self.assertNotIn("generated/artifact.txt", self._scope_section(delta))

    def test_renamed_files_keep_both_paths_in_scope(self) -> None:
        base = self._commit("src/before.py", "moved\n")
        self._record_repair_cycle(repo_head=base)
        self._git("mv", "src/before.py", "src/after.py")
        self._git("commit", "-m", "rename")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(["src/after.py", "src/before.py"], delta["changed_files"])

    def test_dirty_baseline_keeps_a_restored_tracked_file_in_scope(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        # The reviewer read an uncommitted edit to src/base.py.
        (self.repo_root / "src" / "base.py").write_text(
            "reviewed wip\n", encoding="utf-8"
        )
        self._record_repair_cycle(
            repo_head=base,
            worktree_dirty=True,
            dirty_paths=["src/base.py"],
        )
        # The repair restored it to the committed content, so the current
        # base-to-worktree diff no longer mentions it at all.
        (self.repo_root / "src" / "base.py").write_text("base\n", encoding="utf-8")
        self._commit("src/repair.py", "repaired\n")
        self.assertEqual(
            "src/repair.py",
            self._git("diff", "--name-only", "--no-renames", base),
        )

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/base.py", "src/repair.py"], delta["changed_files"])
        self.assertIs(True, delta["baseline_dirty"])
        scope = self._scope_section(delta)
        self.assertIn("src/base.py", scope)
        self.assertIn("carried uncommitted changes", scope)

    def test_dirty_baseline_keeps_a_deleted_untracked_file_in_scope(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        scratch = self.repo_root / "src" / "scratch.py"
        scratch.write_text("reviewed scratch\n", encoding="utf-8")
        self._record_repair_cycle(
            repo_head=base,
            worktree_dirty=True,
            dirty_paths=["src/scratch.py"],
        )
        # The repair deleted the untracked file the reviewer saw, so neither
        # the diff nor the untracked scan reports it any more.
        scratch.unlink()

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/scratch.py"], delta["changed_files"])
        self.assertIn("src/scratch.py", self._scope_section(delta))

    def test_staged_repair_masked_by_a_restored_file_stays_in_scope(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        # The repair staged an edit and then restored the working file to its
        # committed bytes, so only the index still carries the change.
        target = self.repo_root / "src" / "base.py"
        target.write_text("repaired\n", encoding="utf-8")
        self._git("add", "src/base.py")
        target.write_text("base\n", encoding="utf-8")
        self.assertEqual("", self._git("diff", "--name-only", "--no-renames", base))

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/base.py"], delta["changed_files"])
        self.assertIn("src/base.py", self._scope_section(delta))

    def test_rendered_scope_exposes_the_staged_patch_not_just_its_path(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        target = self.repo_root / "src" / "base.py"
        target.write_text("repaired\n", encoding="utf-8")
        self._git("add", "src/base.py")
        target.write_text("base\n", encoding="utf-8")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        commands = self._scope_diff_commands(self._scope_section(delta))
        self.assertEqual(2, len(commands))
        patches = [self._git(*command.split()[1:]) for command in commands]
        # The repair restored the working file, so the base-to-worktree patch
        # is empty: only the second command carries the staged bytes.
        self.assertEqual("", patches[0])
        self.assertIn("src/base.py", patches[1])
        self.assertIn("+repaired", patches[1])

    def test_a_constant_textconv_cannot_empty_the_rendered_patches(
        self,
    ) -> None:
        (self.repo_root / ".gitattributes").write_text(
            "*.txt diff=const\n", encoding="utf-8"
        )
        (self.repo_root / "masked.txt").write_text("one\n", encoding="utf-8")
        self._git("add", ".gitattributes", "masked.txt")
        base = self._commit("staged.txt", "one\n")
        self._git("config", "diff.const.textconv", "printf CONST")
        self._record_repair_cycle(repo_head=base)
        (self.repo_root / "masked.txt").write_text(
            "worktree-repair\n", encoding="utf-8"
        )
        staged = self.repo_root / "staged.txt"
        staged.write_text("cached-repair\n", encoding="utf-8")
        self._git("add", "staged.txt")
        staged.write_text("one\n", encoding="utf-8")
        # The converter renders every content as the same constant text, so
        # git prints no patch at all without the presentation flags and this
        # regression cannot pass vacuously.
        self.assertEqual("", self._git("diff", "--no-renames", "--no-relative", base))
        self.assertEqual(
            "",
            self._git("diff", "--cached", "--no-renames", "--no-relative", base),
        )
        # Both name selectors still see past the converter, so the repairs are
        # in the file list either way.
        flags = list(review_session_body._DELTA_DIFF_FLAGS)
        self.assertEqual("masked.txt", self._git("diff", "--name-only", *flags, base))
        self.assertEqual(
            "staged.txt",
            self._git("diff", "--cached", "--name-only", *flags, base),
        )

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(["masked.txt", "staged.txt"], delta["changed_files"])

        commands = self._scope_diff_commands(self._scope_section(delta))
        self.assertEqual(2, len(commands))
        patches = [self._git(*command.split()[1:]) for command in commands]
        self.assertIn("+worktree-repair", patches[0])
        self.assertIn("+cached-repair", patches[1])

    def test_nested_cwd_keeps_repository_root_repairs_in_scope(self) -> None:
        self._commit("nested/base.py", "base\n")
        self._commit("root_staged.py", "base\n")
        base = self._commit("root.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        self._git("config", "diff.relative", "true")
        (self.repo_root / "nested" / "base.py").write_text(
            "repaired\n", encoding="utf-8"
        )
        (self.repo_root / "root.py").write_text("repaired\n", encoding="utf-8")
        staged = self.repo_root / "root_staged.py"
        staged.write_text("repaired\n", encoding="utf-8")
        self._git("add", "root_staged.py")
        staged.write_text("base\n", encoding="utf-8")
        # Unpinned, both probes report only the nested subtree from that
        # directory, so this regression cannot pass vacuously.
        self.assertEqual(
            "base.py",
            self._git("-C", "nested", "diff", "--name-only", "--no-renames", base),
        )
        self.assertEqual(
            "",
            self._git(
                "-C",
                "nested",
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                base,
            ),
        )

        delta, fallback_reason = self._compute(cwd=self.repo_root / "nested")

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(
            ["nested/base.py", "root.py", "root_staged.py"],
            delta["changed_files"],
        )
        # The rendered commands must reach as far as that list does, run the
        # way a reviewer in the same nested directory would run them.
        commands = self._scope_diff_commands(self._scope_section(delta))
        self.assertEqual(2, len(commands))
        patches = [
            self._git("-C", "nested", *command.split()[1:]) for command in commands
        ]
        self.assertIn("root.py", patches[0])
        self.assertIn("root_staged.py", patches[1])

    def test_repair_to_an_index_flagged_file_stays_in_scope(self) -> None:
        for flag, rel in (
            ("--assume-unchanged", "src/assumed.py"),
            ("--skip-worktree", "src/skipped.py"),
        ):
            with self.subTest(flag=flag):
                base = self._commit(rel, "base\n")
                self._git("update-index", flag, rel)
                self._record_repair_cycle(repo_head=base)
                # The repair edits the flagged file, which git reports in no
                # diff at all.
                (self.repo_root / rel).write_text("repaired\n", encoding="utf-8")
                self.assertEqual(
                    "", self._git("diff", "--name-only", "--no-renames", base)
                )
                self.assertEqual("", self._git("diff", "--cached", "--name-only", base))

                delta, fallback_reason = self._compute()

                self.assertIsNone(fallback_reason)
                assert delta is not None
                self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
                self.assertIn(rel, delta["changed_files"])
                self.assertIn(rel, self._scope_section(delta))

    def test_repo_state_carries_an_index_flagged_path_into_the_next_scope(
        self,
    ) -> None:
        head = self._git("rev-parse", "HEAD")
        self._git("update-index", "--skip-worktree", "README.md")

        state = self._repo_state()

        # No diff can prove the flagged entry matches HEAD, so the reviewed
        # tree is recorded as dirty at that path and the next rereview keeps it.
        self.assertEqual(
            {
                "head": head,
                "worktree_dirty": True,
                "dirty_paths": ["README.md"],
            },
            state,
        )

    def test_dirty_baseline_without_path_evidence_is_full_scope(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base, worktree_dirty=True, dirty_paths=None)
        self._commit("src/repair.py", "repaired\n")

        delta, fallback_reason = self._compute()

        self.assertEqual("dirty-base-evidence-missing", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertNotIn("src/", self._scope_section(delta))

    def test_cycle_with_unknown_dirtiness_is_full_scope(self) -> None:
        base = self._commit("src/base.py", "base\n")
        # A cycle recorded before dirty-path evidence existed.
        self._record_repair_cycle(repo_head=base, worktree_dirty=None, dirty_paths=None)
        self._commit("src/repair.py", "repaired\n")

        delta, fallback_reason = self._compute()

        self.assertEqual("dirty-base-evidence-missing", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])

    def test_cycle_without_recorded_base_falls_back_to_full_scope(
        self,
    ) -> None:
        self._record_repair_cycle()
        self._record_repair_progress("src/repair.py")

        delta, fallback_reason = self._compute()

        self.assertEqual("no-recorded-base", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertNotIn("base_sha", delta)
        self.assertEqual([self.finding], delta["prior_findings"])
        self.assertNotIn("src/", self._scope_section(delta))

    def test_unknown_base_commit_falls_back_to_full_scope(self) -> None:
        self._record_repair_cycle(repo_head="0" * 40)
        self._record_repair_progress("src/repair.py")

        delta, fallback_reason = self._compute()

        self.assertEqual("base-unresolvable", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertNotIn("src/", self._scope_section(delta))

    def test_rewritten_history_falls_back_to_full_scope(self) -> None:
        keep = self._git("rev-parse", "HEAD")
        dropped = self._commit("src/dropped.py", "dropped\n")
        self._record_repair_cycle(repo_head=dropped)
        # A rebase/reset moves the recorded base off the current history while
        # the commit object itself still resolves.
        self._git("reset", "--hard", keep)
        self._commit("src/repair.py", "repaired\n")
        self._record_repair_progress("src/dropped.py")

        delta, fallback_reason = self._compute()

        self.assertEqual("base-not-ancestor", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        # The reviewer is given no file list at all, so neither the handoff's
        # stale path nor the file the repair really changed can be excluded.
        scope = self._scope_section(delta)
        self.assertIn("this scope is not narrowed", scope)
        self.assertNotIn("src/", scope)

    def test_failed_git_probes_fall_back_to_full_scope(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        self._commit("src/repair.py", "repaired\n")
        real_run_git = review_guard._run_git

        for prefix, reason in (
            (("diff", "--name-only"), "base-diff-failed"),
            (("diff", "--cached"), "index-diff-failed"),
            (("ls-files", "--others"), "untracked-scan-failed"),
            (("ls-files", "-v"), "index-flags-scan-failed"),
        ):
            with self.subTest(command=prefix):

                def fail_one(cwd, args, _prefix=prefix):
                    if tuple(args[: len(_prefix)]) == _prefix:
                        return subprocess.CompletedProcess(args, 128, "", "boom")
                    return real_run_git(cwd, args)

                with mock.patch.object(review_guard, "_run_git", fail_one):
                    delta, fallback_reason = self._compute()

                self.assertEqual(reason, fallback_reason)
                assert delta is not None
                self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
                self.assertNotIn("src/", self._scope_section(delta))

    def _repo_state(self) -> dict[str, Any] | None:
        return review_guard._review_repo_state(
            review_guard._capture_review_repo_fingerprint(
                self.repo_root, self.plan_dir, exclude=()
            )
        )

    def _advance_cycle(
        self, repo_state: object
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one all-clear cycle and return its state and event records."""
        write_completed_review_plan(self.plan_dir, [])
        snapshot = plans.inspect_plan(self.plan_dir)
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }

        action, _ = review_cycle._advance_review_cycle(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.tmp_path / "state"),
                allow_not_loop_ready=False,
            ),
            cwd=self.repo_root,
            session_id="2",
            session_label="session 2",
            session_details={"session": "2"},
            snapshot=snapshot,
            turn_payload={
                "review_verdict": {
                    "passed": True,
                    "outcome": "all-clear",
                    "total_findings": 0,
                    "findings": [],
                },
                "review_repo_state": repo_state,
            },
            max_review_fix_cycles=1,
            plan_cache=plans.PlanSnapshotCache(),
            reinspect=session_verification._reinspect_after_plan_mutation,
        )

        self.assertEqual("complete", action)
        recorded = [
            entry["payload"]
            for entry in read_jsonl(self.event_path)
            if entry["name"] == events.REVIEW_CYCLE_EVENT
        ]
        return payload["review_cycles"][0], recorded[-1]

    def _pending_delta_body(self) -> str:
        """Render the session file a crashed run would have left pending."""
        delta, fallback_reason = self._compute()
        self.assertIsNone(fallback_reason)
        return review_session_body.build_generated_review_session_body(
            plans.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
            delta=delta,
        )

    def test_rescoping_lists_an_untracked_file_added_after_the_session(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        original = self._pending_delta_body()
        self.assertNotIn("src/late.py", original)
        # Untracked files are absent from `git diff <base>`, so the static list
        # is the only thing that can tell the reviewer to read this one.
        (self.repo_root / "src" / "late.py").write_text("late\n", encoding="utf-8")

        refreshed = review_session_body.rescoped_review_session_body(
            original, self._compute()[0]
        )

        assert refreshed is not None
        self.assertIn("  - `src/late.py`", refreshed)
        # Identity, version marker, and the pending handoff survive intact.
        session_path = self.plan_dir / "session-3.md"
        session_path.write_text(refreshed, encoding="utf-8")
        self.assertTrue(
            review_session_contract.is_generated_review_session_file(session_path)
        )
        self.assertEqual(
            review_session_contract.generated_review_session_version(session_path),
            review_session_contract.generated_review_session_version(
                self.plan_dir / "session-2.md"
            ),
        )
        session = plans.SessionFile(
            session_id=plans.parse_session_id("3"), path=session_path
        )
        self.assertEqual("pending", plans.read_session_status(session).status)
        self.assertEqual(
            original.split("## Completion Contract", 1)[1],
            refreshed.split("## Completion Contract", 1)[1],
        )
        self.assertIn(f"- `{self.finding['id']}`", refreshed)

    def test_rescoping_widens_to_full_when_the_evidence_is_gone(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        self._commit("src/repair.py", "repaired\n")
        original = self._pending_delta_body()
        self.assertIn("  - `src/repair.py`", original)
        # Without the findings artifact the narrowed list can no longer be
        # shown to cover the repair work.
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()
        delta, fallback_reason = self._compute()
        self.assertEqual((None, "prior-findings-unavailable"), (delta, fallback_reason))

        refreshed = review_session_body.rescoped_review_session_body(original, delta)

        assert refreshed is not None
        self.assertIn("this scope is not narrowed", refreshed)
        self.assertNotIn(base, refreshed)
        self.assertNotIn("src/repair.py", refreshed)
        # The prior findings still have to be re-verified.
        self.assertIn(f"- `{self.finding['id']}`", refreshed)

    def test_a_full_scope_session_is_never_narrowed_by_a_refresh(self) -> None:
        self._record_repair_cycle(repo_head=self._commit("src/base.py", "b\n"))
        full_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
        )

        self.assertIsNone(
            review_session_body.rescoped_review_session_body(
                full_body, self._compute()[0]
            )
        )

    def test_an_unparseable_scope_section_cannot_be_re_scoped(self) -> None:
        self._record_repair_cycle(repo_head=self._commit("src/base.py", "b\n"))
        mangled = self._pending_delta_body().replace(
            f"{review_session_body.REVIEW_SCOPE_DETAIL_HEADING}\n", "", 1
        )

        with self.assertRaises(plans.PlanError):
            review_session_body.rescoped_review_session_body(
                mangled, self._compute()[0]
            )

    def test_repo_state_is_derived_from_the_guard_fingerprint(self) -> None:
        head = self._git("rev-parse", "HEAD")

        self.assertEqual(
            {"head": head, "worktree_dirty": False, "dirty_paths": []},
            self._repo_state(),
        )

        (self.repo_root / "scratch.txt").write_text("wip\n", encoding="utf-8")
        (self.repo_root / "README.md").write_text("edited\n", encoding="utf-8")
        dirty = self._repo_state()
        assert dirty is not None
        self.assertEqual(
            {
                "head": head,
                "worktree_dirty": True,
                "dirty_paths": ["README.md", "scratch.txt"],
            },
            dirty,
        )
        self.assertIsNone(review_guard._review_repo_state({"git": False}))

    def test_repo_state_records_an_index_only_dirty_path(self) -> None:
        head = self._git("rev-parse", "HEAD")
        readme = self.repo_root / "README.md"
        readme.write_text("staged\n", encoding="utf-8")
        self._git("add", "README.md")
        readme.write_text("test repo\n", encoding="utf-8")

        # Nothing the base-to-worktree diff can see, but a commit would still
        # publish the staged bytes, so the next rereview must cover the path.
        self.assertEqual("", self._git("diff", "--name-only", head))
        self.assertEqual(
            {
                "head": head,
                "worktree_dirty": True,
                "dirty_paths": ["README.md"],
            },
            self._repo_state(),
        )

    def test_cycle_record_carries_the_reviewed_repo_head(self) -> None:
        head = self._git("rev-parse", "HEAD")

        cycle, recorded = self._advance_cycle(self._repo_state())

        self.assertEqual(head, cycle["repo_head"])
        self.assertIs(False, cycle["worktree_dirty"])
        self.assertEqual([], cycle["dirty_paths"])
        self.assertEqual(head, recorded["repo_head"])

    def test_cycle_record_carries_the_reviewed_dirty_paths(self) -> None:
        (self.repo_root / "scratch.txt").write_text("wip\n", encoding="utf-8")

        cycle, recorded = self._advance_cycle(self._repo_state())

        self.assertIs(True, cycle["worktree_dirty"])
        self.assertEqual(["scratch.txt"], cycle["dirty_paths"])
        self.assertEqual(["scratch.txt"], recorded["dirty_paths"])
        # The reader of that record agrees on the key it was written under.
        self.assertEqual(
            ["scratch.txt"], review_delta._reviewed_baseline_dirty_paths(cycle)
        )


class ReviewRepairGroupAttemptHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.event_path = self.tmp_path / events.EVENTS_FILE_NAME

    def _write_repair_children(
        self, findings: list[dict[str, Any]]
    ) -> plans.PlanSnapshot:
        write_completed_review_plan(self.plan_dir, findings)
        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)
        self.assertTrue(repair_plan.ok, repair_plan.error)
        for group in repair_plan.repair_groups:
            child_path = self.plan_dir / group.proposed_child_id.filename
            child_path.write_text(group.rendered_text, encoding="utf-8")
        return plans.inspect_plan(self.plan_dir)

    def test_repair_group_attempts_reads_event_history(self) -> None:
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "review_session_id": "2",
                    "repair_session_ids": ["2.1", "2.2"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "2.1",
                            "finding_ids": ["F1"],
                        },
                        {
                            "repair_group": "docs",
                            "repair_session_id": "2.2",
                            "finding_ids": ["F2"],
                        },
                    ],
                },
            ),
        )
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_review_session_id": "4",
                    "materialized": True,
                    "repair_session_ids": ["4.1"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "4.1",
                            "finding_ids": ["F3"],
                        }
                    ],
                },
            ),
        )

        self.assertEqual(
            {"docs": 1, "runner": 2},
            review_continuation.repair_group_attempts(self.event_path),
        )

    def test_repair_group_attempts_recovers_registry_free_children(
        self,
    ) -> None:
        snapshot = self._write_repair_children(
            [
                _structured_review_finding(finding_id="F1", repair_group="runner"),
                _structured_review_finding(finding_id="F2", repair_group="docs"),
            ]
        )

        self.assertEqual(
            {"docs": 1, "runner": 1},
            review_continuation.repair_group_attempts(self.event_path, snapshot),
        )

    def test_repair_group_attempts_deduplicates_events_and_children(
        self,
    ) -> None:
        snapshot = self._write_repair_children(
            [_structured_review_finding(repair_group="runner")]
        )
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "review_session_id": "2",
                    "repair_session_ids": ["2.1", "2.2"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "2.1",
                            "finding_ids": ["F1"],
                        },
                        {
                            "repair_group": "docs",
                            "repair_session_id": "2.2",
                            "finding_ids": ["F2"],
                        },
                    ],
                },
            ),
        )

        self.assertEqual(
            {"docs": 1, "runner": 1},
            review_continuation.repair_group_attempts(self.event_path, snapshot),
        )

    def test_advance_review_cycle_stops_when_group_attempt_cap_is_exhausted(
        self,
    ) -> None:
        initial = _structured_review_finding(finding_id="F1", repair_group="runner")
        rereview = _structured_review_finding(
            finding_id="F2",
            repair_group="runner",
            title="A different runner failure still needs repair",
            evidence=[{"path": "pkg/F2.py", "symbol": "retry_loop"}],
            explanation="F2 is still broken in a different way.",
            done_criteria=["F2 is fixed and tested."],
        )
        self._write_repair_children([initial])
        child_path = self.plan_dir / "session-2.1.md"
        child_path.write_text(
            child_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        review_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("3"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-3.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- pkg/F2.py still fails.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [rereview]}),
            encoding="utf-8",
        )
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                    "fingerprints": [review.finding_fingerprint(initial)],
                    "repair_session_ids": ["2.1"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "2.1",
                            "finding_ids": ["F1"],
                        }
                    ],
                    "max_review_fix_cycles": 4,
                },
            ),
        )
        snapshot = plans.inspect_plan(self.plan_dir)
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }

        action, after = review_cycle._advance_review_cycle(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.tmp_path / "state"),
                allow_not_loop_ready=False,
                max_repair_group_attempts=1,
            ),
            cwd=self.tmp_path,
            session_id="3",
            session_label="session 3",
            session_details={"session": "3"},
            snapshot=snapshot,
            turn_payload={
                "review_verdict": {
                    "passed": True,
                    "outcome": "findings",
                    "total_findings": 1,
                    "findings": [rereview],
                }
            },
            max_review_fix_cycles=4,
            plan_cache=plans.PlanSnapshotCache(),
            reinspect=session_verification._reinspect_after_plan_mutation,
        )

        self.assertEqual("complete", action)
        self.assertIs(snapshot, after)
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())
        cycle = payload["review_cycles"][0]
        assert isinstance(cycle, dict)
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
            cycle["disposition"],
        )
        self.assertEqual([], cycle["repair_session_ids"])
        self.assertEqual(1, cycle["max_repair_group_attempts"])
        self.assertEqual("runner", cycle["exhausted_repair_group"])
        self.assertEqual(1, cycle["exhausted_repair_group_attempts"])
        self.assertEqual(
            [
                {
                    "repair_group": "runner",
                    "repair_session_id": "3.1",
                    "finding_ids": ["F2"],
                }
            ],
            cycle["repair_groups"],
        )


if __name__ == "__main__":
    unittest.main()
