from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any

from agent_session_loop import overseer_history, overseer_policy_client
from tests.support import assert_no_runner_import

PLAN = "/state/agent-session-loop/plan-abc"
AMX = "/usr/local/bin/amx"
FINGERPRINT = "fp-0123456789abcdef"
DIGEST = "rd-fedcba9876543210"

REPOSITORIES = (
    overseer_policy_client.Repository(alias="scripts", path="/home/dev/scripts"),
    overseer_policy_client.Repository(alias="api", path="/home/dev/api"),
)

DEFAULT_LIMITS = overseer_history.Limits()


def _outcome(
    repository: overseer_policy_client.Repository, *, allowed: bool
) -> dict[str, Any]:
    return {
        "alias": repository.alias,
        "path": repository.path,
        "resolved_path": repository.path,
        "allowed": allowed,
        "reason": "opted_in" if allowed else "excluded",
        "detail": "",
    }


def _excerpt(index: int, *, role: str = "user", text: str = "hello") -> dict[str, Any]:
    return {
        "index": index,
        "role": role,
        "timestamp": "2026-01-02T03:04:05Z",
        "text": text,
        "truncated": False,
    }


def _conversation(
    *,
    session_id: str = "sess-1",
    directory: str = "/home/dev/scripts",
    excerpts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "harness": "claude",
        "id": session_id,
        "profiles": ["default"],
        "directory": directory,
        "projects": ["scripts"],
        "path": f"/home/dev/.claude/projects/scripts/{session_id}.jsonl",
        "started_at": "2026-01-02T03:00:00Z",
        "last_at": "2026-01-02T03:30:00Z",
        "session_class": "main",
        "message_count": 42,
        "truncated": True,
        "excerpts": excerpts if excerpts is not None else [_excerpt(40), _excerpt(41)],
    }


def _payload(
    *,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    allowed: bool = True,
    repositories: tuple[overseer_policy_client.Repository, ...] = REPOSITORIES,
    limits: overseer_history.Limits = DEFAULT_LIMITS,
    conversations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A well-formed answer to the question the tests ask by default."""
    return {
        "schema_version": overseer_history.HISTORY_SCHEMA_VERSION,
        "data": {
            "contract": {
                "name": overseer_history.HISTORY_CONTRACT_NAME,
                "version": overseer_history.HISTORY_CONTRACT_VERSION,
                "access_categories": list(overseer_policy_client.ACCESS_CATEGORIES),
            },
            "plan": plan_dir,
            "access": access,
            "allowed": allowed,
            "reason": "allowed" if allowed else "unenrolled",
            "summary": "authorized: 1 of 3 conversations returned, 2 withheld",
            "config_fingerprint": FINGERPRINT,
            "request_digest": DIGEST,
            "repositories": [
                _outcome(repository, allowed=allowed) for repository in repositories
            ],
            "limits": {
                "conversations": limits.conversations,
                "messages": limits.messages,
                "excerpt_chars": limits.excerpt_chars,
            },
            "harnesses": [],
            "examined": 3,
            "truncated": False,
            "conversations": (
                conversations
                if conversations is not None
                else ([_conversation()] if allowed else [])
            ),
            "withheld": (
                [
                    {"harness": "codex", "id": "sess-2", "reason": "out_of_scope"},
                    {
                        "harness": "claude",
                        "id": "sess-3",
                        "reason": "ambiguous_project",
                    },
                ]
                if allowed
                else []
            ),
            "warnings": [],
        },
    }


class _CannedAmx:
    """A ``run_command`` seam that replays one canned answer and records the argv."""

    def __init__(
        self,
        *,
        stdout: str = "",
        returncode: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout=self.stdout, stderr=""
        )


def _read(
    *,
    payload: Any = None,
    stdout: str | None = None,
    returncode: int = 0,
    raises: BaseException | None = None,
    amx_path: str | None = AMX,
    plan_dir: str = PLAN,
    repositories: tuple[overseer_policy_client.Repository, ...] = REPOSITORIES,
    limits: overseer_history.Limits | None = None,
    harnesses: tuple[str, ...] = (),
    since: str = "",
) -> tuple[overseer_history.History, _CannedAmx]:
    text = stdout if stdout is not None else json.dumps(payload)
    invoked = _CannedAmx(stdout=text, returncode=returncode, raises=raises)
    history = overseer_history.read_history(
        plan_dir=plan_dir,
        repositories=repositories,
        limits=limits,
        harnesses=harnesses,
        since=since,
        resolve_binary=lambda _name: amx_path,
        run_command=invoked,
    )
    return history, invoked
