"""Which conversation a runner turn runs on, decided before it starts.

:mod:`~agent_session_loop.conversations` owns the durable ledger: what a
conversation is bound to, how a record is opened, confirmed and retired, and
what a lookup is allowed to answer. It deliberately knows nothing about runs,
turns, or flags. This module is the other half — the runner-facing policy that
turns "here is a pending session and a harness" into one of exactly three
outcomes, and nothing else:

* **continue** the saved conversation, naming it to the adapter;
* **open** one, because this session has never had one;
* **stop**, because a conversation exists that cannot be continued and only an
  operator may decide to walk away from it.

There is no fourth outcome, and in particular no silent fresh start. That is
the whole reason the decision lives in one function
(:func:`plan_turn_continuation`) rather than in the branches of a run loop: a
policy spread over four call sites acquires a fallback the first time one of
them is patched in a hurry.

## The one-use authorization

:class:`FreshConversationAuthorization` is the operator's ``--fresh-conversation``
permission, and it is a *mutable object with a counter* rather than a boolean
on purpose. A boolean is true every time it is read; this is true once. The
first turn that needs it consumes it, and every later turn in the same run --
retry, next session, repair child -- reads an exhausted authorization and stops
like an unauthorized one would.

Three surfaces make it single-use, and all three are needed, because each
covers a replay the others do not:

* this counter, for later turns *within* one run;
* ``run_entry._RESUME_OVERLAY_EXCLUDED_DESTS``, for ``asl run --resume``, which
  rehydrates flags from persisted state;
* ``asl_autopilot._CHILD_RUN_ARG_SKIP_DESTS``, for the supervisor, which
  relaunches the same session on every recoverable pause.

``run_payloads._run_config_payload`` completes the containment by persisting no
key for the flag at all, so there is nothing on disk for a fourth surface to
find. Nothing here writes the authorization down: the value that carries it has
no serialization, exactly like :class:`conversations.ContinuationIntent`.

## Durable records this boundary writes

Two, both through :mod:`~agent_session_loop.conversations` and never by
touching the ledger file directly:

* ``conversation_opened`` -- written by the capture sink
  (:class:`ConversationCapture`) on the harness's own driver thread, *before*
  the turn submits any work. Raising there is the point: a conversation that
  cannot be recorded is one the turn must not run on.
* ``conversation_confirmed`` / ``conversation_retired`` -- written after the
  turn by :func:`record_turn_outcome` and :func:`retire_session_conversations`,
  which report failures rather than raising, because a turn that already ran
  must not be lost to a bookkeeping error.

Nothing here imports the runner or the run loop. The run state dir, the plan
snapshot, the resolved workspace, and the parsed flags all arrive as
parameters.
"""

from __future__ import annotations

from .conversation_resume_capture import (
    ConversationCapture as ConversationCapture,
)
from .conversation_resume_capture import (
    RestoreRefusal as RestoreRefusal,
)
from .conversation_resume_capture import (
    TurnOutcomeRecord as TurnOutcomeRecord,
)
from .conversation_resume_capture import (
    attempt_outcome as attempt_outcome,
)
from .conversation_resume_capture import (
    record_turn_outcome as record_turn_outcome,
)
from .conversation_resume_capture import (
    refuse_unrestorable_conversation as refuse_unrestorable_conversation,
)
from .conversation_resume_capture import (
    restore_refusal_message as restore_refusal_message,
)
from .conversation_resume_capture import (
    retire_session_conversations as retire_session_conversations,
)
from .conversation_resume_capture import (
    turn_submitted_work as turn_submitted_work,
)
from .conversation_resume_models import (
    _UNSUBMITTED_CANCEL_STAGES as _UNSUBMITTED_CANCEL_STAGES,
)
from .conversation_resume_models import (
    CONTINUATION_DISPOSITIONS as CONTINUATION_DISPOSITIONS,
)
from .conversation_resume_models import (
    CONTINUATION_FRESH as CONTINUATION_FRESH,
)
from .conversation_resume_models import (
    CONTINUATION_OPENED as CONTINUATION_OPENED,
)
from .conversation_resume_models import (
    CONTINUATION_RECOVERY_REQUIRED as CONTINUATION_RECOVERY_REQUIRED,
)
from .conversation_resume_models import (
    CONTINUATION_RESTORE_REFUSED as CONTINUATION_RESTORE_REFUSED,
)
from .conversation_resume_models import (
    CONTINUATION_RESUMED as CONTINUATION_RESUMED,
)
from .conversation_resume_models import (
    CONTINUATION_RETIRED as CONTINUATION_RETIRED,
)
from .conversation_resume_models import (
    CONTINUATION_UNAVAILABLE as CONTINUATION_UNAVAILABLE,
)
from .conversation_resume_models import (
    LEGACY_BINDING_VERSION as LEGACY_BINDING_VERSION,
)
from .conversation_resume_models import (
    PROCEEDING_SELECTION_STATES as PROCEEDING_SELECTION_STATES,
)
from .conversation_resume_models import (
    RECOVERY_PAUSE_REASON as RECOVERY_PAUSE_REASON,
)
from .conversation_resume_models import (
    RECOVERY_REQUIRED_SELECTION_STATES as RECOVERY_REQUIRED_SELECTION_STATES,
)
from .conversation_resume_models import (
    RESTORE_REFUSED_RETIREMENT_REASON as RESTORE_REFUSED_RETIREMENT_REASON,
)
from .conversation_resume_models import (
    FreshConversationAuthorization as FreshConversationAuthorization,
)
from .conversation_resume_models import (
    legacy_registry_digest as legacy_registry_digest,
)
from .conversation_resume_models import (
    turn_binding as turn_binding,
)
from .conversation_resume_models import (
    workspace_binding as workspace_binding,
)
from .conversation_resume_plan import (
    TurnContinuation as TurnContinuation,
)
from .conversation_resume_plan import (
    _harness_capability as _harness_capability,
)
from .conversation_resume_plan import (
    _supersede as _supersede,
)
from .conversation_resume_plan import (
    plan_turn_continuation as plan_turn_continuation,
)
from .conversation_resume_plan import (
    recovery_message as recovery_message,
)
