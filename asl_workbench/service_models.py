"""Remote service operation tables, lane protocols, and shared provider state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .http import (
    MODE_REMOTE,
    OP_ARCHIVE_DELETE,
    OP_ARCHIVE_RESTORE,
    OP_ARCHIVE_SETTINGS,
    OP_ARTIFACTS,
    OP_COMMAND_REDEEM,
    OP_COMMITS,
    OP_CONTROL_ABORT,
    OP_CONTROL_REDIRECT,
    OP_CONTROL_SELECT_HARNESS,
    OP_CONTROL_STOP_AFTER_CURRENT,
    OP_CONVERSATION_RECOVERY,
    OP_EVENTS,
    OP_FINAL_REVIEW,
    OP_INGEST,
    OP_LAST_MESSAGE,
    OP_LAUNCH,
    OP_LOGS,
    OP_PLAN_ARCHIVE,
    OP_PLANS_ARCHIVE,
    OP_PROGRESS,
    OP_QUESTION_REPLY,
    OP_QUESTIONS,
    OP_RECOVERY,
    OP_REDIRECT_STATUS,
    OP_REGISTRY,
    OP_REGISTRY_PIN,
    OP_REGISTRY_UNPIN,
    OP_REPLY,
    OP_RESULT,
    OP_STATE,
    OP_UNLOCK,
    OP_VERIFICATION,
)
from .operations import (
    METHOD_ABORT,
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_SETTINGS,
    METHOD_LAUNCH,
    METHOD_PLAN_ARCHIVE,
    METHOD_QUESTION_REPLY,
    METHOD_REDIRECT,
    METHOD_REPLY,
    METHOD_SELECT_HARNESS,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_UNLOCK,
)
from .remote_reads import (
    OUTCOME_ANSWERED,
    OUTCOME_CANCELLED,
    OUTCOME_OFFLINE,
    OUTCOME_REFUSED,
    OUTCOME_TIMEOUT,
    OUTCOME_UNAVAILABLE,
    READ_OUTCOMES,
    ReadAnswer,
)
from .store_workbench import WorkbenchStore

# ---------------------------------------------------------------------------
# What this mode can and cannot do
# ---------------------------------------------------------------------------

#: The routes that become a bounded read on the Mac, and the detail kind each
#: one asks for. Keyed by operation rather than by path so the mapping survives
#: a route being renamed, and valued with
#: :data:`~asl_workbench.details.DETAIL_KINDS` words so the request that goes
#: out is one the far end already understands.
DETAIL_OPERATIONS: Mapping[str, str] = {
    OP_ARTIFACTS: "artifacts",
    OP_STATE: "state",
    OP_EVENTS: "events",
    OP_LOGS: "logs",
    OP_LAST_MESSAGE: "last-message",
    OP_RESULT: "result",
    OP_VERIFICATION: "verification",
    OP_COMMITS: "commits",
    OP_PROGRESS: "progress",
    OP_FINAL_REVIEW: "final-review",
    OP_QUESTIONS: "questions",
    OP_RECOVERY: "recovery",
}

#: The mutations this mode will admit, and the method each submits. Every one
#: of them is in :data:`~asl_workbench.remote_actions.DELIVERABLE_METHODS`;
#: :func:`~asl_workbench.remote_actions.parse_submission` re-checks that on the
#: composed payload, so a method added here that the lane will not carry is
#: refused at submission rather than accepted and then dropped.
#:
#: Plan and archive actions use a stable scope key in the journal's required
#: ``run_key`` column. Their wire targets do not claim that a run exists.
MUTATION_METHODS: Mapping[str, str] = {
    OP_LAUNCH: METHOD_LAUNCH,
    OP_PLAN_ARCHIVE: METHOD_PLAN_ARCHIVE,
    OP_ARCHIVE_RESTORE: METHOD_ARCHIVE_RESTORE,
    OP_ARCHIVE_DELETE: METHOD_ARCHIVE_DELETE,
    OP_ARCHIVE_SETTINGS: METHOD_ARCHIVE_SETTINGS,
    OP_REPLY: METHOD_REPLY,
    OP_QUESTION_REPLY: METHOD_QUESTION_REPLY,
    OP_CONTROL_STOP_AFTER_CURRENT: METHOD_STOP_AFTER_CURRENT,
    OP_CONTROL_ABORT: METHOD_ABORT,
    OP_CONTROL_SELECT_HARNESS: METHOD_SELECT_HARNESS,
    OP_CONTROL_REDIRECT: METHOD_REDIRECT,
    OP_UNLOCK: METHOD_UNLOCK,
}

#: What a paired connector does rather than what an operator does. A set of
#: one, held as a table anyway: the provider's dispatch, its capability report
#: and the invariant that no route is left unserved and unexplained all read
#: this name, so an upload path added beside it cannot be served by two of the
#: three and forgotten by the last.
UPLOAD_OPERATIONS: frozenset[str] = frozenset({OP_INGEST})

#: The methods whose fingerprint is narrowed by something other than the plan.
#: Named per method rather than guessed from whichever selector happens to be
#: present: reusing an operation id against a different question is a conflict,
#: and it can only be recognised as one if both submissions narrow the same way.
#:
#: Only ``question_id`` appears, and the journal's columns are why. A
#: resubmission is compared against the row that was written, so a fingerprint
#: narrowed on a field the row does not keep could never be recomputed from it,
#: and every retry of such a method would read as a conflicting reuse. The
#: journal keeps ``question_id``; it keeps no harness and no session id. So
#: those methods narrow by plan, and reusing one id for two harness choices is
#: read as the retry it almost always is rather than as a refusal nobody can
#: act on.
_SELECTORS: Mapping[str, str] = {
    METHOD_REPLY: "question_id",
    METHOD_QUESTION_REPLY: "question_id",
}

#: Why each operation the Pi cannot carry out is one it cannot carry out.
#: Operator-facing, because ``GET /api/capabilities`` shows these to whoever is
#: looking at a greyed-out control.
UNAVAILABLE_REASONS: Mapping[str, str] = {
    OP_COMMAND_REDEEM: (
        "command bodies can be redeemed only while this service holds its "
        "volatile mutation lane"
    ),
    OP_REGISTRY: (
        "the registry is the Mac's own file and is not published over the link"
    ),
    OP_REGISTRY_PIN: (
        "pinning edits the Mac's registry, which this service never holds"
    ),
    OP_REGISTRY_UNPIN: (
        "unpinning edits the Mac's registry, which this service never holds"
    ),
    OP_PLANS_ARCHIVE: (
        "use cleanup review to archive selected plans with one confirmed "
        "command per plan"
    ),
    OP_REDIRECT_STATUS: (
        "a correction's progress is reported through the operation journal, "
        "not through the Mac's redirect route"
    ),
    OP_CONVERSATION_RECOVERY: (
        "recovering a conversation needs the harness session files, which stay "
        "on the machine that wrote them"
    ),
}

_DEFAULT_UNAVAILABLE = "the remote provider does not carry out this operation"

#: The status and reason each read outcome answers with. Four of the six are
#: failures and each keeps its own code, because "the link is down", "this
#: pairing may not read details", "the Mac took too long", and "the Mac said
#: no" call for four different things from whoever is reading.
READ_STATUSES: Mapping[str, tuple[int, str]] = {
    OUTCOME_ANSWERED: (200, "ok"),
    OUTCOME_REFUSED: (409, "detail-refused"),
    OUTCOME_OFFLINE: (503, "link-offline"),
    OUTCOME_UNAVAILABLE: (501, "detail-unavailable"),
    OUTCOME_TIMEOUT: (504, "detail-timeout"),
    OUTCOME_CANCELLED: (503, "detail-cancelled"),
}

_UNMAPPED_OUTCOMES = tuple(
    sorted(outcome for outcome in READ_OUTCOMES if outcome not in READ_STATUSES)
)

if _UNMAPPED_OUTCOMES:  # pragma: no cover - a registration mistake, not a state
    raise RuntimeError(
        "asl_workbench.service has no status for read outcomes: "
        + ", ".join(_UNMAPPED_OUTCOMES)
    )

# ---------------------------------------------------------------------------
# The two lanes this provider borrows
# ---------------------------------------------------------------------------


class DetailLane(Protocol):
    """Asking the Mac to read one artifact, and waiting for the answer.

    The waiting is the caller's, not this module's: a service loop holds the
    channel and knows when a frame arrived, and putting a wait in here would
    put a socket in a package that deliberately has none.
    """

    def fetch(
        self,
        *,
        detail: str,
        plan_key: str,
        run_key: str | None = None,
        question_id: str | None = None,
        artifact: str | None = None,
        stream: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        max_bytes: int | None = None,
        now: float,
    ) -> ReadAnswer:
        """The answer, material or not. Never raises for an ordinary refusal."""


class SubmissionLane(Protocol):
    """Handing one validated ``operation.submit`` down to the connector."""

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        """Queue the submission for the next acknowledgement frame."""


class MutationBodyLane(Protocol):
    """Hold reply prose in memory and redeem it for the paired connector."""

    def stage_body(
        self, operation_id: str, text: str, *, now: float
    ) -> tuple[str, str, bool]:
        """Return ``(reply_ref, token, created)`` for one submission."""

    def discard_body(self, operation_id: str) -> None:
        """Forget any in-memory prose held for ``operation_id``."""

    def redeem_body(
        self,
        operation_id: str,
        reply_ref: str,
        action_token: str,
        *,
        connector_id: str,
        now: float,
    ) -> str:
        """Exchange the command's capability for its operator-authored text."""


class UploadLane(Protocol):
    """Taking in one uploaded frame and saying what rides back down.

    The seam is here rather than in the route because what an upload *means*
    is the pairing's business: which connector this service is paired with,
    what its credential may speak for, and which frame is worth handing to the
    acknowledgement. :class:`~asl_workbench.uplink.Uplink` is the
    implementation; this is the shape the provider needs from it, and it is a
    protocol so a test can hand the provider a link it drives by hand.
    """

    def ingest(
        self, frame: Any, *, now: float, subject: str | None = None
    ) -> Mapping[str, Any] | None:
        """Apply the frame; return the envelope to carry down, or ``None``."""


_NO_READ_LANE = "no connector is paired, so no artifact can be read right now"

_NO_SUBMISSION_LANE = (
    "no connector is paired, so there is nothing to carry a mutation out"
)

#: Not "no connector is paired" -- the connector is the one asking. This
#: provider was built without an uplink at all, which is a deployment that can
#: serve what it already holds and can never be given anything new.
_NO_UPLOAD_LANE = "this service was not built to accept connector uploads"

_NO_BODY_LANE = "this service was not built to redeem volatile command bodies"


class ServiceState:
    """Shared retained store and connector lanes for remote route handlers."""

    mode = MODE_REMOTE

    def __init__(
        self,
        store: WorkbenchStore,
        *,
        clock: Callable[[], float],
        connector_id: str,
        reads: DetailLane | None = None,
        mutations: SubmissionLane | None = None,
        bodies: MutationBodyLane | None = None,
        uploads: UploadLane | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._connector_id = connector_id
        self._reads = reads
        self._mutations = mutations
        self._bodies = bodies
        self._uploads = uploads
