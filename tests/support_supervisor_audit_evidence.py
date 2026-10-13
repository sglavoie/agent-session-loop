"""Shared fixtures for the supervisor-audit test modules."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    mailbox_constants,
    mailbox_vocabulary,
    plans,
    state,
    supervisor_audit_evidence,
)
from tests.support import init_git_repo
from tests.support_supervisor_audit import RUN_ID


class EvidenceBundleFixture(unittest.TestCase):
    """Shared fixtures for EvidenceBundleTests (split across modules)."""

    SECRET_ARGV = "ghp_AAAAAAAAAAAAAAAAAAAA"

    SECRET_ENV = "ghp_BBBBBBBBBBBBBBBBBBBB"

    SECRET_PASSWORD = "correct-horse-battery"

    APPLICATION_SOURCE = "def transfer_funds(amount):\n    return amount * 2\n"

    QUESTION_ID = "11111111-1111-4111-8111-111111111111"

    WAIT_ID = "22222222-2222-4222-8222-222222222222"

    REPLY_ID = "33333333-3333-4333-8333-333333333333"

    RECEIPT_ID = "44444444-4444-4444-8444-444444444444"

    WAIT_END_ID = "55555555-5555-4555-8555-555555555555"

    NOTICE_ID = "66666666-6666-4666-8666-666666666666"

    FOREIGN_QUESTION_ID = "77777777-7777-4777-8777-777777777777"

    FOREIGN_REPLY_ID = "88888888-8888-4888-8888-888888888888"

    FOREIGN_RUN_ID = "20260810T101112123456Z-9999"

    QUESTION_TEXT = "Two checks print `unknown flag: --docs`. Remove them?"

    REPLY_TEXT = "Option (a), done — removed both from session-15.1.md."

    def _mailbox_records(self) -> list[dict[str, Any]]:
        """One full exchange for this run, plus another run's question."""
        return [
            {
                "created_at": "2026-08-10T10:11:12Z",
                "id": self.QUESTION_ID,
                "kind": mailbox_vocabulary.KIND_QUESTION,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "text": self.QUESTION_TEXT,
                "delivery_mode": mailbox_vocabulary.DELIVERY_MODE_WAIT,
                "run_id": RUN_ID,
                "session_id": "01",
                "turn_index": 1,
            },
            {
                "created_at": "2026-08-10T10:11:13Z",
                "id": self.WAIT_ID,
                "kind": mailbox_vocabulary.KIND_WAIT_START,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "to": self.QUESTION_ID,
            },
            {
                "created_at": "2026-08-10T10:11:14Z",
                "id": self.FOREIGN_QUESTION_ID,
                "kind": mailbox_vocabulary.KIND_QUESTION,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "text": "Another run asks something else.",
                "delivery_mode": mailbox_vocabulary.DELIVERY_MODE_ASYNC,
                "run_id": self.FOREIGN_RUN_ID,
                "session_id": "01",
                "turn_index": 1,
            },
            {
                "created_at": "2026-08-10T10:11:15Z",
                "id": self.REPLY_ID,
                "kind": mailbox_vocabulary.KIND_REPLY,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "text": self.REPLY_TEXT,
                "to": self.QUESTION_ID,
            },
            {
                "created_at": "2026-08-10T10:11:16Z",
                "id": self.FOREIGN_REPLY_ID,
                "kind": mailbox_vocabulary.KIND_REPLY,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "text": "Answering the other run.",
                "to": self.FOREIGN_QUESTION_ID,
            },
            {
                "created_at": "2026-08-10T10:11:17Z",
                "id": self.RECEIPT_ID,
                "kind": mailbox_vocabulary.KIND_RECEIPT,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "to": self.QUESTION_ID,
                "closure_id": self.REPLY_ID,
                "wait_id": self.WAIT_ID,
            },
            {
                "created_at": "2026-08-10T10:11:18Z",
                "id": self.WAIT_END_ID,
                "kind": mailbox_vocabulary.KIND_WAIT_END,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "to": self.QUESTION_ID,
                "wait_id": self.WAIT_ID,
                "reason": mailbox_vocabulary.WAIT_END_REASON_CLOSURE,
                "closure_id": self.REPLY_ID,
            },
            {
                "created_at": "2026-08-10T10:11:19Z",
                "id": self.NOTICE_ID,
                "kind": mailbox_vocabulary.KIND_NOTICE,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                "text": "Session 01 resumed with the narrowed contract.",
                "run_id": RUN_ID,
                "session_id": "01",
                "turn_index": 1,
            },
        ]

    def _history(
        self,
        tmp: Path,
        *,
        stdout_bytes: int = 64,
        event_count: int = 3,
    ) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
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
        (turn_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(
                {
                    "ok": True,
                    "returncode": 0,
                    "invocation": {
                        "name": "codex",
                        "argv": ["codex", "--token", self.SECRET_ARGV, "-p", "go"],
                        "cwd": str(workspace / "app"),
                    },
                    "stdout": self.APPLICATION_SOURCE,
                    "diff": self.APPLICATION_SOURCE,
                    "verification": {
                        "checks": [{"name": "tests", "passed": True, "returncode": 0}]
                    },
                }
            ),
            encoding="utf-8",
        )
        (turn_dir / "stdout.log").write_text("o" * stdout_bytes, encoding="utf-8")
        (turn_dir / "stderr.log").write_text("e\n", encoding="utf-8")
        (turn_dir / "last-message.txt").write_text("Session done.\n", encoding="utf-8")

        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "harness": "codex",
                    "model": "gpt-5",
                    "ok": True,
                    "review_verdict": "approved",
                    "repositories": [
                        {
                            "alias": "app",
                            "path": "app",
                            "origin": "https://user:pw@github.com/owner/repo.git",
                            "resolved_root": str(workspace / "app"),
                        }
                    ],
                    "run_config": {
                        "env": {"GITHUB_TOKEN": self.SECRET_ENV},
                        "password": self.SECRET_PASSWORD,
                    },
                }
            ),
            encoding="utf-8",
        )
        with (state_dir / events.EVENTS_FILE_NAME).open(
            "w", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "name": "run_started",
                        "version": events.EVENT_SCHEMA_VERSION,
                        "created_at": "2026-08-10T10:11:12Z",
                        "payload": {"run_id": RUN_ID},
                    }
                )
                + "\n"
            )
            for index in range(event_count):
                handle.write(
                    json.dumps(
                        {
                            "name": events.TURN_HEARTBEAT_EVENT,
                            "version": events.EVENT_SCHEMA_VERSION,
                            "created_at": "2026-08-10T10:11:13Z",
                            "payload": {"run_id": RUN_ID, "index": index},
                        }
                    )
                    + "\n"
                )
            handle.write(
                json.dumps(
                    {
                        "name": events.RUN_FINISHED_EVENT,
                        "version": events.EVENT_SCHEMA_VERSION,
                        "created_at": "2026-08-10T10:11:14Z",
                        "payload": {"run_id": RUN_ID, "ok": True},
                    }
                )
                + "\n"
            )
        (state_dir / mailbox_constants.MAILBOX_FILE_NAME).write_text(
            "".join(json.dumps(record) + "\n" for record in self._mailbox_records()),
            encoding="utf-8",
        )
        return workspace, plan_dir, state_dir, run_dir

    def _collect(
        self,
        workspace: Path,
        plan_dir: Path,
        state_dir: Path,
        run_dir: Path,
        **overrides: object,
    ) -> supervisor_audit_evidence.AuditInput:
        return supervisor_audit_evidence.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            **overrides,
        )
