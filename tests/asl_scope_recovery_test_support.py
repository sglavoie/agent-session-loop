"""Public CLI regressions for explicit, evidence-bound remote-ref recovery."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    events,
    plans_metadata,
    plans_registry,
    plans_session_scope,
    plans_snapshot,
    repository_evidence,
    scope_recovery,
    scope_recovery_ref_updates,
    state,
    state_locks,
    state_status,
    turn_preflight,
    workspace_evidence,
    workspaces,
)
from tests.support import (
    git_stdout,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)


class ExternalRecoveryTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        specs = (
            ("notes", "notes", "git@github.com:example/notes.git"),
            ("pi", "pi", "git@github.com:example/pi.git"),
        )
        self.repos = init_multi_repo_workspace(self.workspace, specs)
        self.plan = self.root / "plan"
        write_multi_repo_plan(self.plan, ("pending", "pending"), specs=specs)
        self.session = self.plan / "session-1.md"
        self.session.write_text(
            self.session.read_text()
            .replace(
                "**Reads repositories**: notes, pi", "**Reads repositories**: notes"
            )
            .replace(
                "**Writes repositories**: notes, pi", "**Writes repositories**: notes"
            )
        )
        registry = plans_registry.read_repository_registry(self.plan)
        self.resolved = workspaces.resolve_workspace(self.workspace, registry)
        plans_metadata.save_workspace_plan_metadata(
            self.plan, self.workspace, registry.digest
        )
        self.state_dir = self.root / "state"
        self.event_path = (
            state.plan_state_dir(self.plan, self.state_dir) / events.EVENTS_FILE_NAME
        )
        events.append_event(
            self.event_path,
            events.Event(
                name=events.RUN_STARTED_EVENT,
                payload={
                    "run_id": "rejected",
                    "workspace_root": str(self.resolved.root),
                    "registry_digest": registry.digest,
                    "commit_mode": "per-session",
                    "repositories": [
                        dict(
                            alias=r.alias,
                            path=r.repository.path,
                            origin=r.repository.origin,
                            resolved_root=str(r.root),
                        )
                        for r in self.resolved.repositories
                    ],
                },
            ),
        )
        self.before = self.capture()
        (self.repos["notes"] / "README.md").write_text("retained work\n")
        git_stdout(self.repos["pi"], "commit", "--allow-empty", "-m", "independent")
        self.linked = self.root / "publication"
        git_stdout(
            self.repos["notes"],
            "worktree",
            "add",
            "-b",
            "publication",
            str(self.linked),
        )
        self.after = self.capture()
        self.guard = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.resolved,
            scope=plans_session_scope.SessionScope(reads=("notes",), writes=("notes",)),
            comparison=repository_evidence.compare_workspace_snapshots(
                self.before, self.after
            ),
            exclude=(),
            include_ignored=False,
        )
        self.guard.update(
            session_id="1", snapshot_before=self.before, snapshot_after=self.after
        )
        self.pause()
        (self.repos["pi"] / "independent.txt").write_text("independent dirt\n")

    def capture(self):
        return repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan, ("notes", "pi")
        )

    def pause(self):
        events.append_event(
            self.event_path,
            events.Event(
                name=events.RUN_PAUSED_EVENT,
                payload={
                    "run_id": "rejected",
                    "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                    "details": {"session_id": "1", "workspace_write_guard": self.guard},
                },
            ),
        )

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "acknowledge-external-changes",
                    str(self.plan),
                    "--session",
                    "1",
                    "--repository",
                    "pi",
                    "--state-dir",
                    str(self.state_dir),
                    "--json",
                    *args,
                ]
            )
        return code, json.loads(output.getvalue())

    def accept_args(self, preview):
        return (
            "--accept",
            preview["evidence_digest"],
            "--reason",
            "Independent Pi work authorized",
            "--checkpoint-retained",
        )


__all__ = (
    "contextlib",
    "copy",
    "io",
    "json",
    "tempfile",
    "unittest",
    "Path",
    "mock",
    "cli",
    "events",
    "plans_metadata",
    "plans_registry",
    "plans_session_scope",
    "plans_snapshot",
    "repository_evidence",
    "scope_recovery",
    "scope_recovery_ref_updates",
    "state",
    "state_locks",
    "state_status",
    "turn_preflight",
    "workspace_evidence",
    "workspaces",
    "git_stdout",
    "init_multi_repo_workspace",
    "write_multi_repo_plan",
    "write_plan",
    "ExternalRecoveryTestCase",
)
