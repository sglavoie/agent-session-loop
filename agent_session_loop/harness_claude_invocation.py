"""Claude command construction, audit permissions, and session selection."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path

from . import claude_stream
from .harnesses import (
    TRANSPORT_STREAM_JSON,
    WORKER_ACCESS,
    AccessProfile,
    HarnessAdapter,
    HarnessError,
    HarnessInvocation,
)

#: Claude tools that read or search without mutating anything.
CLAUDE_AUDIT_TOOLS = "Read,Glob,Grep"

#: Claude tools denied outright, so an audit turn cannot edit, shell out, reach
#: the network, spawn a subagent, or write session state.
CLAUDE_AUDIT_DENIED_TOOLS = (
    "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
)


class ClaudeInvocationAdapter(HarnessAdapter):
    """Build Claude invocations for the executing adapter."""

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        # Claude's guard lane is the settings overlay written at spawn time,
        # not argv, so the turn environment says nothing this command line
        # needs.
        del cwd, last_message_path, env
        argv = [
            self.executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            # Input is a stream too, which is what makes a turn a conversation
            # rather than a one-shot: the prompt below is written to stdin as
            # the first message, and guidance can follow it while the turn runs.
            "--input-format",
            "stream-json",
            # Echoes prove which messages a result covers, including guidance
            # consumed inside the current tool loop rather than a new turn.
            "--replay-user-messages",
        ]
        if access.is_audit:
            for root in access.read_roots:
                argv.extend(["--add-dir", str(root)])
            argv.extend(
                [
                    "--permission-mode",
                    "plan",
                    "--tools",
                    CLAUDE_AUDIT_TOOLS,
                    "--allowed-tools",
                    CLAUDE_AUDIT_TOOLS,
                    "--disallowed-tools",
                    CLAUDE_AUDIT_DENIED_TOOLS,
                    "--strict-mcp-config",
                    "--no-session-persistence",
                    "--disable-slash-commands",
                ]
            )
        else:
            argv.extend(
                [
                    "--add-dir",
                    self._worker_plan_dir(plan_dir),
                    "--permission-mode",
                    "bypassPermissions" if unsafe_bypass else "dontAsk",
                ]
            )
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        # The prompt is deliberately absent from argv: under streaming input it
        # is the conversation's first stdin message, so it never reaches a
        # process listing, a crash report, or a printed invocation.
        del prompt
        return argv

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessInvocation:
        invocation = super().build_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
            env=env,
            resume_conversation_id=resume_conversation_id,
        )
        argv, assigned = self._with_session_controls(
            invocation.argv,
            access=access,
            resume_conversation_id=resume_conversation_id,
        )
        return dataclasses.replace(
            invocation,
            argv=argv,
            transport=TRANSPORT_STREAM_JSON,
            assigned_conversation_id=assigned,
        )

    def _with_session_controls(
        self,
        argv: list[str],
        *,
        access: AccessProfile,
        resume_conversation_id: str | None,
    ) -> tuple[list[str], str | None]:
        """Name this turn's conversation on argv, and say which one it is.

        Two supported controls and a deliberate omission. ``--resume`` restores
        exactly the conversation asked for; ``--session-id`` assigns one the CLI
        would otherwise have minted itself, which is what makes the identifier
        knowable before the prompt is written rather than only after the CLI's
        first record arrives. ``--fork-session`` is never passed, because it
        answers "continue conversation X" with a different one — the silent
        substitution this whole path exists to prevent.

        An audit turn gets neither: it runs with ``--no-session-persistence``,
        so there is no conversation to name, assign, or come back to.
        """
        if access.is_audit:
            if resume_conversation_id is not None:
                raise HarnessError(
                    "a claude audit turn cannot resume conversation "
                    f"{resume_conversation_id}; it persists no session"
                )
            return list(argv), None
        if resume_conversation_id is not None:
            return [*argv, "--resume", resume_conversation_id], resume_conversation_id
        assigned = claude_stream.new_session_id()
        return [*argv, "--session-id", assigned], assigned
