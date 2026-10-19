"""Compatibility façade for the focused :mod:`.supervisor_audit` modules."""

from __future__ import annotations

# ruff: noqa: F401
import argparse
import contextlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    assurance_models_artifacts,
    assurance_models_audit,
    assurance_models_findings,
    assurance_models_primitives,
    assurance_models_summary,
    assurance_models_vocab,
    assurance_projection,
    assurance_report,
    events,
    harness_failures,
    harnesses,
    model_availability,
    repository_evidence,
    state,
    supervisor_audit_vocabulary,
)
from . import supervisor_audit_agent as audit_agent
from . import supervisor_audit_checks as audit_checks
from . import supervisor_audit_evidence_commits as audit_evidence_commits
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from . import supervisor_audit_evidence_input as audit_evidence_input
from . import supervisor_audit_evidence_models as audit_evidence_models
from . import supervisor_audit_evidence_redaction as audit_evidence_redaction
from .assurance_models_primitives import AssuranceError
from .assurance_models_summary import AssuranceSummary, AuditRole
from .assurance_models_vocab import (
    MAX_AUDIT_ATTEMPT_RECORDS,
    AttemptOutcome,
    AuditRoleSource,
    AuditState,
)
from .harnesses import HARNESS_NAMES
from .literals import as_literal
from .supervisor_audit_agent_models import (
    ATTEMPT_MESSAGE_FILE,
    ATTEMPT_OUTCOME_FILE,
    ATTEMPT_PROMPT_FILE,
    ATTEMPT_STDERR_FILE,
    ATTEMPT_STDOUT_FILE,
    IDENTITY_AMBIGUOUS_OUTCOME,
    MAX_HARNESS_TAIL_BYTES,
    REASON_IDENTITY_AMBIGUOUS,
    RETRYABLE_ANSWER_OUTCOMES,
    RETRYABLE_ATTEMPT_OUTCOMES,
    RETRYABLE_DELIVERY_OUTCOMES,
    AgentAuditOutcome,
    _availability_reason,
    _harness_tail,
    _redacted_classification,
)
from .supervisor_audit_agent_run import (
    _bounded_argv,
    _delivery_is_retryable,
    _reason_token,
    _record_capacity_outage,
    _refused_for_size,
    _write_attempt_artifacts,
    run_agent_audit,
)
from .supervisor_audit_config import (
    _DISTINCT_AVAILABILITY_REASONS,
    AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS,
    AUDIT_WORKDIR_NAME,
    DEFAULT_AUDIT_TIMEOUT_SECONDS,
    REASON_DRY_RUN,
    REASON_EVIDENCE_UNAVAILABLE,
    REASON_HARNESS_FAILED,
    REASON_INTERRUPTED,
    REASON_NO_INDEPENDENT_ROLE,
    REASON_OPTED_OUT,
    REASON_QUOTA_EXHAUSTED,
    REASON_TIMEOUT,
    REASON_WORKSPACE_CHANGED,
    REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
    UNAVAILABLE_ARTIFACT_NAME,
    AuditCancelled,
    AuditConfig,
    AuditPlan,
    attempt_timeouts,
    audit_read_roots,
    audit_workdir,
    build_audit_invocation,
    cancelled_summary,
    config_from_namespace,
    initial_summary,
    plan_audit,
    resolve_role,
)
from .supervisor_audit_restricted import (
    _MUTATION_CHANGE_KEYS,
    AuditAttemptResult,
    _evidence_unprobeable,
    _mutation_comparison,
    _observability_changed,
    _record_workspace_change,
    _registered_evidence_unavailable,
    _snapshot_omits_registered,
    _without_remote_tracking_refs,
    run_restricted_audit,
)
from .supervisor_audit_terminal import (
    _deterministic_only_audit,
    _publish_evidence_failure,
    audit_terminal_run,
    status_line,
)
