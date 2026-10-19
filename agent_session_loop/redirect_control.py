"""The urgent controller: one live turn's route from correction to dead worker.

:mod:`agent_session_loop.redirect_store_requests` makes a correction durable and
:mod:`agent_session_loop.redirect_admission` decides whether a live run may take
it. Neither of them stops anything. This module is what turns an accepted
correction into a worker that has actually stopped, inside a bound the operator
was promised, and it exists as its own module because every other route to that
outcome runs through something that can be blocked.

The turn's own wait loop is one such route, and it is the cooperative one: it
notices the armed :class:`~agent_session_loop.harnesses.AbortSignal`, names the
cancellation window, kills the group, sweeps the descendants, and produces the
``aborted`` result the run loop accounts for. All of that is worth having and
none of it is guaranteed. It runs only once the transport has been opened, so a
handshake wedged inside ``open_transport`` never reaches it; and it samples the
process table before it kills, on a five-second bound that is longer than this
whole escalation window. A correction whose delivery depends on a healthy
harness is a correction that fails exactly when it is needed.

So the escalation here is a separate, dedicated path with four properties:

**It is armed before acceptance is reported.** An operator is told "accepted"
by the journal record, and by then the deadline that makes acceptance mean
something is already running. :meth:`UrgentController.arm` is deliberately
cheap and revocable — a timer and a background sample, nothing anyone can
observe — so it can precede the append without committing to anything, and
:meth:`UrgentController.abandon` stands it down if the append is refused.
Revocable also means repeatable: a refusal costs the *request* its escalation,
never the turn its urgent channel, so an arming that was stood down can be
armed again for the next correction aimed at the same live worker.

**It initiates native cancellation immediately.** The moment acceptance is
durable, the harness is told to stop over its own protocol and the abort signal
is armed. Both are fire-and-forget: the protocol call goes on a daemon thread
nothing joins, because ``interrupt`` on a wedged transport is precisely the call
that can block forever.

**It force-stops at the deadline regardless.** The watchdog runs on its own
thread against a monotonic clock, takes no lock but its own leaf lock, and calls
:func:`~agent_session_loop.process_tree.force_stop_process_group`, which spawns
nothing. There is no ``ps``, no journal read, no transport call and no shared
lock between the deadline passing and the ``killpg``. The watchdog cannot queue
behind live guidance delivery, a protocol request, or a process-table probe,
because it never asks any of them for anything.

**It reports whether the worker actually stopped.** Killing is not stopping. A
descendant that left the process group survives a ``killpg``, and a signalled
process stays visible until its parent reaps it, so continuation is gated on
:class:`TerminationReport` rather than on the kill having been issued. Evidence
is collected while the worker is still alive — the only moment an escapee is
reachable by a descendant walk — and identity-checked cleanup and confirmation
both run after the kill, so neither is on the deadline path.

The gate is :meth:`UrgentController.require_successor_allowed`, and it refuses
for an uncollected predecessor as readily as for a live one, and for a report
that never arrived as readily as for one that says no. Cleanup and confirmation
run per pid against a real process table after the kill, so an escalation that
is merely slow, one that is wedged, and one whose thread died all present the
same way — as an acceptance with nothing published behind it — and none of them
is evidence that anything stopped. Two workers on one conversation, one of them
a process nobody is waiting on any more, is the failure this whole subsystem
would otherwise introduce.
"""

from __future__ import annotations

import threading as threading
import time as time
from collections.abc import Callable as Callable
from collections.abc import Mapping as Mapping
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from pathlib import Path as Path
from typing import Any as Any
from typing import Literal as Literal
from typing import Protocol as Protocol

from . import process_tree as process_tree
from . import redirect_admission as redirect_admission
from . import redirect_restart as redirect_restart
from . import redirect_store_models as redirect_store_models
from .redirect_control_controller import UrgentController as UrgentController
from .redirect_control_models import BLOCK_FAILED as BLOCK_FAILED
from .redirect_control_models import BLOCK_UNCOLLECTED as BLOCK_UNCOLLECTED
from .redirect_control_models import BLOCK_UNCONFIRMED as BLOCK_UNCONFIRMED
from .redirect_control_models import BLOCK_UNREPORTED as BLOCK_UNREPORTED
from .redirect_control_models import COLLECTION_WAIT_SECONDS as COLLECTION_WAIT_SECONDS
from .redirect_control_models import DECISION_GRACE_SECONDS as DECISION_GRACE_SECONDS
from .redirect_control_models import (
    ESCALATION_WINDOW_SECONDS as ESCALATION_WINDOW_SECONDS,
)
from .redirect_control_models import EVIDENCE_WAIT_SECONDS as EVIDENCE_WAIT_SECONDS
from .redirect_control_models import PHASE_ABANDONED as PHASE_ABANDONED
from .redirect_control_models import PHASE_ARMED as PHASE_ARMED
from .redirect_control_models import PHASE_CONFIRMED as PHASE_CONFIRMED
from .redirect_control_models import PHASE_ESCALATED as PHASE_ESCALATED
from .redirect_control_models import PHASE_IDLE as PHASE_IDLE
from .redirect_control_models import PHASE_RELEASED as PHASE_RELEASED
from .redirect_control_models import REASON_REDIRECT as REASON_REDIRECT
from .redirect_control_models import SKIP_ABANDONED as SKIP_ABANDONED
from .redirect_control_models import SKIP_DISARMED as SKIP_DISARMED
from .redirect_control_models import SKIP_TURN_ENDED as SKIP_TURN_ENDED
from .redirect_control_models import SKIP_UNDECIDED as SKIP_UNDECIDED
from .redirect_control_models import WATCHDOG_POLL_SECONDS as WATCHDOG_POLL_SECONDS
from .redirect_control_models import CancellableTurn as CancellableTurn
from .redirect_control_models import InterruptibleTransport as InterruptibleTransport
from .redirect_control_models import OwnedWorker as OwnedWorker
from .redirect_control_models import Phase as Phase
from .redirect_control_models import SuccessorBlockedError as SuccessorBlockedError
from .redirect_control_models import TerminationReport as TerminationReport
from .redirect_control_models import TerminationTools as TerminationTools
from .redirect_control_models import _evidence_summary as _evidence_summary
