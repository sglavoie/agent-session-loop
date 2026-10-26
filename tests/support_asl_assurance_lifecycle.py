"""Shared fixtures for the assurance lifecycle test modules."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import (
    assurance_models_findings,
    assurance_models_summary,
    events,
    harnesses,
    live_verification,
    plans_files,
    plans_metadata,
    plans_session_scope,
    state,
    supervisor_audit_agent,
    supervisor_audit_checks,
    supervisor_audit_evidence_input,
    supervisor_audit_evidence_models,
)
from agent_session_loop.assurance_models_vocab import RUBRIC_LANES
from tests.support import (
    init_git_repo,
)

RUN_ID = "20260810T101112123456Z-4242"

#: Injected so an audit's timestamps are inputs rather than wall-clock reads.
FIXED_CLOCK = datetime(2026, 8, 10, 10, 11, 12, tzinfo=UTC)

#: Historical critical identity; snapshot activity must not reopen it.
MUTATION_INVARIANT_KEY = "audit-read-only-mutation"

#: A run other than the one under test, so a seeded ledger issue is never
#: mistaken for the audit that run still owes.
SEED_RUN_ID = "20260810T101112999999Z-4243"
SEED_TIMESTAMP = "2026-08-09T00:00:00+00:00"

#: The moment a defect was declared fixed, and records either side of it.
FIX_TIMESTAMP = "2026-08-10T00:00:00+00:00"
BEFORE_FIX_TIMESTAMP = "2026-08-09T12:00:00+00:00"
AFTER_FIX_TIMESTAMP = "2026-08-11T00:00:00+00:00"


def _summary(state_name: str = "all-clear", **overrides: object):
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "state": state_name,
        "eligible": True,
    }
    values.update(overrides)
    return assurance_models_summary.AssuranceSummary(**values)  # type: ignore[arg-type]


class _RunHistory:
    """One recorded run on disk, before its terminal record is written."""

    MANIFEST = (
        "# Manifest\n\n## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        "| app | app | https://example.invalid/app.git |\n"
    )

    def _history(self, tmp: Path, *, turns: int = 1) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state" / plans_files.path_sha256(plan_dir)
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(self.MANIFEST, encoding="utf-8")
        (plan_dir / "session-01.md").write_text(
            "# Session 1\n\n"
            f"{plans_session_scope.READS_REPOSITORIES_PREFIX} app\n"
            f"{plans_session_scope.WRITES_REPOSITORIES_PREFIX} app\n",
            encoding="utf-8",
        )
        (plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "workspace_root": str(workspace)}),
            encoding="utf-8",
        )

        for index in range(1, turns + 1):
            turn_dir = run_dir / f"turn-{index:04d}-01"
            turn_dir.mkdir()
            (turn_dir / state.RESULT_FILE_NAME).write_text(
                json.dumps(self._result(workspace, turn_dir)), encoding="utf-8"
            )
            (turn_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
            (turn_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
            (turn_dir / "last-message.txt").write_text("Done.\n", encoding="utf-8")

        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(self._state(workspace)), encoding="utf-8"
        )
        # The terminal record is deliberately absent: this is the history an
        # audit reads when it runs ahead of its own run's run_finished.
        (state_dir / events.EVENTS_FILE_NAME).write_text(
            json.dumps(
                {
                    "name": "run_started",
                    "version": events.EVENT_SCHEMA_VERSION,
                    "created_at": "2026-08-10T10:11:12Z",
                    "payload": {"run_id": RUN_ID},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return workspace, plan_dir, state_dir, run_dir

    def _result(self, workspace: Path, turn_dir: Path) -> dict[str, object]:
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

    def _state(self, workspace: Path) -> dict[str, object]:
        return {
            "harness": "codex",
            "model": "gpt-5",
            "ok": True,
            "paused": False,
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

    def _bundle(
        self, history: tuple[Path, Path, Path, Path], *, terminal_pending: bool
    ) -> supervisor_audit_evidence_models.AuditInput:
        workspace, plan_dir, state_dir, run_dir = history
        return supervisor_audit_evidence_input.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            terminal_pending=terminal_pending,
        )

    def _verdict(
        self, bundle: supervisor_audit_evidence_models.AuditInput, check: str
    ) -> assurance_models_findings.DeterministicResult:
        for result in supervisor_audit_checks.run_deterministic_checks(bundle):
            if result.check == check:
                return result
        raise AssertionError(f"no deterministic check named {check}")


class _ScriptedAuditor:
    """Stand-in auditor whose replies are consumed in order; the last repeats.

    ``interrupted`` returns what ``HarnessAdapter.run`` itself returns once it
    has absorbed a Ctrl+C: no exception escapes to the caller, only a result.
    """

    def __init__(self, *replies: str, interrupted: bool = False) -> None:
        self.replies = list(replies)
        self.interrupted = interrupted
        self.prompts: list[str] = []
        self.harnesses: list[str] = []

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        self.harnesses.append(name)
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
        return harnesses.HarnessResult(
            argv=argv,
            returncode=130 if self.interrupted else 0,
            stdout="",
            stderr="",
            last_message=self.replies[index],
            name=name,
            interrupted=self.interrupted,
        )


class _AuditorAbortedMidTurn(_ScriptedAuditor):
    """An auditor an operator aborts while its turn is in flight.

    ``enqueue`` stands in for the second ``asl control abort``: it appends the
    command the finalization drain reads, so the kill this returns is the one
    the runner's own poll armed rather than one the test staged directly.
    """

    def __init__(self, enqueue: Callable[[], None], *replies: str) -> None:
        super().__init__(*replies)
        self._enqueue = enqueue

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        signal = kwargs["abort"]
        assert isinstance(signal, harnesses.AbortSignal)
        self._enqueue()
        deadline = time.monotonic() + 10.0
        while not signal.armed:
            if time.monotonic() > deadline:
                raise AssertionError("the finalization drain never armed the abort")
            time.sleep(0.01)
        return dataclasses.replace(
            super().__call__(name, **kwargs), returncode=137, aborted=True
        )


def _fast_finalization() -> Callable[..., live_verification._LiveFinalization]:
    """``_LiveFinalization`` whose control drain ticks at test speed."""
    live = live_verification._LiveFinalization

    def build(**kwargs: object) -> live_verification._LiveFinalization:
        kwargs.setdefault("poll_interval", 0.01)
        return live(**kwargs)  # type: ignore[arg-type]

    return build


def _all_clear_reply(locator: str, *, unjudged: tuple[str, ...] = ()) -> str:
    body = json.dumps(
        {
            "schema_version": supervisor_audit_agent.AGENT_OUTPUT_SCHEMA_VERSION,
            "lanes": [
                {
                    "lane": lane,
                    "status": "unavailable" if lane in unjudged else "pass",
                    "summary": f"{lane} judged from the recorded evidence",
                    "evidence": [locator],
                }
                for lane in RUBRIC_LANES
            ],
            "findings": [],
        }
    )
    return f"```json\n{body}\n```\n"
