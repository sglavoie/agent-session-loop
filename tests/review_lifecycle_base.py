"""Shared fixtures for the ``--review-harness`` lifecycle tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    config,
    control_queue,
    events,
    plans,
    review,
    review_session_body,
    review_session_contract,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    init_git_repo,
    install_fake_codex,
    install_fake_harness,
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


class RunReviewHarnessLifecycleBase(unittest.TestCase):
    """Shared fixtures for the ``--review-harness`` lifecycle tests."""

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

    def _events_path(self) -> Path:
        plan_state = self.state_dir / plans.path_sha256(
            plans.canonicalize_path(self.plan_dir)
        )
        return plan_state / events.EVENTS_FILE_NAME

    def _read_events(self) -> list[dict[str, Any]]:
        return read_jsonl(self._events_path())

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

    def _review_verdict_retries(self) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in self._read_events()
            if record.get("name") == events.REVIEW_VERDICT_RETRY_EVENT
        ]

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
