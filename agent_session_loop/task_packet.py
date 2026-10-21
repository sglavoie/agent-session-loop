"""The typed boundary that builds the autonomous worker's task packet.

An autonomous ``asl run`` turn is opened by a packet, not by a skill call.
This module is the entry point of that boundary and keeps its public API: the
worker-mode switch that decides whether a run sends a packet at all, plus the
:class:`SessionTaskPacket` record, the :func:`build_task_packet` factory, and
:data:`TASK_PACKET_HEADING_PREFIX`, re-exported from the modules that own them:

* :mod:`agent_session_loop.task_packet_model` -- the frozen
  :class:`SessionTaskPacket` record.
* :mod:`agent_session_loop.task_packet_render` -- the prompt text and the
  heading prefix.
* :mod:`agent_session_loop.task_packet_build` -- the factory that validates its
  inputs.
* :mod:`agent_session_loop.task_packet_lookups` -- the two plan lookups
  (``_predecessor_session_id`` and ``_session_has_expansion``) and the
  body-free ``_resolution_source_metadata``.
* :mod:`agent_session_loop.task_packet_help` -- the paused-run
  ``interactive_help`` payload that tells an operator how to answer.

This boundary writes **no durable record**. It returns a frozen record, a
string, or a plain payload dictionary; the run loop remains the only thing
that decides when a packet is built, which prompt is persisted into a turn
record, and which process it is handed to. The ``interactive_help`` payload is
likewise handed back to the caller to embed, never written here.

Four invariants survive the move out of ``runner.py`` unchanged:

* **The heading is the packet's signature.** Every rendered packet starts with
  ``TASK_PACKET_HEADING_PREFIX``, which is how tests and operators tell a
  packet-mode turn from the legacy skill invocation on sight.
* **Packet mode fails safe.** :func:`_worker_mode` resolves anything other
  than a case-insensitive ``skill`` to :data:`WORKER_MODE_PACKET`, so a
  mistyped override cannot silently disable the default.
* **A continuation block never leaks forward.** An operator reply and an
  answerless supervisor resolution are mutually exclusive, each requires its
  own context, and both are supplied only on the turn that resumes, so
  reply context cannot reappear on a later turn. The urgent-correction block is
  a third, independent one — an operator may correct a turn that was itself
  resuming from a pause — and it cannot repeat either, because the journal
  records which attempt carried each correction.
* **Resolution identity carries no body.** ``_resolution_source_metadata``
  returns a fixed-shape identity — ids, sequence numbers, and the note's
  *length* — so a durable record can name a resolution without copying
  operator prose into it.

Nothing here imports the runner. The harness name, plan directory, session
id, workspace, scope, mailbox records, and the parsed argument namespace all
enter as parameters, and the prompt fragments this module composes come from
:mod:`agent_session_loop.run_prompts`, which is likewise runner-free.
"""

from __future__ import annotations

import os

from .task_packet_build import build_task_packet as build_task_packet
from .task_packet_model import SessionTaskPacket as SessionTaskPacket
from .task_packet_render import (
    TASK_PACKET_HEADING_PREFIX as TASK_PACKET_HEADING_PREFIX,
)

# Worker mode selects how autonomous ``asl run`` turns prompt the harness.
# ``packet`` (the default) sends a compact Python-built task packet
# (:func:`build_task_packet`) so autonomous runs never invoke the manual
# ``session-run`` skill. ``skill`` is a temporary rollback that restores the
# legacy ``session-run`` skill invocation
# (:func:`agent_session_loop.run_prompts.build_session_prompt`). The
# manual ``$session-run`` path is unaffected by this switch either way.
WORKER_MODE_ENV_VAR = "ASL_WORKER_MODE"
WORKER_MODE_PACKET = "packet"
WORKER_MODE_SKILL = "skill"


def _worker_mode() -> str:
    """Return the active worker mode, defaulting to packet mode.

    Any value other than a case-insensitive ``skill`` resolves to
    :data:`WORKER_MODE_PACKET` so an unrecognized override fails safe onto the
    new default rather than silently disabling packet mode.
    """
    raw = os.environ.get(WORKER_MODE_ENV_VAR)
    if raw is None:
        return WORKER_MODE_PACKET
    if raw.strip().lower() == WORKER_MODE_SKILL:
        return WORKER_MODE_SKILL
    return WORKER_MODE_PACKET
