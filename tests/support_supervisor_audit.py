"""Shared fixtures for the supervisor-audit test modules."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_session_loop import (
    cli,
    events,
    harnesses,
    plans,
    state,
    supervisor_audit,
    supervisor_audit_evidence,
    verification_transitions,
)
from tests.support import init_git_repo

RUN_ID = "20260810T101112123456Z-4242"


#: Read at its owning module rather than spelled inline: the reason is 62
#: characters and the fixture payloads that carry it nest three levels deep.
DEFERRED_REASON = verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON


def parse(tokens: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(tokens)


def namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, Any] = {
        "harness": "codex",
        "review_harness": None,
        "review_harness_model": None,
        "supervisor_audit": True,
        "audit_harness": None,
        "audit_harness_model": None,
        "audit_effort": None,
        "audit_timeout": supervisor_audit.DEFAULT_AUDIT_TIMEOUT_SECONDS,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RunHistoryFixture:
    """One recorded run, shared by the deterministic and agent audit tests."""

    MANIFEST = (
        "# Manifest\n\n## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        "| app | app | https://example.invalid/app.git |\n"
    )

    def _history(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        """Write one run whose every recorded artifact agrees with the others."""
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(self.MANIFEST, encoding="utf-8")
        (plan_dir / "session-01.md").write_text(
            "# Session 1\n\n"
            f"{plans.READS_REPOSITORIES_PREFIX} app\n"
            f"{plans.WRITES_REPOSITORIES_PREFIX} app\n",
            encoding="utf-8",
        )
        (plan_dir / plans.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "workspace_root": str(workspace)}),
            encoding="utf-8",
        )

        turn_dir = run_dir / "turn-0001-01"
        turn_dir.mkdir()
        self._write_result(turn_dir, self._clean_result(workspace, turn_dir))
        (turn_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
        (turn_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
        (turn_dir / "last-message.txt").write_text("Session done.\n", encoding="utf-8")

        self._write_state(state_dir, self._clean_state(workspace))
        self._write_events(
            state_dir,
            [
                ("run_started", {"run_id": RUN_ID}),
                (events.TURN_HEARTBEAT_EVENT, {"run_id": RUN_ID, "index": 0}),
                (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
            ],
        )
        return workspace, plan_dir, state_dir, run_dir

    def _clean_result(self, workspace: Path, turn_dir: Path) -> dict[str, Any]:
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "interrupted": False,
            "session_kind": "implementation",
            "artifact_dir": str(turn_dir),
            "stdout_path": str(turn_dir / "stdout.log"),
            "stderr_path": str(turn_dir / "stderr.log"),
            "last_message_path": str(turn_dir / "last-message.txt"),
            "result_path": str(turn_dir / state.RESULT_FILE_NAME),
            "invocation": {"name": "codex", "cwd": str(workspace / "app")},
            "workspace_write_guard": {
                "passed": True,
                "write_aliases": ["app"],
                "authorized_aliases": ["app"],
                "violations": [],
                "unavailable": {},
            },
            "verification": {
                "passed": True,
                "checks": [{"name": "tests", "passed": True, "returncode": 0}],
                "skipped_checks": [],
            },
        }

    def _clean_state(self, workspace: Path) -> dict[str, Any]:
        return {
            "harness": "codex",
            "model": "gpt-5",
            "ok": True,
            "workspace_root": str(workspace),
            "commit_mode": "session",
            "review_completed": True,
            "review_harness": "claude",
            "review_verdict": "approved",
            "repositories": [
                {
                    "alias": "app",
                    "path": "app",
                    "origin": "https://example.invalid/app.git",
                    "resolved_root": str(workspace / "app"),
                }
            ],
            "run_config": {"cwd": str(workspace)},
        }

    def _read_state(self, state_dir: Path) -> dict[str, Any]:
        return json.loads(
            (state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )

    def _write_state(self, state_dir: Path, payload: dict[str, Any]) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _read_result(self, run_dir: Path) -> dict[str, Any]:
        return json.loads(
            (run_dir / "turn-0001-01" / state.RESULT_FILE_NAME).read_text(
                encoding="utf-8"
            )
        )

    def _write_result(self, turn_dir: Path, payload: dict[str, Any]) -> None:
        (turn_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _write_events(
        self, state_dir: Path, records: list[tuple[str, dict[str, Any]]]
    ) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "name": name,
                    "version": events.EVENT_SCHEMA_VERSION,
                    "created_at": f"2026-08-10T10:11:{12 + index:02d}Z",
                    "payload": payload,
                }
            )
            for index, (name, payload) in enumerate(records)
        ]
        (state_dir / events.EVENTS_FILE_NAME).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _make_legacy_plan(self, history: tuple[Path, Path, Path, Path]) -> Path:
        """Rewrite the run as a registry-free legacy plan; return its repository."""
        workspace, plan_dir, state_dir, _ = history
        repository = workspace / "app"
        (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        (plan_dir / "session-01.md").write_text("# Session 1\n", encoding="utf-8")
        (plan_dir / plans.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "cwd": str(repository)}), encoding="utf-8"
        )
        payload = self._read_state(state_dir)
        payload.pop("repositories")
        payload.pop("workspace_root")
        payload["commit_mode"] = None
        payload["run_config"]["cwd"] = str(repository)
        self._write_state(state_dir, payload)
        return repository

    def _bundle(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        workspace_root: Path | None = None,
    ) -> supervisor_audit_evidence.AuditInput:
        workspace, plan_dir, state_dir, run_dir = history
        return supervisor_audit_evidence.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace if workspace_root is None else workspace_root,
        )


#: Injected so an audit's timestamp is an input rather than a wall-clock read,
#: which is what lets two identical runs produce byte-identical artifacts.
FIXED_CLOCK = datetime(2026, 8, 10, 10, 11, 12, tzinfo=UTC)


class ScriptedAuditor:
    """Stand-in auditor with scripted answers that records every prompt it saw.

    Replies are consumed in order and the last one repeats, so a test that wants
    the same failure on both attempts scripts it once.
    """

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.names: list[str] = []

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        self.names.append(name)
        access = kwargs["access"]
        assert isinstance(access, harnesses.AccessProfile)
        argv = harnesses.get_adapter(name).build_argv(
            cwd=Path(str(kwargs["cwd"])),
            plan_dir=None,
            prompt=prompt,
            last_message_path=Path(str(kwargs["last_message_path"])),
            access=access,
        )
        index = min(len(self.prompts), len(self.replies)) - 1
        reply = self.replies[index]
        spec = reply if isinstance(reply, dict) else {"message": reply}
        return harnesses.HarnessResult(
            argv=argv,
            returncode=int(spec.get("returncode", 0)),
            stdout=str(spec.get("stdout", "")),
            stderr=str(spec.get("stderr", "")),
            last_message=str(spec.get("message", "")),
            name=name,
            timed_out=bool(spec.get("timed_out", False)),
            interrupted=bool(spec.get("interrupted", False)),
        )
