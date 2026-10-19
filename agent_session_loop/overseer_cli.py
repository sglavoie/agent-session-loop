"""Operator surface for the overseer helper: scan, watch, inspect, decide, act.

These six leaves are the whole seam between a host agent and the overseer
machinery. Each one is deterministic: it reads state, applies the rules the
guarded modules already encode, and prints what it found. None of them decides
anything. Judgement enters in exactly one place -- the claims a host writes for
``asl overseer decide`` -- and it enters as data the register can reject, not as
a branch in this module.

The split matters because the host is a language model. A model that can be
argued into an action is a model that must never be the thing holding the
guard, so the guards live in :mod:`overseer_dispatch` and the adapters behind
it, and this module's only route to a mutation is
:func:`overseer_dispatch.dispatch`. There is no second path: no adapter's
``execute`` or ``prepare`` is called here, and the adapters are built only to be
handed to the dispatcher.

The host loop these leaves compose is:

1. **observe** -- ``scan`` lists what discovery can see, ``watch`` enrols what
   AgentMux authorizes and holds a lease on it.
2. **authorize** -- every leaf that reads content or prepares a mutation asks
   AgentMux again, immediately before it acts. A grant is never cached across
   a leaf boundary; the CLI's process lifetime is the cache lifetime.
3. **collect context** -- ``inspect`` opens an incident and captures a bounded
   packet, and ``--expand`` widens one source at a time under a byte and record
   ceiling rather than handing over a run's whole state.
4. **reason** -- ``decide`` turns recorded human statements into a ruling. What
   a human already settled is reused; what no human has settled is refused.
5. **dispatch** -- ``act`` builds an :class:`overseer_dispatch.Action` from the
   token and scope ``inspect`` captured earlier and hands it to the guarded
   dispatcher. Capturing them earlier is the point: a token recomputed at
   dispatch time would agree with itself and ``stale_action`` would never fire.
6. **reconcile** -- ``reconcile`` settles rows a crashed dispatch left in doubt,
   which is the only way an in-doubt row ever becomes an answer.
7. **report** -- every leaf prints a JSON envelope under ``--json`` and exits
   non-zero on any refusal, so a shell ``&&`` chain stops on the first one.

Escalation is structural. :class:`Escalation` has no default, no timeout and no
deadline, so a caller cannot express "proceed if nobody answers in N seconds".
The only way past a genuine question is a human answering it.


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .overseer_cli_actions import (
    _adapters as _adapters,
)
from .overseer_cli_actions import (
    _payload_for as _payload_for,
)
from .overseer_cli_actions import (
    act_from_args as act_from_args,
)
from .overseer_cli_claims import (
    _claim_projects as _claim_projects,
)
from .overseer_cli_claims import (
    _claims_text as _claims_text,
)
from .overseer_cli_claims import (
    _grant_for as _grant_for,
)
from .overseer_cli_claims import (
    _independent_plans as _independent_plans,
)
from .overseer_cli_claims import (
    _raises as _raises,
)
from .overseer_cli_claims import (
    _resolve_candidate as _resolve_candidate,
)
from .overseer_cli_claims import (
    _scope_for as _scope_for,
)
from .overseer_cli_claims import (
    parse_claims as parse_claims,
)
from .overseer_cli_claims import (
    project_identities as project_identities,
)
from .overseer_cli_common import (
    DEFAULT_SEAMS as DEFAULT_SEAMS,
)
from .overseer_cli_common import (
    EXIT_OK as EXIT_OK,
)
from .overseer_cli_common import (
    EXIT_REFUSED as EXIT_REFUSED,
)
from .overseer_cli_common import (
    EXIT_USAGE as EXIT_USAGE,
)
from .overseer_cli_common import (
    OPERATIONS as OPERATIONS,
)
from .overseer_cli_common import (
    SOURCE_KINDS as SOURCE_KINDS,
)
from .overseer_cli_common import (
    Authorize as Authorize,
)
from .overseer_cli_common import (
    BuildInventory as BuildInventory,
)
from .overseer_cli_common import (
    Escalation as Escalation,
)
from .overseer_cli_common import (
    InspectRun as InspectRun,
)
from .overseer_cli_common import (
    OpenLedger as OpenLedger,
)
from .overseer_cli_common import (
    OverseerCliError as OverseerCliError,
)
from .overseer_cli_common import (
    ReadHistory as ReadHistory,
)
from .overseer_cli_common import (
    ReadMailbox as ReadMailbox,
)
from .overseer_cli_common import (
    Seams as Seams,
)
from .overseer_cli_common import (
    Sleep as Sleep,
)
from .overseer_cli_common import (
    UtcIsoNow as UtcIsoNow,
)
from .overseer_cli_common import (
    UtcNow as UtcNow,
)
from .overseer_cli_common import (
    _emit as _emit,
)
from .overseer_cli_common import (
    _failure as _failure,
)
from .overseer_cli_common import (
    _pause_window_seconds as _pause_window_seconds,
)
from .overseer_cli_common import (
    _scope_payload as _scope_payload,
)
from .overseer_cli_common import (
    _selection as _selection,
)
from .overseer_cli_common import (
    _state_dir as _state_dir,
)
from .overseer_cli_common import (
    canonical_plan as canonical_plan,
)
from .overseer_cli_common import (
    plan_dir_of as plan_dir_of,
)
from .overseer_cli_decide import (
    decide_from_args as decide_from_args,
)
from .overseer_cli_evidence import (
    UNUSABLE_READING as UNUSABLE_READING,
)
from .overseer_cli_evidence import (
    Evidence as Evidence,
)
from .overseer_cli_evidence import (
    _rule as _rule,
)
from .overseer_cli_evidence import (
    gather as gather,
)
from .overseer_cli_evidence import (
    unbound as unbound,
)
from .overseer_cli_observe import (
    inspect_from_args as inspect_from_args,
)
from .overseer_cli_observe import (
    scan_from_args as scan_from_args,
)
from .overseer_cli_observe import (
    watch_from_args as watch_from_args,
)
from .overseer_cli_payloads import (
    _candidate_payload as _candidate_payload,
)
from .overseer_cli_payloads import (
    _decision_payload as _decision_payload,
)
from .overseer_cli_payloads import (
    _dispatch_payload as _dispatch_payload,
)
from .overseer_cli_payloads import (
    _excerpt_payload as _excerpt_payload,
)
from .overseer_cli_payloads import (
    _history_payload as _history_payload,
)
from .overseer_cli_payloads import (
    _packet_payload as _packet_payload,
)
from .overseer_cli_payloads import (
    _receipt_payload as _receipt_payload,
)
from .overseer_cli_payloads import (
    _record_payload as _record_payload,
)
from .overseer_cli_payloads import (
    _ruling_payload as _ruling_payload,
)
from .overseer_cli_payloads import (
    _statement_payload as _statement_payload,
)
from .overseer_cli_payloads import (
    _sweep_payload as _sweep_payload,
)
from .overseer_cli_reconcile import (
    reconcile_from_args as reconcile_from_args,
)
