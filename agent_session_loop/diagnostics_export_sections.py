"""Focused portion of agent_session_loop/diagnostics_export.py."""

from .diagnostics_export_read import *


def _projection_section(
    state_obj: Mapping[str, Any], omissions: Omissions
) -> tuple[str, dict[str, Any] | None]:
    """Validate and copy one run's AgentMux projection.

    The projection is copied through the same field list ``Projection.to_dict``
    writes, so the export cannot drift into publishing a field the projection
    contract does not define — including a field a *newer* ASL wrote into a
    state file this build is reading.
    """
    raw = state_obj.get(agent_mux_projection.PROJECTION_KEY)
    if raw is None:
        omissions.record("projection", OMISSION_UNPROJECTABLE, "absent")
        return PROJECTION_ABSENT, None
    if not isinstance(raw, Mapping):
        omissions.record("projection", OMISSION_MALFORMED, "not-a-mapping")
        return PROJECTION_INVALID, None
    if raw.get("schema_version") != agent_mux_projection.PROJECTION_SCHEMA_VERSION:
        omissions.record("projection", OMISSION_MALFORMED, raw.get("schema_version"))
        return PROJECTION_INVALID, None
    contract = raw.get("contract_set_id")
    if contract != agent_mux_projection.CONTRACT_SET_ID:
        # Exported as absent rather than as a foreign body: this build cannot
        # vouch for the field meanings of a contract set it does not implement.
        omissions.record("projection", OMISSION_FOREIGN_CONTRACT, contract)
        return PROJECTION_FOREIGN, None

    fields = (
        "schema_version",
        "contract_set_id",
        "run_id",
        "plan_dir",
        "workspace_root",
        "registry_digest",
        "harness",
        "kitty_window_id",
        "phase",
        "session_id",
        "turn_index",
        "question_id",
        "lifecycle_source_sequence",
        "projection_generation",
        "started_at",
        "updated_at",
        "finished_at",
    )
    projection = _pick(raw, fields)
    process = raw.get("process")
    projection["process"] = (
        _pick(process, ("pid", "start_token")) if isinstance(process, Mapping) else None
    )
    return PROJECTION_PRESENT, projection


def _assurance_section(
    state_obj: Mapping[str, Any], omissions: Omissions
) -> dict[str, Any] | None:
    """Summarize the run's terminal assurance verdict without its findings.

    Issue *identifiers* travel because they join this run to the shared
    assurance ledger; issue bodies do not, because a finding quotes the code and
    the transcript it was found in.
    """
    raw = state_obj.get("assurance")
    if not isinstance(raw, Mapping):
        return None
    issue_ids = raw.get("issue_ids")
    issue_ids = list(issue_ids) if isinstance(issue_ids, (list, tuple)) else []
    exported = [value for value in issue_ids[:MAX_ISSUE_IDS] if isinstance(value, str)]
    if len(issue_ids) > MAX_ISSUE_IDS:
        omissions.record(
            "assurance",
            OMISSION_BOUNDED,
            ["issue_ids", MAX_ISSUE_IDS],
            count=len(issue_ids) - MAX_ISSUE_IDS,
        )
    attempts = raw.get("attempts")
    artifacts = raw.get("artifacts")
    omissions.record("assurance", OMISSION_EXCLUDED_DETAIL, "findings")
    return {
        "schema_version": _bounded_scalar(raw.get("schema_version")),
        "state": _bounded_str(raw.get("state")),
        "eligible": bool(raw.get("eligible")),
        "reason": _bounded_str(raw.get("reason")),
        "issue_ids": exported,
        "issue_count": len(issue_ids),
        "new_issue_count": _bounded_scalar(raw.get("new_issue_count")),
        "recurring_issue_count": _bounded_scalar(raw.get("recurring_issue_count")),
        "attempt_count": len(attempts) if isinstance(attempts, (list, tuple)) else 0,
        "artifact_count": len(artifacts) if isinstance(artifacts, (list, tuple)) else 0,
        "started_at": _bounded_str(raw.get("started_at")),
        "completed_at": _bounded_str(raw.get("completed_at")),
        "duration_seconds": _bounded_scalar(raw.get("duration_seconds")),
    }


def _events_sections(
    state_dir: Path, run_id: str | None, omissions: Omissions
) -> dict[str, Any]:
    """Derive the delivery, control and event sections from one event log."""
    records, summary = _read_jsonl(
        state_dir / events.EVENTS_FILE_NAME, "events", omissions
    )

    names: list[Any] = []
    delivery: list[dict[str, Any]] = []
    control: list[dict[str, Any]] = []
    for record in records:
        payload = record.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if run_id is not None and payload.get("run_id") not in (None, run_id):
            continue
        name = record.get("name")
        names.append(name)
        if name == events.AGENT_MUX_DELIVERY_EVENT:
            delivery.append(_pick(payload, DELIVERY_FIELDS))
        elif name == events.RUN_CONTROL_EVENT:
            entry = _pick(payload, CONTROL_FIELDS)
            if payload.get("message"):
                omissions.record("control", OMISSION_EXCLUDED_BODY, "message")
            control.append(entry)

    if delivery:
        omissions.record("delivery", OMISSION_EXCLUDED_DETAIL, "attempts")

    delivery_kept = _tail(delivery, MAX_DELIVERY_RECORDS, "delivery", omissions)
    control_kept = _tail(control, MAX_CONTROL_RECORDS, "control", omissions)

    operations = sorted(
        {
            str(entry["command_id"])
            for entry in control_kept
            if isinstance(entry.get("command_id"), str) and entry["command_id"]
        }
    )
    return {
        "events": {
            "available": summary["available"],
            "reason": summary["reason"],
            "scanned": summary["scanned"],
            "rejected": summary["rejected"],
            "truncated": summary["truncated"],
            "total": len(names),
            "by_name": _counted("event", names),
        },
        "delivery": {
            "total": len(delivery),
            "exported": len(delivery_kept),
            "acknowledged": sum(1 for entry in delivery if entry.get("acknowledged")),
            "by_status": _counted(
                "status", (entry.get("final_status") for entry in delivery)
            ),
            "records": delivery_kept,
        },
        "control": {
            "total": len(control),
            "exported": len(control_kept),
            "by_action": _counted("action", (entry.get("action") for entry in control)),
            "operation_ids": operations,
            "records": control_kept,
        },
    }


def _receipts_section(
    state_dir: Path, run_id: str | None, omissions: Omissions
) -> dict[str, Any]:
    """Summarize the mailbox as receipts: identities, kinds and body lengths.

    A mailbox line is the one place in ASL where an operator's words are
    durable, so this reads it through the narrowest allowlist in the module and
    reports each body as a character count. The count is the evidence a
    correlation actually needs — that a reply of some size closed a question —
    and it is not reversible into the reply.
    """
    records, summary = _read_jsonl(
        state_dir / mailbox_constants.MAILBOX_FILE_NAME, "receipts", omissions
    )

    entries: list[dict[str, Any]] = []
    for record in records:
        if run_id is not None and record.get("run_id") not in (None, run_id):
            continue
        entry = _pick(record, RECEIPT_FIELDS)
        for field in ("text", "note"):
            value = record.get(field)
            entry[f"{field}_chars"] = len(value) if isinstance(value, str) else 0
            if isinstance(value, str) and value:
                omissions.record("receipts", OMISSION_EXCLUDED_BODY, field)
        entries.append(entry)

    kept = _tail(entries, MAX_RECEIPT_RECORDS, "receipts", omissions)
    return {
        "available": summary["available"],
        "reason": summary["reason"],
        "total": len(entries),
        "exported": len(kept),
        "by_kind": _counted("kind", (entry.get("kind") for entry in entries)),
        "closure_ids": sorted(
            {
                str(entry["closure_id"])
                for entry in kept
                if isinstance(entry.get("closure_id"), str) and entry["closure_id"]
            }
        ),
        "records": kept,
    }


def _run_record(state_dir: Path, omissions: Omissions) -> dict[str, Any]:
    """Export one plan state directory as a single correlated run record."""
    record: dict[str, Any] = {
        # The state directory name *is* ``sha256(plan_dir)``, which makes it the
        # one correlation key that survives a share-mode bundle's path aliasing:
        # both systems can compute it from the same plan without either of them
        # publishing the path.
        "plan_digest": state_dir.name,
        "state_dir": str(state_dir),
        "plan_dir": None,
        "run_id": None,
        "state_schema_version": None,
        "state_available": False,
        "state_reason": None,
        "projection_status": PROJECTION_ABSENT,
        "projection": None,
        "phase": None,
        "projection_generation": None,
        "lifecycle_source_sequence": None,
        "assurance": None,
    }

    state_obj = state_plan_refs._read_json_object_safe(
        state_dir / events.STATE_FILE_NAME
    )
    if state_obj is None:
        exists = (state_dir / events.STATE_FILE_NAME).exists()
        reason = OMISSION_MALFORMED if exists else OMISSION_MISSING
        record["state_reason"] = reason
        omissions.record("state", reason, state_dir.name)
        record.update(_events_sections(state_dir, None, omissions))
        record["receipts"] = _receipts_section(state_dir, None, omissions)
        return record

    record["state_available"] = True
    record["plan_dir"] = _bounded_str(state_obj.get("plan_dir"))
    run_id = _bounded_str(state_obj.get("run_id"))
    record["run_id"] = run_id
    record["state_schema_version"] = state_schema_upgrade.state_schema_version(
        state_obj
    )

    status, projection = _projection_section(state_obj, omissions)
    record["projection_status"] = status
    record["projection"] = projection
    if projection is not None:
        record["phase"] = projection.get("phase")
        record["projection_generation"] = projection.get("projection_generation")
        record["lifecycle_source_sequence"] = projection.get(
            "lifecycle_source_sequence"
        )
    else:
        # The two counters are durable independently of the projection block, so
        # a run whose projection is absent or foreign still reports where its
        # generation and lifecycle sequence stand.
        record["projection_generation"] = _bounded_scalar(
            state_obj.get(agent_mux_projection.GENERATION_KEY)
        )
        record["lifecycle_source_sequence"] = _bounded_scalar(
            state_obj.get(agent_mux_projection.LIFECYCLE_SEQUENCE_KEY)
        )
        record["phase"] = agent_mux_projection.phase_for_payload(state_obj)

    record["assurance"] = _assurance_section(state_obj, omissions)
    record.update(_events_sections(state_dir, run_id, omissions))
    record["receipts"] = _receipts_section(state_dir, run_id, omissions)
    return record


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

__all__ = ["argparse", "hashlib", "json", "os", "sys", "Iterable", "Mapping", "Sequence", "Path", "IO", "Any", "__version__", "agent_mux_pairing", "agent_mux_projection", "events", "mailbox_constants", "plans_files", "responses", "state", "state_plan_refs", "state_schema_upgrade", "COMMAND_NAME", "EXPORT_SCHEMA_VERSION", "EXPORT_KIND", "REASON_OK", "REASON_STATE_ROOT_ERROR", "REASON_NO_PROFILE", "REASON_WRITE_FAILED", "OMISSION_MISSING", "OMISSION_UNREADABLE", "OMISSION_MALFORMED", "OMISSION_BOUNDED", "OMISSION_EXCLUDED_BODY", "OMISSION_EXCLUDED_DETAIL", "OMISSION_FOREIGN_CONTRACT", "OMISSION_UNPROJECTABLE", "PROJECTION_PRESENT", "PROJECTION_ABSENT", "PROJECTION_INVALID", "PROJECTION_FOREIGN", "MAX_RUNS", "MAX_DELIVERY_RECORDS", "MAX_CONTROL_RECORDS", "MAX_RECEIPT_RECORDS", "MAX_ISSUE_IDS", "MAX_LOG_LINES", "MAX_LOG_TAIL_BYTES", "LOG_TAIL_BLOCK_BYTES", "MAX_STRING_CHARS", "MAX_EXPORT_BYTES", "MAX_BINARY_HASH_BYTES", "DELIVERY_FIELDS", "CONTROL_FIELDS", "RECEIPT_FIELDS", "BODY_FIELDS", "REDACTION_POLICY", "canonical_json_bytes", "_bounded_str", "_bounded_scalar", "_pick", "_counted", "Omissions", "_tail_lines", "_read_jsonl", "_tail", "binary_sha256", "build_identity", "_projection_section", "_assurance_section", "_events_sections", "_receipts_section", "_run_record"]  # fmt: skip
