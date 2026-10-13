"""Durable vocabulary for supervisor audits and the assurance inbox.

The supervisor audit judges ASL's own control plane after a persisted loop
ends. Its verdicts outlive the run that produced them: they are appended to an
immutable ledger, deduplicated across runs by fingerprint, and dispositioned by
a human weeks later. That makes the vocabulary itself the durable contract, so
it is defined in the ``assurance_models_*`` modules — before any storage,
orchestration, or agent invocation — and every other assurance module builds on
these models rather than on ad-hoc dictionaries.

Four rules shape the schema:

* **Fingerprints are deliberately narrow.** A finding's identity is its schema
  version, category, component, and stable invariant key — nothing else. Run
  IDs, timestamps, summaries, and evidence are all volatile, and folding any of
  them in would make the same defect look new on every run, which defeats the
  deduplication the inbox depends on.
* **All-clear is an assertion, not a default.** A terminal audit must carry
  lane-by-lane evidence for every rubric lane, and ``all-clear`` additionally
  requires every lane to have passed. An audit that simply failed to look at a
  lane cannot claim the lane is clean; it is ``unavailable`` instead.
* **Canonical artifacts are written exactly once.** :func:`write_new_bytes`
  uses ``O_CREAT | O_EXCL`` rather than the last-writer-wins
  :func:`agent_session_loop.artifacts.atomic_write_bytes`, because a second
  write to ``supervisor-audit.json`` means two audits disagreed about one run
  and silently overwriting one of them destroys the evidence of that conflict.
* **Everything is bounded and fails closed.** Text, collections, references,
  and serialized artifacts all have explicit caps, and every parser rejects
  unknown schema versions, unknown keys, duplicate lanes, malformed severity,
  traversal-capable references, and references to evidence the audit never
  declared.

Parsing mirrors :func:`agent_session_loop.contracts.parse_completion_contract`:
errors name the offending field and the rule it broke, so a malformed record is
quotable rather than merely rejected.

The ``assurance_models_*`` modules split that vocabulary by concern, in layers:
this module (vocabulary, artifact names, bounds), ``_primitives`` (validators and
identifier helpers), ``_findings``, ``_audit``, ``_inbox``, ``_summary``,
``_reconcile``, and ``_artifacts`` (exclusive-create writers).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

#: Bumped whenever the meaning of any model below changes. The version is part
#: of every finding fingerprint, so a schema change deliberately retires the
#: old identities instead of silently merging them with the new ones.
ASSURANCE_SCHEMA_VERSION = 1

#: Versions this build can read. Anything else is rejected before any side
#: effect, mirroring :func:`agent_session_loop.prepare_parsing.parse_validator_output`.
SUPPORTED_ASSURANCE_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

AuditState = Literal[
    "all-clear", "findings", "partial", "unavailable", "skipped", "pending"
]

#: Every state a run's assurance object can hold, in reporting order.
#: ``partial`` is the honest middle: the audit ran, found nothing wrong, and
#: could not judge every lane. Calling that ``all-clear`` overstates it and
#: calling it ``unavailable`` throws away the lanes that were judged.
AUDIT_STATES: tuple[AuditState, ...] = (
    "all-clear",
    "findings",
    "partial",
    "unavailable",
    "skipped",
    "pending",
)

#: States that assert a complete audit and therefore require full lane
#: coverage. ``partial`` and ``unavailable`` may carry partial deterministic
#: findings, ``skipped`` carries none, and ``pending`` has not run yet.
TERMINAL_AUDIT_STATES: frozenset[str] = frozenset({"all-clear", "findings"})

#: States the ledger files as a completed audit. ``partial`` belongs here and
#: not above: the audit ran to a verdict, so it completed, but it never claims
#: the full lane coverage the terminal states assert.
COMPLETED_AUDIT_STATES: frozenset[str] = TERMINAL_AUDIT_STATES | {"partial"}

RubricLane = Literal[
    "identity-authority",
    "task-packet-coherence",
    "lifecycle-retry-resume",
    "verification-review-independence",
    "mutation-commit-guards",
    "truthfulness-observability",
    "resources-stalls",
    "unknown-anomalies",
]

#: The rubric an audit must cover. The order is stable because it is rendered
#: to humans and compared in tests.
RUBRIC_LANES: tuple[RubricLane, ...] = (
    "identity-authority",
    "task-packet-coherence",
    "lifecycle-retry-resume",
    "verification-review-independence",
    "mutation-commit-guards",
    "truthfulness-observability",
    "resources-stalls",
    "unknown-anomalies",
)

CheckStatus = Literal["pass", "fail", "unavailable"]

#: A deterministic check either proved the invariant, disproved it, or could
#: not see enough to judge. Missing evidence is ``unavailable``, never ``pass``.
CHECK_STATUSES: tuple[CheckStatus, ...] = ("pass", "fail", "unavailable")

Severity = Literal["low", "medium", "high", "critical"]

#: Severities in ascending order of urgency.
SEVERITIES: tuple[Severity, ...] = ("low", "medium", "high", "critical")

IssueState = Literal["open", "resolved", "dismissed", "accepted-risk"]

#: Lifecycle states an inbox issue can hold.
ISSUE_STATES: tuple[IssueState, ...] = (
    "open",
    "resolved",
    "dismissed",
    "accepted-risk",
)

DispositionKind = Literal["resolve", "dismiss", "accept-risk"]

#: Human dispositions, and the issue state each one produces. Reopening is not
#: a disposition: it is a consequence of a recurrence and belongs to the
#: projection, not to a human command.
DISPOSITION_KINDS: tuple[DispositionKind, ...] = ("resolve", "dismiss", "accept-risk")
DISPOSITION_RESULT_STATE: Mapping[str, IssueState] = {
    "resolve": "resolved",
    "dismiss": "dismissed",
    "accept-risk": "accepted-risk",
}

CloseoutOutcome = Literal["accepted", "reworked", "escaped"]

#: How a human closed out a run's assurance verdict.
CLOSEOUT_OUTCOMES: tuple[CloseoutOutcome, ...] = ("accepted", "reworked", "escaped")

IdentityBindingSource = Literal["allocation", "merge", "reconcile"]

#: Why an identity key names the issue it names. ``allocation`` is the key an
#: issue was minted with, ``merge`` is a key whose history moved to another
#: issue, and ``reconcile`` is an explicitly reviewed operator decision.
IDENTITY_BINDING_SOURCES: tuple[IdentityBindingSource, ...] = (
    "allocation",
    "merge",
    "reconcile",
)

AttemptOutcome = Literal[
    "ok",
    "timeout",
    "transport-error",
    "malformed-output",
    "missing-lanes",
    "invalid-reference",
    "interrupted",
    "mutation-detected",
    "workspace-changed",
    "unavailable",
]

#: Why one agent attempt ended. Everything except ``ok`` is a reason to retry
#: once or to declare the audit unavailable.
ATTEMPT_OUTCOMES: tuple[AttemptOutcome, ...] = (
    "ok",
    "timeout",
    "transport-error",
    "malformed-output",
    "missing-lanes",
    "invalid-reference",
    "interrupted",
    "mutation-detected",
    "workspace-changed",
    "unavailable",
)

EvidenceKind = Literal[
    "artifact",
    "event",
    "state",
    "status",
    "plan",
    "task-packet",
    "prompt",
    "harness",
    "verification",
    "review",
    "commit",
    "repository",
]

#: What an evidence reference points at. Kinds are closed so a reader can route
#: a reference without guessing from its path shape.
EVIDENCE_KINDS: tuple[EvidenceKind, ...] = (
    "artifact",
    "event",
    "state",
    "status",
    "plan",
    "task-packet",
    "prompt",
    "harness",
    "verification",
    "review",
    "commit",
    "repository",
)

AuditRoleSource = Literal["explicit", "review"]

#: Whether the audit role came from an explicit ``--audit-harness`` or was
#: inherited from the effective review harness.
AUDIT_ROLE_SOURCES: tuple[AuditRoleSource, ...] = ("explicit", "review")


# ---------------------------------------------------------------------------
# Canonical artifact names
# ---------------------------------------------------------------------------

AUDIT_INPUT_FILENAME = "supervisor-audit-input.json"
AUDIT_REPORT_FILENAME = "supervisor-audit.json"
AUDIT_MARKDOWN_FILENAME = "supervisor-audit.md"
ATTEMPT_DIR_PREFIX = "supervisor-audit-attempt-"

#: Canonical per-run audit artifacts, in the order they are produced.
CANONICAL_AUDIT_ARTIFACTS: tuple[str, ...] = (
    AUDIT_INPUT_FILENAME,
    AUDIT_REPORT_FILENAME,
    AUDIT_MARKDOWN_FILENAME,
)

#: Budget for attempts the agent *answered*: one retry in a fresh invocation
#: after a malformed or incomplete answer, and no more. A bad answer is the
#: agent's own doing, so the budget stays tight.
MAX_AUDIT_ATTEMPTS = 2

#: Budget for attempts that never reached the agent at all — a timeout or a
#: transport/capacity failure. Delivery is the provider's doing, not the
#: agent's, so it gets its own, more forgiving budget: a provider outage
#: lasting minutes must not consume the answer retries or fail the audit.
MAX_AUDIT_DELIVERY_ATTEMPTS = 4

#: Budget for attempts the provider refused before the agent ran because the
#: request itself was too large. Neither the agent nor the provider is at fault
#: — the caller sent too much — so it is charged here rather than to either of
#: the budgets above, which would let one oversized bundle spend the retries a
#: real bad answer or a real outage is owed. One retry only: the shed order
#: gives up the least useful evidence first, so a second, smaller bundle would
#: start trading away the terminal evidence the audit exists to read.
MAX_AUDIT_SHRINK_ATTEMPTS = 1

#: Upper bound on recorded attempts: every budget can be spent in full within
#: one audit, and every attempt is recorded.
MAX_AUDIT_ATTEMPT_RECORDS = (
    MAX_AUDIT_ATTEMPTS + MAX_AUDIT_DELIVERY_ATTEMPTS + MAX_AUDIT_SHRINK_ATTEMPTS
)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

#: Identifier-shaped fields: categories, components, invariant keys, check
#: names, run IDs, harness models, and reason classes.
MAX_TOKEN_CHARS = 200

#: Free text a human reads: summaries, recommendations, reasons, notes.
MAX_TEXT_CHARS = 2000

#: An evidence locator is a run-relative path or pointer, never a payload.
MAX_LOCATOR_CHARS = 512

#: References attached to a single finding, lane, or check result.
MAX_REFERENCES_PER_ITEM = 32

#: The declared evidence index of one audit.
MAX_EVIDENCE_ENTRIES = 512

#: Findings and deterministic results in one audit.
MAX_FINDINGS = 200
MAX_DETERMINISTIC_RESULTS = 200

#: Issue IDs attached to one summary or closeout.
MAX_ISSUE_REFERENCES = 200

#: Serialized size of any canonical audit artifact. Session collectors cap
#: their own input well below this; the writer refuses anything larger so an
#: unbounded payload cannot reach the run directory.
MAX_ARTIFACT_BYTES = 1024 * 1024
