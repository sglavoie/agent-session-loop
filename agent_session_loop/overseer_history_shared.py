# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: F401
"""The ASL side of AgentMux's authorized transcript-evidence contract.

``overseer_policy_client`` asks whether the overseer may act. This module is
what acting on a ``context`` grant looks like: it runs
``amx overseer history --json``, reads the schema-v1 envelope, and turns the
answer into a :class:`History` the overseer can quote from. Like its sibling it
holds no state between calls, keeps nothing on disk, and has no configuration of
its own -- everything it knows comes back over stdout from the process it just
started.

## Why this is a separate question and not a bigger answer

Reading a conversation is ``context`` access, and the access category is not a
parameter here: it is fixed at :data:`~overseer_policy_client.ACCESS_CONTEXT`
and verified in the reply. A client that could ask for evidence under an
``action`` grant would be a client that could launder one permission into
another, and the request digest binds the category precisely so that cannot
happen quietly.

AgentMux authorizes twice on its side -- once for the plan before it discovers
anything, and once per conversation against the working directory that
conversation's identity metadata names -- and it does both before a message body
is opened. This client cannot verify that ordering from the outside. What it can
verify is that the answer it holds is an answer to the question it asked, and
that the answer respects the bounds it was asked under. Both are checked below,
because a caller that accepts whatever came back is a caller that can be handed
somebody else's transcripts.

## The bounds are part of the question

An answer taken under a 10-message bound is not evidence for a request that
asked for 200, and AgentMux binds the bounds into the request digest for that
reason. This client re-derives the same conclusion locally: an answer carrying
more conversations, more messages, or longer excerpts than were asked for is
:data:`FAILURE_ANSWER_MISMATCH`. That is not defensive duplication of AgentMux's
clamping. It is the difference between "the bounds were applied" and "the bounds
were reported", and only the first is a property of the evidence.

## Refusals must not describe what they withheld

A withheld conversation comes back as a harness, a session id, and a reason.
The path, the working directory, and the project are exactly what an exclusion
is about, so an answer that named them under the heading of explaining a refusal
would be handing back the withheld thing. This client refuses such an answer
outright rather than reading past the extra fields: a contract that leaks on one
side is not one this side should normalize.

## Unavailable is never permitted, and never cached

Every way the question can fail to be answered produces a :class:`History` with
``allowed`` false, ``available`` false, and an empty conversation tuple, under a
reason from :data:`FAILURE_REASONS`. There is no fallback and no cache, so a
failure cannot be quietly served from evidence gathered under configuration that
has since changed. ``available`` and ``allowed`` stay separate for the same
reason they do in the policy client: "AgentMux said no" is an operator decision
to respect and "AgentMux could not be asked" is a fault to report.

Durable records this boundary writes: **none**. It starts one subprocess and
returns what it said.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from . import overseer_policy_client
