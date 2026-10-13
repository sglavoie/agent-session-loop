"""Size caps, reason tokens, roots and key tables for the audit evidence."""

from __future__ import annotations

from . import events, mailbox_vocabulary

#: Version of the ``supervisor-audit-input.json`` payload. Version 2 renamed the
#: events and mailbox summaries' ``rejected_count`` to
#: :data:`UNPARSABLE_LINE_COUNT_KEY`.
AUDIT_INPUT_SCHEMA_VERSION = 2


#: How many lines of a JSONL log would not parse. It sat beside
#: ``counts.turn_rejected`` under the name ``rejected_count``, and the auditing
#: agent read the two as one number: a summary saying zero rejections next to a
#: ``turn_rejected`` record looked like a contradiction, and was filed as one 46
#: times over runs that had all behaved correctly. A field an auditor is asked
#: to reason about has to say what it counts.
UNPARSABLE_LINE_COUNT_KEY = "unparsable_line_count"


#: What the same count was called before schema version 2. Bundles written under
#: version 1 are still the corpus every replay proof is drawn from, so the
#: reader falls back to the old spelling rather than reading a damaged log as an
#: undamaged one.
LEGACY_UNPARSABLE_LINE_COUNT_KEY = "rejected_count"


#: Serialized cap on one audit input bundle, well under the
#: ``assurance_models_vocab.MAX_ARTIFACT_BYTES`` ceiling the writer enforces.
AUDIT_INPUT_MAX_BYTES = 512 * 1024


#: How much of a refused bundle the oversized-input retry keeps. A ratio rather
#: than a smaller fixed ceiling because the observed refusals were not at one
#: size — 357 KB was refused where 550 KB went through — so the provider's limit
#: is in tokens, which bytes only approximate. Halving whatever was actually
#: refused moves it decisively under that limit without asserting a byte figure
#: the evidence does not support, and lowering
#: :data:`AUDIT_INPUT_MAX_BYTES` for every run would pay that cost on the runs
#: that never had the problem.
AUDIT_INPUT_OVERFLOW_SHRINK_RATIO = 0.5


#: Per-side preview budget for a captured log or last message.
PREVIEW_HEAD_BYTES = 2 * 1024


PREVIEW_TAIL_BYTES = 2 * 1024


#: Smallest preview the size cap shrinks to before dropping the text entirely.
MIN_PREVIEW_BYTES = 128


#: Longest string kept verbatim inside a copied control-plane payload.
MAX_INLINE_STRING_BYTES = 512


#: Entries kept from any one copied list before the remainder is counted.
MAX_SEQUENCE_ITEMS = 100


#: Files hashed when fingerprinting the ASL package or the skill tree.
MAX_TREE_FILES = 2000


#: Directories a tree digest walks past: the interpreter writes them, nobody
#: authors them.
GENERATED_TREE_DIRECTORIES = frozenset({"__pycache__"})


#: Suffixes of compiled artifacts a tree digest ignores, so equivalent sources
#: hash alike.
GENERATED_TREE_SUFFIXES = frozenset({".pyc", ".pyo"})


#: Turns kept regardless of age, on top of every anomalous one.
RECENT_TURN_COUNT = 3


#: Stand-in for a value withheld because it looked like a credential.
REDACTED = "[redacted]"


#: Why something is absent from the bundle. Token-shaped so they can be read
#: back as reasons without further parsing.
REASON_SIZE_CAP = "size-cap"


REASON_MISSING = "missing"


REASON_UNREADABLE = "unreadable"


REASON_MALFORMED = "malformed"


REASON_OUTSIDE_APPROVED_ROOTS = "outside-approved-roots"


REASON_NOT_RECORDED = "not-recorded"


REASON_APPLICATION_CONTENT = "application-content-excluded"


#: Symbolic names for the roots every reference in the bundle resolves under.
EVIDENCE_ROOT_RUN = "run"


EVIDENCE_ROOT_STATE = "state"


EVIDENCE_ROOT_PLAN = "plan"


EVIDENCE_ROOT_WORKSPACE = "workspace"


EVIDENCE_ROOT_PACKAGE = "package"


EVIDENCE_ROOT_SKILLS = "skills"


#: Mailbox kinds the bundle gives up first under the size cap. They time-stamp
#: an exchange; the question, the ruling on it, and the notice never go.
_SHEDDABLE_MAILBOX_KINDS = frozenset(mailbox_vocabulary.LIFECYCLE_KINDS) | {
    mailbox_vocabulary.KIND_ACK
}


#: Fields through which a mailbox record joins an exchange it does not name a
#: run for: a reply and a resolution point at their question, a receipt at the
#: closure it acknowledges, and a wait record at the wait it opened.
_MAILBOX_JOIN_KEYS = ("to", "wait_id", "closure_id")


#: How a mailbox record the collector claimed by delivery is marked, as opposed
#: to one that named this run outright or joined an exchange that did.
MAILBOX_OWNERSHIP_DELIVERED = "delivered"


#: Events emitted per heartbeat or per delivery. They dominate a long run's log
#: and say the least about it, so they are the first thing the cap sheds.
_BULK_EVENT_NAMES = frozenset(
    {
        events.AGENT_MUX_DELIVERY_EVENT,
        events.PROCESS_SWEEP_EVENT,
        events.TURN_HEARTBEAT_EVENT,
        events.VERIFICATION_CHECK_STARTED_EVENT,
        events.VERIFICATION_HEARTBEAT_EVENT,
    }
)


#: Events that are themselves the anomaly, a guard result, or the run's
#: terminal record. They are shed last and only when nothing else is left.
_ANOMALY_EVENT_NAMES = frozenset(
    {
        events.ACTION_REQUIRED_EVENT,
        events.PREFLIGHT_AUTO_REPAIRED_EVENT,
        events.RESOLUTION_CONSUMED_EVENT,
        events.REVIEW_REPAIR_REOPENED_EVENT,
        events.REVIEW_VERDICT_RETRY_EVENT,
        events.RUN_CONTROL_EVENT,
        events.RUN_FINISHED_EVENT,
        events.RUN_PAUSED_EVENT,
        events.TURN_HANG_CLEARED_EVENT,
        events.TURN_HANG_SUSPECTED_EVENT,
        events.TURN_REJECTED_EVENT,
        events.VERIFICATION_CHECK_SKIPPED_EVENT,
        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
    }
)


#: Protected records the cap gives up only after every other protected one:
#: how the run ended and which turns the guards refused are the facts a
#: containment verdict cannot be reached without.
_LAST_SHED_EVENT_NAMES = frozenset(
    {
        events.RUN_FINISHED_EVENT,
        events.RUN_PAUSED_EVENT,
        events.TURN_REJECTED_EVENT,
    }
)


_RUN_STARTED_EVENT = events.RUN_STARTED_EVENT


_TERMINAL_EVENT_NAMES = frozenset({events.RUN_FINISHED_EVENT, events.RUN_PAUSED_EVENT})


#: State keys copied verbatim (after redaction) into each projection.
_REVIEW_STATE_KEYS = (
    "review_artifact_sha256",
    "review_completed",
    "review_cycles",
    "review_harness",
    "review_output_path",
    "review_output_sha256",
    "review_requested",
    "review_session_executed",
    "review_session_id",
    "review_skipped_reason",
    "review_verdict",
)


_REPAIR_STATE_KEYS = (
    "review_fix_disposition",
    "review_reopen",
    "review_resume",
)


#: Mirrors ``commit_settlement.PAUSED_WORK_COMMIT_KEY`` as a literal. Importing
#: that module would drag the commit ledger, the loop's reporting and turn
#: execution into every audit; the bundle reads state as data, so a literal is
#: what the projection needs anyway.
PAUSED_WORK_COMMIT_KEY = "paused_work_commit"


#: How a projected commit was learned. The runner's own commit records carry no
#: origin, so this marks exactly the entries the audit derived from the write
#: guard rather than from a commit the runner ran itself.
OBSERVED_COMMIT_ORIGIN = "observed-write-guard"


_STATUS_STATE_KEYS = (
    "action_required",
    # A run that superseded a live ask on its way into a runner-declared pause
    # wrote the displaced record here. Projecting it is what stops the bundle
    # from showing an `action_required` of null with nothing saying what stood
    # there.
    events.ACTION_REQUIRED_SUPERSEDED_KEY,
    "assurance",
    "command",
    "completed_session_ids",
    "completed_sessions",
    "control",
    "details",
    "dry_run",
    "duration_seconds",
    "ok",
    "pause_details",
    "pause_message",
    "pause_reason",
    "pause_policy",
    "paused",
    "readiness_gate",
    "readiness_gate_reopened",
    "readiness_state_active",
    "readiness_state_current",
    "readiness_state_final",
    "readiness_state_initial",
    "reason",
    "run_finished_at",
    "run_started_at",
    "version",
)


#: Where a retired ``status.action_required`` is kept. The status section is
#: otherwise a verbatim copy of the terminal state payload, so a retirement has
#: to name what overrode the copy and keep the record it replaced: the history
#: an auditor reads is not allowed to shrink because the current state moved on.
#: Shared with ``run_termination``, which writes the same block onto the payload
#: when a runner-declared pause supersedes a live ask, so the copy the audit
#: makes and the copy it might inherit are spelled one way.
_STATUS_ACTION_REQUIRED_SUPERSEDED = events.ACTION_REQUIRED_SUPERSEDED_KEY


#: What retired it: the run's mailbox holds the ruling that answered the ask,
#: and a receipt proving the ruling reached the run.
SUPERSEDED_BY_CONSUMED_RULING = "mailbox-ruling-consumed"


_SNAPSHOT_IDENTITY_KEYS = (
    "content_sha256",
    "counts",
    "next_pending",
    "next_pending_id",
    "path_hash",
    "plan_hash",
)
