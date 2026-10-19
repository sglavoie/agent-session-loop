# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
"""One path for every run control and every correction, whoever asked.

The browser and the AgentMux bridge had each grown a full copy of this. Two
``_redirect_report``\\ s, two ``_recorded_redirect``\\ s, two ``_redirect_receipt``\\ s,
two ceilings named ``MAX_REDIRECT_OPERATIONS``, two readings of what an abort is
about to mean, and two orderings of replay against authorization. They agreed
today because someone kept them agreeing; the seventh boundary added to the
redirect journal would have had to be taught to both.

This module is that one path. Both surfaces call the functions here and then
render, so what differs between them is the *shape of the answer* rather than
the answer:

* **The same reads.** One journal read per request, one live-target read, one
  ``inspect_run``. A client that reconnects to a restarted server and a client
  that reconnects to a different bridge process learn the same thing about the
  same correction, because both learn it from the journal rather than from
  anything either process was holding.
* **The same order.** Replay is judged out of the durable journal *before* the
  freshness proof is checked, in both surfaces, because that ordering is the
  only reason a correction request may be retried at all: a correction that
  landed moves the plan, which moves the action token, which would make the
  retry of a lost response look like a stale second correction.
* **The same refusals, differently spelled.** A refusal carries why it happened
  and what a client should do about it; turning that into a status code or into
  a versioned protocol error stays with the surface, because those two
  vocabularies are promises to different clients and neither may drift into the
  other.

What is deliberately *not* unified is how hard a surface checks before it acts.
:data:`~asl_workbench.controls.AUTHORIZATION_OPEN` queues a control for a plan
whose runner has not started yet, which is what a local operator with no
snapshot has always been able to do; ``AUTHORIZATION_STRICT`` refuses anything
that is not about the run the caller was shown. Both are correct for their
caller, so the difference is a parameter rather than two implementations.

Nothing here writes a payload. Functions return ASL's own objects -- ``RunFacts``,
``RecoveryReport``, ``OperationOutcome`` -- and each surface bounds and names the
fields its clients already expect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asl_workbench import controls as portable_controls

from . import (
    control_cli,
    control_queue,
    harnesses,
    intervention,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store_models,
    redirect_store_validation,
    redirect_store_vocab,
)

#: The operator-facing sentence each abort note code stands for. The codes are
#: portable and these words are not: they are ASL's, and the CLI already prints
#: them, so an operator who aborts from a browser and an operator who aborts
#: from a terminal are told the same thing in the same words.
