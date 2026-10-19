"""Everything settled about a selected session before its turn is built.

The run loop selects a session and then, before any harness process exists,
has to answer four questions about it that only the plan on disk can answer.
This module owns those four answers and nothing downstream of them: it stops
where the turn's own payload begins.

* **What kind of session is this?** A generated review session, a generated
  review *repair* child, or ordinary implementation work. Everything below
  branches on that classification, so it is resolved once, here, rather than
  re-derived from the file path at each site that cares.
* **Is a review session still describing the truth?** A pending review may have
  been authored by an earlier run: its contract, its file list and its
  per-repository bases are all static, and the working tree has moved since.
  :func:`prepare_review_session` re-derives all three from current Git evidence
  and rewrites the session file when any of them moved.
* **May this repair child run at all?** A materialized child's durable write
  scope can be narrower than the findings it now carries, the continuation may
  refuse it outright, and its writable set has to be clean before it starts.
  :func:`resume_repair_session` settles all three ahead of the turn.
* **What does the worker actually receive?** A reply continuation, a resolution
  continuation, or a fresh session start — each in packet mode or in the legacy
  skill mode — plus the repository-intent verdict re-derived from the exact
  text that prompt turned out to be.

Durable records this boundary writes, all through collaborators that already
own them: the **pending review session file** (its refreshed contract through
``review_session_contract``, its re-derived scope through ``review_delta``, and
its repository bases through ``review_session_body``), the **repair child's
write scope** (``workspace_evidence``), the plan's **active readiness state**
after either rewrite (``readiness_gate``), and the **repair commit preflight**
record and its event. The prompt itself is not durable here — the loop persists
it on the active turn — so nothing else this module produces reaches disk.

Nothing here imports the runner, and since Session 37 it does not import
``run_loop``, which owns ``RunContext``, either: the context enters as the
structural :class:`RunContext` Protocol below, naming only the members these
four steps reach.

Each step hands back a frozen result rather than performing the loop's
``break``: the branches this replaced ended in one, and a boundary cannot
break a loop it does not own. ``paused`` is that ``break``, and every
``ctx.pause`` here is immediately followed by the return that carries it, so a
refusal can never fall through into the turn it refused.

Seven invariants are load-bearing:

* **A review turn reads the scope it will be judged against.** The contract
  refresh, the scope re-derivation and the repository-base refresh all happen
  before the turn, and any one of them moving forces a readiness refresh and a
  fresh plan inspection — so the loop selects against the file it just
  rewrote rather than the image it read before.
* **The review base is captured once.** The workspace snapshot taken to
  compute the session's per-repository bases is the same object the turn later
  uses as its "before" image, so the review guard measures against exactly the
  base the session file names. A base that cannot be captured, or that is
  unavailable for any required repository, is a refusal rather than a
  narrower review.
* **A repair child's scope is widened before anything reads it.** The durable
  child file is the single description of what the child may write, so it is
  regenerated ahead of commit preflight, the task packet, the persisted prompt
  and the guard — all four read one scope instead of two.
* **An ineligible child never spends a turn.** The continuation is the only
  view that reconciles every pending child with the artifact it came from, and
  a child it refuses is not launched under the scope that refusal describes.
* **A continuation is one thing or the other.** A reply and a resolution are
  mutually exclusive; both pending at once is a programming error here, not a
  silent preference for one of them.
* **The intent gate judges the prompt, not the plan.** The repository-intent
  report is re-derived from the rendered prompt the worker will receive, and
  only for a run with no registered workspace — a registered run states its
  repositories in the registry instead.
* **An explicit plan-store write root must be covered before launch.** The
  selected completion contract's plan-store ``--state-dir`` requirement is
  checked for legacy and registered runs, including reply and resolution
  continuations. A missing root pauses without launching or changing state
  configuration; historical handoffs never declare that requirement.
"""

from __future__ import annotations

import argparse as argparse
import dataclasses as dataclasses
import json as json
from collections.abc import Sequence as Sequence
from pathlib import Path as Path
from typing import Any as Any
from typing import Protocol as Protocol

from . import conversation_resume as conversation_resume
from . import events as events
from . import handoff_guards as handoff_guards
from . import mailbox_models as mailbox_models
from . import plans_errors as plans_errors
from . import plans_models as plans_models
from . import plans_payloads as plans_payloads
from . import plans_progress as plans_progress
from . import plans_session_scope as plans_session_scope
from . import plans_snapshot as plans_snapshot
from . import preflight_footprint as preflight_footprint
from . import readiness_gate as readiness_gate
from . import redirect_ack as redirect_ack
from . import repository_evidence as repository_evidence
from . import review_delta as review_delta
from . import review_guard as review_guard
from . import review_repair as review_repair
from . import review_session_body as review_session_body
from . import review_session_contract as review_session_contract
from . import review_transitions as review_transitions
from . import run_inputs as run_inputs
from . import run_persistence as run_persistence
from . import run_prompts as run_prompts
from . import run_selection as run_selection
from . import state as state
from . import task_packet as task_packet
from . import turn_execution_argv as turn_execution_argv
from . import verification_models as verification_models
from . import workspace_evidence as workspace_evidence
from . import workspaces as workspaces
from . import write_guards as write_guards
from .turn_preparation_models import PacketInputs as PacketInputs
from .turn_preparation_models import RepairResumption as RepairResumption
from .turn_preparation_models import ReviewPreparation as ReviewPreparation
from .turn_preparation_models import RunContext as RunContext
from .turn_preparation_models import SessionClassification as SessionClassification
from .turn_preparation_models import TurnPrompt as TurnPrompt
from .turn_preparation_prompt import collect_packet_inputs as collect_packet_inputs
from .turn_preparation_prompt import prepare_turn_prompt as prepare_turn_prompt
from .turn_preparation_repair import (
    prepare_turn_continuation as prepare_turn_continuation,
)
from .turn_preparation_repair import resume_repair_session as resume_repair_session
from .turn_preparation_review import classify_session as classify_session
from .turn_preparation_review import prepare_review_session as prepare_review_session
