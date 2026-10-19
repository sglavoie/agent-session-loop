# ruff: noqa: F401, F403, F405, I001
"""``asl diagnostics export --agent-mux`` tests.

The export exists so AgentMux never parses ``state.json``, ``events.jsonl`` or
``mailbox.jsonl``, and it is only worth that trust if three properties hold
under adversarial input: it never emits a body or a secret, it never grows
without bound, and it produces the same bytes twice over frozen inputs. Each of
those is asserted against the *whole* serialized document rather than against
the field that was supposed to carry the value, because the failure that
matters is the one where a body reaches an unexpected key.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from agent_session_loop import (
    agent_mux_projection,
    cli,
    diagnostics_export,
    events,
    mailbox_constants,
    plans_files,
)

#: Strings that must never survive into an export. Each one stands for a class
#: of value the document is forbidden to carry: an operator's words, a model's
#: words, a credential, and a transport's raw diagnostics.
CANARIES: tuple[str, ...] = (
    "QUESTION-BODY-CANARY-DO-NOT-EXPORT",
    "REPLY-BODY-CANARY-DO-NOT-EXPORT",
    "RESOLUTION-NOTE-CANARY-DO-NOT-EXPORT",
    "CONTROL-MESSAGE-CANARY-DO-NOT-EXPORT",
    "PROMPT-CANARY-DO-NOT-EXPORT",
    "sk-livesecret000000000000000000000000",
    "ghp_livesecret0000000000000000000000000",
    "AKIAIOSFODNN7EXAMPLE",
    "Bearer abc.def.ghi",
    "password=hunter2",
    "bridge: stderr canary traceback line",
)

RUN_ID = "run-export-0001"
SOURCE_EVENT_ID = "src-0001"
REQUEST_ID = "req-0001"
OPERATION_ID = "op-0001"
CLOSURE_ID = "clo-0001"


class ExportTestBase(unittest.TestCase):
    """A throwaway state root seeded with one fully-populated plan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans_files.canonicalize_path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir(parents=True)

    # -- seeding ---------------------------------------------------------

    def plan_state_dir(self, plan_dir: Path) -> Path:
        target = self.state_dir / plans_files.path_sha256(plan_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def seed_state(
        self,
        plan_dir: Path,
        *,
        run_id: str = RUN_ID,
        projection: Any = "default",
        assurance: bool = True,
        raw: str | None = None,
    ) -> Path:
        target = self.plan_state_dir(plan_dir)
        path = target / events.STATE_FILE_NAME
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return target
        payload: dict[str, Any] = {
            "version": agent_mux_projection.STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "plan_dir": str(plan_dir),
            "run_started_at": "2026-01-01T00:00:00Z",
            agent_mux_projection.GENERATION_KEY: 7,
            agent_mux_projection.LIFECYCLE_SEQUENCE_KEY: 4,
            # A private field carrying a body. Nothing may copy it.
            "last_prompt": CANARIES[4],
        }
        if projection == "default":
            payload[agent_mux_projection.PROJECTION_KEY] = {
                "schema_version": agent_mux_projection.PROJECTION_SCHEMA_VERSION,
                "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                "run_id": run_id,
                "plan_dir": str(plan_dir),
                "workspace_root": str(self.tmp_path),
                "registry_digest": "d" * 64,
                "harness": "claude",
                "kitty_window_id": 12,
                "phase": agent_mux_projection.PHASE_PAUSED,
                "session_id": "s1",
                "turn_index": 3,
                "question_id": "q-0001",
                "lifecycle_source_sequence": 4,
                "projection_generation": 7,
                "process": {"pid": 4242, "start_token": "token-1"},
                "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "finished_at": None,
                # A field the projection contract does not define, planted to
                # prove the export copies an allowlist rather than a payload.
                "unexpected_body": CANARIES[0],
            }
        elif projection is not None:
            payload[agent_mux_projection.PROJECTION_KEY] = projection
        if assurance:
            payload["assurance"] = {
                "schema_version": 1,
                "run_id": run_id,
                "state": "findings",
                "eligible": True,
                "reason": None,
                "issue_ids": [f"issue-{index:03d}" for index in range(15)],
                "new_issue_count": 2,
                "recurring_issue_count": 1,
                "attempts": [{"detail": CANARIES[10]}],
                "artifacts": [{"path": "audit.json", "note": CANARIES[9]}],
                "started_at": "2026-01-02T00:00:00Z",
                "completed_at": "2026-01-02T00:01:00Z",
                "duration_seconds": 60,
            }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def seed_events(self, plan_dir: Path, *, extra: int = 0) -> None:
        target = self.plan_state_dir(plan_dir)
        lines = [
            {
                "name": events.RUN_STARTED_EVENT,
                "payload": {"run_id": RUN_ID, "prompt": CANARIES[4]},
            },
            {
                "name": events.AGENT_MUX_DELIVERY_EVENT,
                "payload": {
                    "run_id": RUN_ID,
                    "agent_mux_event": "run_paused",
                    "harness": "claude",
                    "source_event_id": SOURCE_EVENT_ID,
                    "source_sequence": 4,
                    "request_id": REQUEST_ID,
                    "trace_id": "trace-0001",
                    "attempt_count": 2,
                    "final_status": "accepted",
                    "reason": None,
                    "error_classification": None,
                    "acknowledged": True,
                    # Per-attempt detail quotes the bridge's stderr.
                    "attempts": [
                        {"status": "retryable_failure", "detail": CANARIES[10]}
                    ],
                },
            },
            {
                "name": events.RUN_CONTROL_EVENT,
                "payload": {
                    "run_id": RUN_ID,
                    "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                    "command_id": OPERATION_ID,
                    "queue_command": "stop-after-current",
                    "stop_after_current": True,
                    "message": CANARIES[3],
                    "session": "s1",
                    "turn_index": 3,
                    "total_sessions": 14,
                },
            },
        ]
        for index in range(extra):
            lines.append(
                {
                    "name": events.AGENT_MUX_DELIVERY_EVENT,
                    "payload": {
                        "run_id": RUN_ID,
                        "agent_mux_event": "turn_finished",
                        "source_event_id": f"src-extra-{index:04d}",
                        "request_id": f"req-extra-{index:04d}",
                        "final_status": "accepted",
                        "acknowledged": True,
                    },
                }
            )
        text = "".join(json.dumps(line) + "\n" for line in lines)
        (target / events.EVENTS_FILE_NAME).write_text(text, encoding="utf-8")

    def seed_mailbox(self, plan_dir: Path, *, extra: int = 0) -> None:
        target = self.plan_state_dir(plan_dir)
        lines = [
            {
                "seq": 1,
                "id": "msg-0001",
                "kind": "question",
                "run_id": RUN_ID,
                "session_id": "s1",
                "turn_index": 3,
                "created_at": "2026-01-02T00:00:00Z",
                "text": CANARIES[0],
            },
            {
                "seq": 2,
                "id": "msg-0002",
                "kind": "reply",
                "run_id": RUN_ID,
                "to": "msg-0001",
                "created_at": "2026-01-02T00:00:10Z",
                "text": CANARIES[1],
            },
            {
                "seq": 3,
                "id": "msg-0003",
                "kind": "resolution",
                "run_id": RUN_ID,
                "action": "resolve",
                "closure_id": CLOSURE_ID,
                "created_at": "2026-01-02T00:00:20Z",
                "note": CANARIES[2],
            },
            {
                "seq": 4,
                "id": "msg-0004",
                "kind": "receipt",
                "run_id": RUN_ID,
                "closure_id": CLOSURE_ID,
                "created_at": "2026-01-02T00:00:21Z",
            },
        ]
        for index in range(extra):
            lines.append(
                {
                    "seq": 5 + index,
                    "id": f"msg-extra-{index:04d}",
                    "kind": "question",
                    "run_id": RUN_ID,
                    "created_at": "2026-01-02T00:01:00Z",
                    "text": CANARIES[0],
                }
            )
        text = "".join(json.dumps(line) + "\n" for line in lines)
        (target / mailbox_constants.MAILBOX_FILE_NAME).write_text(
            text, encoding="utf-8"
        )

    def seed_full_plan(self, name: str = "plan-a", **kwargs: Any) -> Path:
        plan_dir = self.tmp_path / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        self.seed_state(plan_dir, **kwargs)
        self.seed_events(plan_dir)
        self.seed_mailbox(plan_dir)
        return plan_dir

    # -- helpers ---------------------------------------------------------

    def export(self, **kwargs: Any) -> dict[str, Any]:
        return diagnostics_export.build_export(state_dir=self.state_dir, **kwargs)

    def rendered(self, document: dict[str, Any]) -> str:
        return diagnostics_export.canonical_json_bytes(document).decode("utf-8")

    def assert_no_canaries(self, document: dict[str, Any]) -> None:
        text = self.rendered(document)
        for canary in CANARIES:
            self.assertNotIn(canary, text)

    def omission(self, document: dict[str, Any], section: str, reason: str) -> Any:
        for entry in document["omissions"]:
            if entry["section"] == section and entry["reason"] == reason:
                return entry
        return None


__all__ = (
    "hashlib",
    "io",
    "json",
    "os",
    "tempfile",
    "unittest",
    "redirect_stderr",
    "redirect_stdout",
    "Path",
    "Any",
    "agent_mux_projection",
    "cli",
    "diagnostics_export",
    "events",
    "mailbox_constants",
    "plans_files",
    "CANARIES",
    "RUN_ID",
    "SOURCE_EVENT_ID",
    "REQUEST_ID",
    "OPERATION_ID",
    "CLOSURE_ID",
    "ExportTestBase",
)
