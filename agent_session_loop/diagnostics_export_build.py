"""Focused portion of agent_session_loop/diagnostics_export.py."""

from .diagnostics_export_sections import *


def _limits() -> dict[str, int]:
    """The caps this build applied, published so a reader need not assume them."""
    return {
        "max_runs": MAX_RUNS,
        "max_delivery_records": MAX_DELIVERY_RECORDS,
        "max_control_records": MAX_CONTROL_RECORDS,
        "max_receipt_records": MAX_RECEIPT_RECORDS,
        "max_issue_ids": MAX_ISSUE_IDS,
        "max_log_lines": MAX_LOG_LINES,
        "max_log_tail_bytes": MAX_LOG_TAIL_BYTES,
        "max_string_chars": MAX_STRING_CHARS,
        "max_export_bytes": MAX_EXPORT_BYTES,
    }


def _selects(record: Mapping[str, Any], run_id: str | None, digest: str | None) -> bool:
    if digest is not None and record["plan_digest"] != digest:
        return False
    return not (run_id is not None and record["run_id"] != run_id)


def _measurable(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the document as it will serialize, digest field included.

    The digest is a fixed-width hex string, so a placeholder of the same width
    measures the finished artifact exactly. Measuring the document without it
    would under-count by the one field every export carries.
    """
    return {**document, "digest": "0" * 64}


def _finalize(document: dict[str, Any]) -> dict[str, Any]:
    """Stamp the content digest over everything else in the document.

    Computed last and over the document minus this field, so the digest
    identifies the export's content rather than a serialization run. Two
    captures over frozen inputs produce the same digest; a bundle that records
    it can prove it re-read the same evidence.
    """
    document.pop("digest", None)
    document["digest"] = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
    return document


def build_export(
    *,
    state_dir: str | Path | None = None,
    run_id: str | None = None,
    plan_dir: str | Path | None = None,
    argv0: str | None = None,
) -> dict[str, Any]:
    """Return the sanitized AgentMux diagnostics export for one state root.

    Read-only in the strict sense: nothing under the state root is opened for
    writing, no lock is taken and no stale lock is reclaimed. A bundle must be
    capturable while a run is live, and while it is wedged.
    """
    omissions = Omissions()
    root = state.state_root(state_dir)
    selector_digest = (
        plans_files.path_sha256(plan_dir) if plan_dir not in (None, "") else None
    )
    document: dict[str, Any] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": EXPORT_KIND,
        "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
        "build": build_identity(argv0),
        "state_root": str(root),
        "state_root_present": root.is_dir(),
        "selector": {
            "run_id": run_id,
            "plan_dir": None if plan_dir is None else os.fspath(plan_dir),
            "plan_digest": selector_digest,
        },
        "limits": _limits(),
        "redaction_policy": [dict(entry) for entry in REDACTION_POLICY],
        "counts": {
            "plans_scanned": 0,
            "runs_matched": 0,
            "runs_exported": 0,
            "delivery_records": 0,
            "control_records": 0,
            "receipt_records": 0,
        },
        "truncated": False,
        "runs": [],
        "omissions": [],
    }

    try:
        state_dirs = list(state_plan_refs.iter_plan_state_dirs(root))
    except state.StateError as error:
        document["ok"] = False
        document["reason"] = REASON_STATE_ROOT_ERROR
        document["error"] = str(error)
        document["omissions"] = omissions.to_list()
        return _finalize(document)

    document["counts"]["plans_scanned"] = len(state_dirs)
    if selector_digest is not None:
        state_dirs = [entry for entry in state_dirs if entry.name == selector_digest]

    matched: list[dict[str, Any]] = []
    for entry in state_dirs:
        record = _run_record(entry, omissions)
        if _selects(record, run_id, selector_digest):
            matched.append(record)

    # Newest first by the projection's own clock, with the plan digest as the
    # tie-break so two runs that share a timestamp — or carry none — still order
    # identically on every host.
    matched.sort(key=lambda item: item.get("plan_digest") or "")
    matched.sort(
        key=lambda item: (item.get("projection") or {}).get("updated_at") or "",
        reverse=True,
    )
    document["counts"]["runs_matched"] = len(matched)

    if len(matched) > MAX_RUNS:
        omissions.record(
            "runs", OMISSION_BOUNDED, ["runs", MAX_RUNS], count=len(matched) - MAX_RUNS
        )
        matched = matched[:MAX_RUNS]
        document["truncated"] = True

    document["runs"] = matched
    document["ok"] = True
    document["reason"] = REASON_OK

    # Size is enforced on the serialized form rather than on an estimate, and it
    # is re-measured after every drop: recording an omission is itself bytes, so
    # a loop that trusted its first measurement could still overshoot.
    while True:
        _tally(document, matched, omissions)
        if len(canonical_json_bytes(_measurable(document))) <= MAX_EXPORT_BYTES:
            break
        if not matched:
            break
        dropped = matched.pop()
        omissions.record(
            "runs", OMISSION_BOUNDED, ["export-bytes", dropped["plan_digest"]]
        )
        document["truncated"] = True

    return _finalize(document)


def _tally(
    document: dict[str, Any], runs: list[dict[str, Any]], omissions: Omissions
) -> None:
    """Recompute the exported-record counts and omission ledger in place."""
    document["counts"]["runs_exported"] = len(runs)
    document["counts"]["delivery_records"] = sum(
        len(run.get("delivery", {}).get("records", ())) for run in runs
    )
    document["counts"]["control_records"] = sum(
        len(run.get("control", {}).get("records", ())) for run in runs
    )
    document["counts"]["receipt_records"] = sum(
        len(run.get("receipts", {}).get("records", ())) for run in runs
    )
    document["omissions"] = omissions.to_list()


# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------

__all__ = ["argparse", "hashlib", "json", "os", "sys", "Iterable", "Mapping", "Sequence", "Path", "IO", "Any", "__version__", "agent_mux_pairing", "agent_mux_projection", "events", "mailbox_constants", "plans_files", "responses", "state", "state_plan_refs", "state_schema_upgrade", "COMMAND_NAME", "EXPORT_SCHEMA_VERSION", "EXPORT_KIND", "REASON_OK", "REASON_STATE_ROOT_ERROR", "REASON_NO_PROFILE", "REASON_WRITE_FAILED", "OMISSION_MISSING", "OMISSION_UNREADABLE", "OMISSION_MALFORMED", "OMISSION_BOUNDED", "OMISSION_EXCLUDED_BODY", "OMISSION_EXCLUDED_DETAIL", "OMISSION_FOREIGN_CONTRACT", "OMISSION_UNPROJECTABLE", "PROJECTION_PRESENT", "PROJECTION_ABSENT", "PROJECTION_INVALID", "PROJECTION_FOREIGN", "MAX_RUNS", "MAX_DELIVERY_RECORDS", "MAX_CONTROL_RECORDS", "MAX_RECEIPT_RECORDS", "MAX_ISSUE_IDS", "MAX_LOG_LINES", "MAX_LOG_TAIL_BYTES", "LOG_TAIL_BLOCK_BYTES", "MAX_STRING_CHARS", "MAX_EXPORT_BYTES", "MAX_BINARY_HASH_BYTES", "DELIVERY_FIELDS", "CONTROL_FIELDS", "RECEIPT_FIELDS", "BODY_FIELDS", "REDACTION_POLICY", "canonical_json_bytes", "_bounded_str", "_bounded_scalar", "_pick", "_counted", "Omissions", "_tail_lines", "_read_jsonl", "_tail", "binary_sha256", "build_identity", "_projection_section", "_assurance_section", "_events_sections", "_receipts_section", "_run_record", "_limits", "_selects", "_measurable", "_finalize", "build_export", "_tally"]  # fmt: skip
