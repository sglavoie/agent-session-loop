"""Shared fixtures for support asl native restore recovery."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    conversation_resume,
    conversations,
    harness_codex,
    harness_process,
    harnesses,
    redirect_store_journal,
    redirect_store_models,
    state,
)
from tests.asl_redirect_continuation_support import (
    SESSION_ID,
    WORKER_PID,
)
from tests.asl_redirect_run_support import _RedirectedRun
from tests.support_asl_codex_app_server import (
    FAKE_APP_SERVER_FIXTURE,
    PROMPT,
    WAIT_SECONDS,
)
from tests.test_asl_harness_lifecycle import _FakePipe

THREAD_ID = "thread-77"


RUN_CONVERSATION_ID = "conv-1"


UNRELATED_NO_SUCH_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "no such host: api.provider.example",
}


SESSION_FILE_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "session file not found: /etc/provider/session-config.json",
}


METHOD_NAME_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "no such session.load method",
}


CONFIG_FILE_NAME_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "session config.json not found",
}


NOT_A_MISSING_HISTORY: tuple[dict[str, Any], ...] = (
    {"code": -32603, "message": "internal error"},
    {"code": -32603, "message": "temporary service unavailable; retry later"},
    {"code": -32000, "message": "authentication required: the token has expired"},
    {"code": -32000, "message": "permission denied for this workspace"},
    {"code": -32000, "message": UNRELATED_NO_SUCH_ERROR["message"]},
    {"code": -32000, "message": "no such model: missing-model-id"},
    {"code": -32000, "message": "no such workspace: /workspace"},
    SESSION_FILE_ERROR,
    {"code": -32000, "message": "thread pool not found"},
    {"code": -32000, "message": "no such session/load method"},
    {"code": -32000, "message": "unknown session storage backend"},
    METHOD_NAME_ERROR,
    CONFIG_FILE_NAME_ERROR,
)


def _install(fixture: Path, destination: Path) -> Path:
    shutil.copyfile(fixture, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    return destination


def codex_resume_result(
    tmp: Path,
    *,
    conversation: str,
    mode: str = "resume-unknown",
    resume_error: dict[str, Any] | None = None,
) -> harnesses.HarnessResult:
    """One real codex resume against an app-server that answers it with an error.

    A module-level function rather than a test method because two very different
    cases need the same result: the adapter claims below, and the whole-loop runs
    that have to be driven by results the transport actually produces. Which
    error it is answered with is the parameter, because the two whole-loop
    stories differ in nothing else.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    executable = _install(FAKE_APP_SERVER_FIXTURE, tmp / "codex-app-server")
    adapter = harness_codex.CodexAdapter(executable=str(executable))
    plan_dir = tmp / "plan"
    plan_dir.mkdir(exist_ok=True)
    last_message = tmp / "last-message.txt"
    with (
        mock.patch.object(harness_process, "sweep_enabled", return_value=False),
        mock.patch.dict(
            "os.environ",
            {
                "FAKE_APP_SERVER_MODE": mode,
                "FAKE_APP_SERVER_RECORD": str(tmp / "record.json"),
                "FAKE_APP_SERVER_RESUME_ERROR": json.dumps(resume_error or {}),
            },
        ),
    ):
        return adapter.run(
            cwd=tmp,
            plan_dir=plan_dir,
            prompt=PROMPT,
            last_message_path=last_message,
            timeout=WAIT_SECONDS,
            invocation=adapter.build_invocation(
                cwd=tmp,
                plan_dir=plan_dir,
                prompt=PROMPT,
                last_message_path=last_message,
                resume_conversation_id=conversation,
            ),
        )


class _BrokenPipe(_FakePipe):
    """A stdin whose reader is gone, like a CLI that exited at startup."""

    def write(self, text: str) -> int:  # type: ignore[override]
        raise BrokenPipeError(32, "Broken pipe")


class _NativeRestoreRun(_RedirectedRun):
    """What both whole-loop stories share: a turn that never got its prompt out.

    Each story runs the same loop and differs only in the sentence the
    app-server answers ``thread/resume`` with, so the hooks that say how far the
    refused turn got, and the two lookups its consequences are read through,
    belong to neither of them.
    """

    def _spawned(self, **kwargs: Any) -> None:
        """Report the spawn, and nothing beyond it.

        A spawn is as far as a refused restore ever gets. The app-server process
        exists and this run is still holding the prompt: ``thread/resume`` is
        answered before ``turn/start``, so nothing was submitted and no worker
        ever read the correction the prompt was carrying.
        """
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(WORKER_PID)

    def _submitted(self, **kwargs: Any) -> None:
        """Report a spawn whose handshake went through and whose prompt went out.

        The pair of hooks is the distinction this fixture exists to exercise: a
        worker that reached here holds the operator's words, and one that
        stopped at :meth:`_spawned` does not.
        """
        self._spawned(**kwargs)
        on_prompt_submitted = kwargs.get("on_prompt_submitted")
        if on_prompt_submitted is not None:
            on_prompt_submitted()

    def _selection(self) -> conversations.ConversationSelection:
        """What a later launch's lookup now finds for this session."""
        return conversations.select_conversation(
            state.plan_state_dir(self.plan_dir, self.state_dir),
            conversation_resume.turn_binding(
                plan_dir=self.plan_dir,
                session_id=SESSION_ID,
                harness="codex",
                cwd=self.repo_root,
            ),
        )

    def _request(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request
