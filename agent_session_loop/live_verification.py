"""The two live records a run publishes when no turn is in flight.

A run that is neither launching a harness nor between sessions is not
necessarily idle. Two phases can hold the plan lock for many minutes with no
turn running at all: the post-turn `Verify` recipe, which may shell out to a
whole test suite, and the terminal work after the last turn, whose supervisor
audit is itself a harness turn with its own answer and delivery budgets. From
the outside both are indistinguishable from a hang unless something publishes
them. This module is that something. It owns the two thread-safe holders that
announce those phases while they run — :class:`_LiveVerification` and
:class:`_LiveFinalization` — so the loop is left deciding *when* a phase begins
and ends rather than reimplementing *how* a background thread stamps liveness
onto a record other threads are also writing.

Both are per-run mutable holders rather than process globals: one is
constructed per verification recipe, one per terminal phase, and each is handed
the collaborators it writes through instead of reaching for them. The durable
records they own are:

* ``state.json``'s ``active_verification`` key and the five verification events
  — started, check-started, check-finished, check-skipped, finished — plus the
  ``verification_heartbeat`` its background thread appends on a cadence;
* ``state.json``'s ``active_finalization`` key and the three finalization
  events — started, heartbeat, finished.

Every one of those writes goes through the :class:`_RunPersistence` handle the
caller supplied, under that handle's lock, so this module never opens a file of
its own. Verification additionally announces itself to the AgentMux projection
through ``run_persistence._notify_agent_mux_loop``, looked up on the module at
call time rather than bound at definition, so a patched notifier is still the
one reached; finalization publishes no notification at all, because its whole
audience is the state record an operator or a supervisor reads.

Five invariants survive the move and are load-bearing:

* **The terminal transition is claimed before anything that blocks.**
  :meth:`_LiveVerification.finish` sets ``_finished`` and clears the active
  checks under ``_lock`` *before* it stops and joins the heartbeat thread. A
  parallel recipe that was abandoned mid-flight leaves command checks running,
  and their late callbacks — including any that land while the thread is being
  joined — must not resurrect ``active_verification`` or append behind
  ``verification_finished``.
* **State and output take different locks.** ``_lock`` guards the live state
  and the event append; ``_progress_lock`` orders terminal output only. Keeping
  them distinct is what stops a slow stderr pipe from stalling a worker
  callback's state mutation under a parallel recipe. The persistence handle's
  lock is always the innermost of the three.
* **Notifications are ordered against the live record and taken outside every
  lock.** The verification-started notification is emitted before the heartbeat
  thread exists, so it always carries a lower sequence than any heartbeat, and
  a heartbeat notification is skipped once ``_stop`` is set so a late one
  cannot outrace the verification-finished notification.
* **A published phase is closed exactly once.**
  :meth:`_LiveFinalization.finish` returns immediately on a second call, which
  is what lets a cancellable terminal phase be closed by whichever of the audit
  and the abort path reaches it first without appending two
  ``finalization_finished`` records.
* **A parallel recipe always has something to display.** More than one check is
  live at a time, so the display slot is drawn from the active set: a
  background check that finishes hands the slot to another live check, and the
  check timer is cleared only when the last one retires. Without that a
  heartbeat mid-recipe would report no running check at all.
"""

from __future__ import annotations

from .live_verification_finalization import (
    FINALIZATION_HEARTBEAT_INTERVAL_SECONDS as FINALIZATION_HEARTBEAT_INTERVAL_SECONDS,
)
from .live_verification_finalization import (
    FINALIZATION_PHASE_ASSURANCE_AUDIT as FINALIZATION_PHASE_ASSURANCE_AUDIT,
)
from .live_verification_finalization import _LiveFinalization as _LiveFinalization
from .live_verification_run import _LiveVerification as _LiveVerification
from .live_verification_state import (
    VERIFICATION_HEARTBEAT_INTERVAL_SECONDS as VERIFICATION_HEARTBEAT_INTERVAL_SECONDS,
)
