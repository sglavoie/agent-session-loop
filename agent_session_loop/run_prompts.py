"""The typed boundary that turns run state into the text a worker reads.

Every string a harness turn is launched with is assembled here: the legacy
``session-run`` skill invocation, the repository-authority block that bounds a
multi-repository session, the live mailbox protocol, the urgent-correction block
an interrupted attempt resumes with, and the two continuation blocks — an
operator reply and an answerless supervisor resolution — that a resumed turn
carries ahead of its own work.

This boundary writes **no durable record at all**. It reads a resolved
workspace, a session scope, a verification context, and already-fetched
mailbox messages, and it returns strings, line lists, and frozen records. The
run loop remains the only thing that decides *when* a prompt is built, which
of them is persisted into a turn record, and what happens to the process the
prompt is handed to. That is why the module needs no persistence seam: there
is nothing here for a seam to serialize.

Three invariants survive the move out of ``runner.py`` unchanged:

* **The skill invocation stays first.** ``build_session_prompt`` puts
  ``{skill_prefix}{SESSION_RUN_SKILL_NAME} PLAN_DIR --runner-managed`` on line
  one, and both continuation builders reuse its output byte-for-byte as their
  base rather than re-deriving it, so a resumed turn and a fresh turn open
  identically no matter which harness adapter is in play.
* **Untrusted text is framed, never interpolated.** Pending guidance, an
  operator reply, a resolution note, and an urgent correction are rendered as
  escaped ASCII JSON inside named ``BEGIN``/``END`` delimiters, so a body that
  looks like prompt markup cannot alter the packet around it. The rendering
  budget that decides which guidance records fit belongs to
  :mod:`mailbox_rendering`; this module only supplies the section builder it
  calls back into.
* **Bounded repository authority.** ``_workspace_prompt_lines`` publishes the
  full ordered registry but resolves roots only for the aliases the selected
  session may read, so a prompt never hands a worker a path outside its own
  scope.

Nothing here imports the runner. The plan directory, harness name, workspace,
scope, verification context, and mailbox records all enter as parameters, and
:class:`_PendingReply` and :class:`_PendingResolution` are the frozen records
the loop carries between the turn that paused and the turn that resumes.
"""

from __future__ import annotations

import json as json
import shlex as shlex
import sys as sys
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass
from pathlib import Path as Path
from typing import Any as Any

from . import harnesses as harnesses
from . import mailbox_models as mailbox_models
from . import mailbox_rendering as mailbox_rendering
from . import mailbox_vocabulary as mailbox_vocabulary
from . import plans_errors as plans_errors
from . import plans_files as plans_files
from . import plans_models as plans_models
from . import plans_session_scope as plans_session_scope
from . import qna as qna
from . import redirect_ack as redirect_ack
from . import verification_models as verification_models
from . import workspaces as workspaces
from .run_prompt_continuations import REPLY_CONTEXT_HEADING as REPLY_CONTEXT_HEADING
from .run_prompt_continuations import REPLY_MESSAGE_BEGIN as REPLY_MESSAGE_BEGIN
from .run_prompt_continuations import REPLY_MESSAGE_END as REPLY_MESSAGE_END
from .run_prompt_continuations import REPLY_ORIGIN_ARGV as REPLY_ORIGIN_ARGV
from .run_prompt_continuations import REPLY_ORIGIN_MAILBOX as REPLY_ORIGIN_MAILBOX
from .run_prompt_continuations import REPLY_ORIGIN_STDIN as REPLY_ORIGIN_STDIN
from .run_prompt_continuations import REPLY_ORIGINS as REPLY_ORIGINS
from .run_prompt_continuations import (
    RESOLUTION_CONTEXT_HEADING as RESOLUTION_CONTEXT_HEADING,
)
from .run_prompt_continuations import (
    RESOLUTION_MESSAGE_BEGIN as RESOLUTION_MESSAGE_BEGIN,
)
from .run_prompt_continuations import RESOLUTION_MESSAGE_END as RESOLUTION_MESSAGE_END
from .run_prompt_continuations import _PendingReply as _PendingReply
from .run_prompt_continuations import _PendingResolution as _PendingResolution
from .run_prompt_continuations import _reply_context_body as _reply_context_body
from .run_prompt_continuations import _reply_display as _reply_display
from .run_prompt_continuations import (
    _resolution_context_body as _resolution_context_body,
)
from .run_prompt_continuations import build_reply_prompt as build_reply_prompt
from .run_prompt_continuations import build_resolution_prompt as build_resolution_prompt
from .run_prompt_continuations import read_reply_from_stdin as read_reply_from_stdin
from .run_prompt_mailbox import CORRECTION_CONTEXT_HEADING as CORRECTION_CONTEXT_HEADING
from .run_prompt_mailbox import CORRECTION_MESSAGE_BEGIN as CORRECTION_MESSAGE_BEGIN
from .run_prompt_mailbox import CORRECTION_MESSAGE_END as CORRECTION_MESSAGE_END
from .run_prompt_mailbox import MAILBOX_GUIDANCE_BEGIN as MAILBOX_GUIDANCE_BEGIN
from .run_prompt_mailbox import MAILBOX_GUIDANCE_END as MAILBOX_GUIDANCE_END
from .run_prompt_mailbox import MAILBOX_GUIDANCE_HEADING as MAILBOX_GUIDANCE_HEADING
from .run_prompt_mailbox import MAILBOX_PROTOCOL_HEADING as MAILBOX_PROTOCOL_HEADING
from .run_prompt_mailbox import _correction_context_body as _correction_context_body
from .run_prompt_mailbox import (
    _mailbox_acknowledge_command_text as _mailbox_acknowledge_command_text,
)
from .run_prompt_mailbox import _mailbox_ask_command_text as _mailbox_ask_command_text
from .run_prompt_mailbox import (
    _mailbox_notify_command_text as _mailbox_notify_command_text,
)
from .run_prompt_mailbox import _mailbox_protocol_lines as _mailbox_protocol_lines
from .run_prompt_mailbox import _mailbox_read_command_text as _mailbox_read_command_text
from .run_prompt_session import RUNNER_MANAGED_SESSION_ARG as RUNNER_MANAGED_SESSION_ARG
from .run_prompt_session import SESSION_RUN_SKILL_NAME as SESSION_RUN_SKILL_NAME
from .run_prompt_session import build_harness_invocation as build_harness_invocation
from .run_prompt_session import build_session_prompt as build_session_prompt
from .run_prompt_workspace import _claimed_repository_roots as _claimed_repository_roots
from .run_prompt_workspace import _prompt_plan_dir as _prompt_plan_dir
from .run_prompt_workspace import (
    _resolved_repositories_payload as _resolved_repositories_payload,
)
from .run_prompt_workspace import _session_scope as _session_scope
from .run_prompt_workspace import _workspace_payload_fields as _workspace_payload_fields
from .run_prompt_workspace import _workspace_prompt_lines as _workspace_prompt_lines
