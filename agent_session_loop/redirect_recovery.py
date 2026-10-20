"""What an interrupted redirect means once the process holding it is gone.

Every earlier module in this subsystem assumes a live runner. Submission waits
on one, acceptance is a statement by one, escalation is a deadline inside one,
and delivery is a prompt one builds. A crash removes that process at an
arbitrary instant, and the instant it removes it decides what the operator is
owed. Seven boundaries matter, and the journal distinguishes all seven because
nothing in it is ever rewritten:

- before acceptance: a ``submitted`` line and nothing after it. Nobody promised
  the operator anything except that the correction was written down;
- after acceptance, before the prompt: ``accepted`` with no ``continued``. A
  process undertook to stop a worker and hand this correction to its successor,
  and then stopped existing. Nobody else has taken that on;
- after the prompt, before the launch window: ``continued`` with no
  ``launching``. The words were bound into a prompt, and then the process that
  was going to run it died or refused *before it could possibly have sent
  them*. No worker has ever seen them, so this reads like the line above it:
  still owed, and :func:`reconcile_startup` releases the carry so the next
  attempt can pick it up. This boundary exists only from journal version 3
  onward -- version 1 wrote the carry after the launch and version 2 had no
  ``launching`` line, so the same shape on either is the boundary below;
- inside the launch window: ``launching`` with no ``delivered`` and no
  ``released``. The attempt said the prompt was about to leave the process and
  then said nothing more. It may have sent the words and died, or died first,
  and no reading of the file can separate the two. Reported as
  :data:`STATUS_UNCERTAIN` and never released;
- after launch, before receipt: ``delivered`` with no ``acknowledged``. A worker
  was started holding the words. Whether a model read them is exactly the
  question the acknowledgment protocol exists because nothing cheaper answers;
- after receipt: ``acknowledged``. Settled in the only sense that counts;
- the torn tail: a line that was being written when the machine stopped. Replay
  refuses it and the next append terminates it, but a projection built in
  between has to say that one record may be missing rather than presenting a
  short history as a whole one.

Two rules shape everything here.

**Reading never starts anything.** :func:`inspect_plan_recovery` opens the
journal and the ledger read-only, classifies the lock without reclaiming it, and
touches no FIFO, no process, and no plan file. An operator asking what happened
to a correction must not be the reason a worker starts, and a status command
that spawned one would make "check before acting" the dangerous option.

**Ambiguity is reported, never resolved by guessing.** Two cases this module
refuses to decide: a delivery without receipt, and an open launch window
without either. Re-handing the correction to a new worker would tell it the
operator's first correction is new; dropping it would discard a correction that
may never have been read. Both are worse than saying so, so those ids come back
as :data:`STATUS_UNCERTAIN` and nothing here acts on them -- not by default and
not under a flag. The operator who looks and decides it was never read says so
by submitting the correction again, in words chosen knowing what happened.

The second case is the one a *failed* write produces as readily as a crash. An
attempt that cannot record what became of its carry has not thereby proved the
carry went nowhere, and the run loop is required to abandon the prompt rather
than send one it could not declare -- but a write that fails after the prompt is
already out leaves this shape, and it has to read as a question rather than as
an invitation to send the words again.

The mutating half, :func:`reconcile_startup`, runs under the new run's own lock
and follows the shape ``state.migrate_plan_state`` established for relocation:
no mutation until the lock evidence agrees. A plan another live process owns is
refused outright rather than reconciled optimistically, because a second
reconciler settling requests the first is still answering is how a correction
gets two contradictory outcomes.
"""

from __future__ import annotations

from .redirect_recovery_evidence import (
    BLOCKED_FOREIGN_OWNER as BLOCKED_FOREIGN_OWNER,
)
from .redirect_recovery_evidence import (
    BLOCKED_JOURNAL_UNREADABLE as BLOCKED_JOURNAL_UNREADABLE,
)
from .redirect_recovery_evidence import (
    BLOCKED_LIVE_OWNER as BLOCKED_LIVE_OWNER,
)
from .redirect_recovery_evidence import (
    OPERATION_STATUSES as OPERATION_STATUSES,
)
from .redirect_recovery_evidence import (
    OWNER_GONE as OWNER_GONE,
)
from .redirect_recovery_evidence import (
    OWNER_LIVE as OWNER_LIVE,
)
from .redirect_recovery_evidence import (
    OWNER_UNKNOWN as OWNER_UNKNOWN,
)
from .redirect_recovery_evidence import (
    STATUS_ACCEPTED as STATUS_ACCEPTED,
)
from .redirect_recovery_evidence import (
    STATUS_ACKNOWLEDGED as STATUS_ACKNOWLEDGED,
)
from .redirect_recovery_evidence import (
    STATUS_DELIVERED as STATUS_DELIVERED,
)
from .redirect_recovery_evidence import (
    STATUS_EXPIRED as STATUS_EXPIRED,
)
from .redirect_recovery_evidence import (
    STATUS_REJECTED as STATUS_REJECTED,
)
from .redirect_recovery_evidence import (
    STATUS_SUBMITTED as STATUS_SUBMITTED,
)
from .redirect_recovery_evidence import (
    STATUS_UNANSWERED as STATUS_UNANSWERED,
)
from .redirect_recovery_evidence import (
    STATUS_UNCERTAIN as STATUS_UNCERTAIN,
)
from .redirect_recovery_evidence import (
    UNFINISHED_STATUSES as UNFINISHED_STATUSES,
)
from .redirect_recovery_evidence import (
    ConversationEvidence as ConversationEvidence,
)
from .redirect_recovery_evidence import (
    OperationStatus as OperationStatus,
)
from .redirect_recovery_evidence import (
    OwnerEvidence as OwnerEvidence,
)
from .redirect_recovery_evidence import (
    OwnerLiveness as OwnerLiveness,
)
from .redirect_recovery_evidence import (
    inspect_conversations as inspect_conversations,
)
from .redirect_recovery_evidence import (
    inspect_owner as inspect_owner,
)
from .redirect_recovery_reconcile import (
    ReconciliationReport as ReconciliationReport,
)
from .redirect_recovery_reconcile import (
    StrandedCorrection as StrandedCorrection,
)
from .redirect_recovery_reconcile import (
    reconcile_startup as reconcile_startup,
)
from .redirect_recovery_reconcile import (
    recoverable_operation_ids as recoverable_operation_ids,
)
from .redirect_recovery_report import (
    OperationOutcome as OperationOutcome,
)
from .redirect_recovery_report import (
    RecoveryReport as RecoveryReport,
)
from .redirect_recovery_report import (
    inspect_plan_recovery as inspect_plan_recovery,
)
from .redirect_recovery_report import (
    operation_status as operation_status,
)
