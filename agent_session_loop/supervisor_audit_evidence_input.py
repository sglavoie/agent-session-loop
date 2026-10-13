"""Collect the bounded operational evidence a supervisor audit reads.

That bundle is control-plane only: identities, paths, digests, exit codes,
timings, and guard results, never application source or semantic diffs. It is
a pure function of the run's persisted history, so two equivalent histories
serialize to the same bytes, and anything the size cap forces out is reported
as a count, a digest, and a reason rather than silently dropped.

This is the lowest of the supervisor audit entry points: the checks, the agent
audit and the :mod:`.supervisor_audit` entry module all read what it collects.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import assurance_models_vocab, events, mailbox_constants, review
from .supervisor_audit_evidence_collector import _Collector, _projection
from .supervisor_audit_evidence_commits import _commit_section
from .supervisor_audit_evidence_constants import (
    _REPAIR_STATE_KEYS,
    _REVIEW_STATE_KEYS,
    AUDIT_INPUT_MAX_BYTES,
    AUDIT_INPUT_SCHEMA_VERSION,
    REASON_MALFORMED,
)
from .supervisor_audit_evidence_helpers import _event_counts, _read_json_file
from .supervisor_audit_evidence_models import AuditInput, EvidenceRoots
from .supervisor_audit_evidence_sections import (
    _plan_section,
    _registry_section,
    _repository_section,
    _scopes_section,
    _versions_section,
)
from .supervisor_audit_evidence_shedding import _shed
from .supervisor_audit_evidence_status import _status_section
from .supervisor_audit_evidence_streams import (
    _delivered_mailbox_ids,
    _read_events,
    _read_mailbox,
)
from .supervisor_audit_evidence_turns import (
    _mark_rejected_turns,
    _mark_unredeemed_deferral_turns,
    _stamp_supersessions,
    _turn_directories,
    _turn_section,
    _verification_section,
)


def collect_audit_input(
    *,
    run_id: str,
    run_dir: Path | str,
    state_dir: Path | str,
    plan_dir: Path | str,
    workspace_root: Path | str | None = None,
    state_payload: Mapping[str, Any] | None = None,
    workspace_snapshot: Mapping[str, Any] | None = None,
    terminal_pending: bool = False,
    max_bytes: int = AUDIT_INPUT_MAX_BYTES,
) -> AuditInput:
    """Collect the bounded operational evidence bundle for one finished run.

    The bundle is a pure function of the run's persisted history and of the
    trees ASL itself ships, so two equivalent histories serialize to identical
    bytes — which is what lets the artifact's digest key the idempotent
    publication in :mod:`agent_session_loop.assurance_projection`. Nothing is
    collected by wall clock, and nothing is collected from the application
    under test: paths, digests, exit codes, timings, and guard results carry
    the control-plane story, while file contents and semantic diffs stay out.

    Set ``terminal_pending`` when the caller audits a run whose terminal record
    has not been written yet, so the checks that read for a missing terminal
    record do not report the caller's own ordering as a defect.
    """
    run_dir = Path(run_dir)
    state_dir = Path(state_dir)
    plan_dir = Path(plan_dir)
    roots = EvidenceRoots.build(
        run_dir=run_dir,
        state_dir=state_dir,
        plan_dir=plan_dir,
        workspace_root=workspace_root,
    )
    collector = _Collector(roots)
    # Registered first so it survives the evidence-index cap. A deterministic
    # check may not cite nothing, so this is the locator every check falls back
    # to when the artifact it read was shed or was never written.
    collector.reference(run_dir, kind="artifact", detail="run directory")

    state_path = state_dir / events.STATE_FILE_NAME
    if state_payload is None:
        loaded, reason = _read_json_file(state_path)
        if isinstance(loaded, Mapping):
            state_payload = loaded
        else:
            collector.omissions.record(
                "state", reason or REASON_MALFORMED, str(state_path)
            )
            state_payload = {}
    state_reference = collector.reference(state_path, kind="state")

    registry = _registry_section(collector, state_payload)
    aliases = [
        str(entry.get("alias"))
        for entry in (registry.get("repositories") or ())
        if isinstance(entry, Mapping) and entry.get("alias")
    ]
    event_records, event_summary = _read_events(
        state_dir / events.EVENTS_FILE_NAME, run_id, collector
    )
    mailbox_records, mailbox_summary = _read_mailbox(
        state_dir / mailbox_constants.MAILBOX_FILE_NAME,
        run_id,
        collector,
        delivered_ids=_delivered_mailbox_ids(event_records, run_id),
    )
    turns = [
        _turn_section(collector, directory) for directory in _turn_directories(run_dir)
    ]
    _mark_rejected_turns(turns, event_records)
    _mark_unredeemed_deferral_turns(
        turns, state_payload, event_records, run_id=str(run_id)
    )

    payload: dict[str, Any] = {
        "schema_version": AUDIT_INPUT_SCHEMA_VERSION,
        "run_id": run_id,
        "roots": roots.to_dict(),
        "plan": _plan_section(
            collector, plan_dir=plan_dir, state_payload=state_payload
        ),
        "registry": registry,
        "scopes": _scopes_section(collector, plan_dir=plan_dir, aliases=aliases),
        "configuration": collector.redact(state_payload.get("run_config") or {}),
        "state": {
            "reference": state_reference,
            "harness": state_payload.get("harness"),
            # Requested/effective semantics are explicit for the audit.  The
            # historical ``model`` aliases remain for consumers of older
            # bundles, while ``model_projection`` is authoritative per role.
            "requested_model": collector.redact(
                state_payload.get("model"), key="requested_model"
            ),
            "requested_model_source": state_payload.get("model_source"),
            "model": collector.redact(state_payload.get("model"), key="model"),
            "model_source": state_payload.get("model_source"),
            "observed_model": collector.redact(
                state_payload.get("observed_model"), key="observed_model"
            ),
            "observed_model_source": state_payload.get("observed_model_source"),
            # Where the two are reconciled per role: an unresolved request that
            # every turn of a role answered the same way resolves here, and a
            # review harness keeps its own record.
            "model_projection": collector.redact(
                state_payload.get("model_projection") or [], key="model_projection"
            ),
            "effort": state_payload.get("effort"),
            "turn_count": len(turns),
        },
        "turns": turns,
        "events": {
            "records": event_records,
            "summary": event_summary,
            "counts": _event_counts(event_records),
            "terminal_pending": bool(terminal_pending),
        },
        "mailbox": {"records": mailbox_records, "summary": mailbox_summary},
        "verification": _verification_section(turns),
        "review": _projection(collector, state_payload, _REVIEW_STATE_KEYS),
        "repair": _projection(collector, state_payload, _REPAIR_STATE_KEYS),
        "commits": _commit_section(
            collector, run_dir=run_dir, state_payload=state_payload, turns=turns
        ),
        "status": _status_section(collector, state_payload, mailbox_records),
        "repository": _repository_section(collector, workspace_snapshot),
        "versions": _versions_section(),
    }
    # Digest rather than merely name each artifact: a locator alone cannot tell
    # the auditor whether the file it points at is the one the run produced.
    for name in assurance_models_vocab.CANONICAL_AUDIT_ARTIFACTS:
        candidate = run_dir / name
        if candidate.exists():
            collector.file_digest(candidate, kind="artifact")
    # The review cycle records these two paths and their digests, so an index
    # that never mentions them leaves the auditor unable to tell a review that
    # produced no artifact from one whose artifact was simply never indexed.
    # The absence is therefore recorded rather than skipped.
    for candidate in (
        review.final_review_output_path(plan_dir),
        review.final_review_artifact_path(plan_dir),
    ):
        collector.file_digest(candidate, kind="artifact", section="review.artifacts")

    payload["evidence"] = collector.evidence()
    payload["omissions"] = collector.omissions.to_list()
    _stamp_supersessions(turns)
    _shed(payload, collector, max_bytes=max_bytes)
    payload["evidence"] = collector.evidence()
    payload["omissions"] = collector.omissions.to_list()
    return AuditInput(payload=payload)


def tighten_audit_input(bundle: AuditInput, *, max_bytes: int) -> AuditInput:
    """Shed an already-collected bundle down to a smaller ceiling.

    Shed again rather than collected again: collecting reads the run directory,
    which by the time this is needed holds the audit's own input artifact and
    attempt records, so a second collection would describe a different run than
    the first one did — including a reference to its own predecessor. Shedding
    the payload already in hand keeps the result a strictly smaller view of the
    same evidence, given up in the same least-useful-first order, and leaves the
    canonical bundle on disk the one the deterministic verdicts were read from.

    The evidence index is not shed, so the locators an auditor may cite are the
    same either way.
    """
    payload = copy.deepcopy(bundle.payload)
    collector = _Collector(EvidenceRoots(roots=()))
    collector.omissions.seed(payload.get("omissions") or ())
    collector.adopt_previews(payload)
    _shed(payload, collector, max_bytes=max_bytes)
    payload["omissions"] = collector.omissions.to_list()
    return AuditInput(payload=payload)
