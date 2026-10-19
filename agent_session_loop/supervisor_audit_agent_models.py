"""Focused implementation extracted from :mod:`.supervisor_audit`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from . import (
    assurance_models_audit,
    assurance_models_summary,
    harness_failures,
    harnesses,
)
from . import supervisor_audit_agent as audit_agent
from . import supervisor_audit_evidence_redaction as audit_evidence_redaction
from .assurance_models_vocab import (
    AttemptOutcome,
)
from .supervisor_audit_config import (
    _DISTINCT_AVAILABILITY_REASONS,
    REASON_HARNESS_FAILED,
)


def _redacted_classification(
    result: harnesses.HarnessResult,
) -> harness_failures.FailureClassification:
    """Classify a failed launch, with credentials stripped from its evidence."""
    classification = harness_failures.classify_harness_failure(result)
    return replace(
        classification,
        evidence=audit_evidence_redaction._redact_secrets(classification.evidence),
    )


def _availability_reason(
    classification: harness_failures.FailureClassification,
) -> str:
    """Name the availability class a failed launch belongs in.

    Most failed launches are ``harness-failed``, which is the class for "the
    auditor was launched and the launch broke" and is read as an ASL defect
    worth investigating. A handful of classifications say something else
    entirely -- the account is out of quota -- and filing those as harness
    failures reported an outage nobody here caused as a defect, under a
    recommendation to re-run the audit that could not be followed until the
    quota reset (ASI-127). The class is taken from the classifier's own reason
    rather than re-matched here, so the audit and the session loop cannot
    disagree about what the provider said.
    """
    return _DISTINCT_AVAILABILITY_REASONS.get(
        classification.reason, REASON_HARNESS_FAILED
    )


def _harness_tail(text: str) -> str:
    """Return the redacted end of a failing harness stream.

    The end, not the head: a harness prints its banner first and its error last,
    so a head-truncated capture of a chatty run keeps everything except the one
    line explaining why it died.
    """
    if not text:
        return ""
    encoded = audit_evidence_redaction._redact_secrets(text).encode("utf-8")
    if len(encoded) <= MAX_HARNESS_TAIL_BYTES:
        return encoded.decode("utf-8", errors="ignore")
    return encoded[-MAX_HARNESS_TAIL_BYTES:].decode("utf-8", errors="ignore")


RETRYABLE_DELIVERY_OUTCOMES: frozenset[str] = frozenset(
    {
        "timeout",
        "transport-error",
    }
)

RETRYABLE_ANSWER_OUTCOMES: frozenset[str] = frozenset(
    {
        "malformed-output",
        "missing-lanes",
        "invalid-reference",
    }
)

RETRYABLE_ATTEMPT_OUTCOMES: frozenset[str] = (
    RETRYABLE_DELIVERY_OUTCOMES | RETRYABLE_ANSWER_OUTCOMES
)

REASON_IDENTITY_AMBIGUOUS = "identity-ambiguous"

IDENTITY_AMBIGUOUS_OUTCOME: AttemptOutcome = "invalid-reference"

ATTEMPT_PROMPT_FILE = "prompt.txt"

ATTEMPT_MESSAGE_FILE = "last-message.txt"

ATTEMPT_OUTCOME_FILE = "outcome.json"

ATTEMPT_STDOUT_FILE = "harness-stdout.txt"

ATTEMPT_STDERR_FILE = "harness-stderr.txt"

MAX_HARNESS_TAIL_BYTES = 16 * 1024


@dataclass(frozen=True)
class AgentAuditOutcome:
    """The audit an engine run produced, with the attempts that produced it."""

    audit: assurance_models_audit.SupervisorAudit
    attempts: tuple[assurance_models_summary.AuditAttempt, ...]
    agent_covered: bool
    #: The validated answer behind ``audit``, kept because the canonical audit
    #: carries no identity: allocation reads the decisions from here.
    agent: audit_agent.AgentAudit | None = None
    #: The catalog those decisions were made against, whose digest names the
    #: identity state they are valid for.
    catalog: audit_agent.AgentCatalog | None = None
    #: The issue each selecting finding named, keyed by fingerprint, in the form
    #: the publication revalidates. Empty for an audit that selected nothing.
    identity_selections: Mapping[str, str] = field(default_factory=dict)

    @property
    def catalog_digest(self) -> str | None:
        """The identity state this audit's decisions were reviewed against.

        ``None`` when no agent answer stands behind the audit: a digest is only
        worth revalidating beside a decision it could invalidate, and supplying
        one for an audit that decided nothing would ask the publisher to check
        an answer that does not exist.
        """
        if self.agent is None or self.catalog is None:
            return None
        return self.catalog.digest
