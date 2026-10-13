"""The public sanitized diagnostics export AgentMux reads instead of ASL state.

``amx debug bundle`` has to explain one causal path that crosses two processes:
a lifecycle source event ASL emitted, the request it was delivered under, the
projection generation it advanced, and what AgentMux's daemon then decided. Half
of that evidence lives in ASL's ``state.json``, ``events.jsonl`` and
``mailbox.jsonl``, which are private formats: they carry question bodies, reply
text, harness prompts and whatever a harness wrote into a reason string, and
their shapes change whenever the loop changes. A debug bundle that parsed them
would be a second consumer of every internal file ASL owns, and would leak
bodies into an artifact meant to be shareable.

So this module is the seam. It is the *only* supported way for another program
to read ASL's side of the AgentMux correlation, and it is built to three
properties that make it safe to hand to a bundle:

*Bounded.* Every collection has a stated cap and every cap that bites is
reported. A run with ten thousand events contributes counts, not ten thousand
records, and the export never grows past :data:`MAX_EXPORT_BYTES`. The bound on
an append-only log is a bound on its *tail*, read backwards from the end: the
records that explain a failure are the newest ones, so a cap that kept the
oldest would bound the work and lose the evidence.

*Deterministic.* The document carries no wall clock, no iteration-order
dependence and no host entropy: over frozen inputs the same bytes come out
every time, which is what lets a bundle assert byte equality across two
captures. The one timestamp-shaped thing in it is ``digest``, and that is a
hash of the content.

*Control-plane only.* Field allowlists, not denylists, decide what is copied.
``text``, ``note``, ``message``, prompts, model output and bridge stderr have
no path into the document at all — not because a filter removes them, but
because nothing ever reads them. What is withheld is still *counted*, under
:data:`REDACTION_POLICY`, so a reader can tell "nothing was there" from
"something was there and this export will not show it".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from . import (
    __version__,
    agent_mux_pairing,
    agent_mux_projection,
    events,
    mailbox_constants,
    plans_files,
    responses,
    state,
    state_plan_refs,
    state_schema_upgrade,
)

COMMAND_NAME = "diagnostics-export"

#: The document contract. AgentMux refuses an export whose major version it does
#: not implement rather than guessing at unknown fields, so this is bumped only
#: when an existing field changes meaning — additive fields do not bump it.
EXPORT_SCHEMA_VERSION = 1

#: What the document *is*, restated inside it. A bundle member that has been
#: copied out of its archive still names its own producer.
EXPORT_KIND = "asl-agent-mux-diagnostics"

# Exit reasons. Part of the command contract: AgentMux branches on them.
REASON_OK = "ok"
REASON_STATE_ROOT_ERROR = "state-root-error"
REASON_NO_PROFILE = "no-export-profile"
REASON_WRITE_FAILED = "write-failed"

# Omission reasons. Every one names a distinct thing a reader can do about it.
OMISSION_MISSING = "missing"
OMISSION_UNREADABLE = "unreadable"
OMISSION_MALFORMED = "malformed"
OMISSION_BOUNDED = "bounded"
OMISSION_EXCLUDED_BODY = "excluded-body"
OMISSION_EXCLUDED_DETAIL = "excluded-detail"
OMISSION_FOREIGN_CONTRACT = "foreign-contract-set"
OMISSION_UNPROJECTABLE = "unprojectable"

# Projection availability, as a closed vocabulary rather than a null plus prose.
PROJECTION_PRESENT = "present"
PROJECTION_ABSENT = "absent"
PROJECTION_INVALID = "invalid"
PROJECTION_FOREIGN = "foreign-contract-set"

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

#: Plans exported in one document. A state root accumulates one directory per
#: plan forever; a bundle wants the recent correlation, not the archive.
MAX_RUNS = 25

#: Per-run record caps. Each is a *tail* cap: the most recent records are the
#: ones that explain the failure being debugged, so older ones are dropped
#: first and counted.
MAX_DELIVERY_RECORDS = 50
MAX_CONTROL_RECORDS = 50
MAX_RECEIPT_RECORDS = 50
MAX_ISSUE_IDS = 10

#: Lines read from one append-only log. This bounds work, not just output: an
#: unrotated log must not turn a debug bundle into a full-file read. The lines
#: read are the log's *newest* ones: these files only ever grow, so a bounded
#: prefix of one is a window onto whatever the run was doing hours ago, while
#: the records that explain the failure being debugged are at the end.
MAX_LOG_LINES = 20000

#: Bytes read back from the end of one append-only log. ``MAX_LOG_LINES`` bounds
#: that tail by record count, which is the bound a reader reasons about; this one
#: exists because a log whose lines are pathologically long would otherwise have
#: to read the whole file to reach that many of them. Whichever bites first stops
#: the read.
MAX_LOG_TAIL_BYTES = 16_777_216

#: Bytes per backward step while locating that tail. One step covers an ordinary
#: log's entire retained window, so the common case is a single seek and read.
LOG_TAIL_BLOCK_BYTES = 262_144

#: Longest string copied into the document. Reason and classification strings
#: are enumerated codes in practice; the cap exists for the build that decides
#: otherwise.
MAX_STRING_CHARS = 256

#: Ceiling on the serialized document. Runs are dropped from the end until the
#: document fits, and the drop is recorded.
MAX_EXPORT_BYTES = 1_048_576

#: Ceiling on the entry point hashed for ``binary_sha256``. A launcher script is
#: kilobytes and a frozen binary is megabytes; anything past this is not the
#: thing this build is, and hashing it would be an unbounded read.
MAX_BINARY_HASH_BYTES = 67_108_864

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

#: Delivery outcome fields copied verbatim. Everything ASL records about a
#: lifecycle delivery *except* the per-attempt error detail, which is harness
#: text; ``attempt_count`` carries the part a reader acts on.
DELIVERY_FIELDS: tuple[str, ...] = (
    "acknowledged",
    "agent_mux_event",
    "attempt_count",
    "error_classification",
    "final_status",
    "harness",
    "reason",
    "request_id",
    "run_id",
    "source_event_id",
    "source_sequence",
    "trace_id",
)

#: Control receipt fields copied verbatim. ``command_id`` is the bridge
#: operation ID: it is what joins a bridge call to the run that observed it.
CONTROL_FIELDS: tuple[str, ...] = (
    "action",
    "command_id",
    "queue_command",
    "session",
    "stop_after_current",
    "total_sessions",
    "turn_index",
)

#: Mailbox fields copied verbatim. Note what is absent: ``text`` and ``note``
#: are the question, reply and resolution bodies, and they are never read.
RECEIPT_FIELDS: tuple[str, ...] = (
    "action",
    "closure_id",
    "created_at",
    "id",
    "kind",
    "origin",
    "run_id",
    "seq",
    "session_id",
    "supervision_id",
    "to",
    "turn_index",
    "wait_id",
)

#: Body-bearing fields whose *presence* is reported as a length and whose
#: content is never copied. Keyed by the section that would have carried them.
BODY_FIELDS: tuple[tuple[str, str], ...] = (
    ("receipts", "text"),
    ("receipts", "note"),
    ("control", "message"),
)

#: The standing statement of what this export refuses to carry, shipped inside
#: the document so an auditor reading one bundle member does not have to find
#: this file to know what it could not have contained.
REDACTION_POLICY: tuple[dict[str, str], ...] = (
    {
        "field": "mailbox.text",
        "reason": "question, reply and supervision bodies are never exported",
    },
    {
        "field": "mailbox.note",
        "reason": "resolution notes are never exported",
    },
    {
        "field": "run_control.message",
        "reason": "operator control messages are never exported",
    },
    {
        "field": "delivery.attempts",
        "reason": "per-attempt transport detail may quote bridge stderr",
    },
    {
        "field": "harness.prompt",
        "reason": "prompts and raw model output are not control-plane evidence",
    },
    {
        "field": "verification.output",
        "reason": "verification output is unbounded and may quote source",
    },
    {
        "field": "environment",
        "reason": "process environment may carry credentials",
    },
)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize to stable, newline-terminated UTF-8 JSON.

    Restated here rather than imported from :mod:`assurance_models_primitives` so the
    export stays readable by a consumer that has only this module's contract:
    the byte-equality promise is this function, and it should be visible beside
    the promise.
    """
    text = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return text.encode("utf-8") + b"\n"


def _bounded_str(value: Any) -> str | None:
    """Return a length-capped string, or ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    return value[:MAX_STRING_CHARS]


def _bounded_scalar(value: Any) -> Any:
    """Copy one allowlisted field value, capping strings and dropping the rest.

    Only JSON scalars survive. A field that arrived as a nested object is not
    something this contract describes, and copying it would smuggle an
    unreviewed shape — and whatever it contains — into the document.
    """
    if isinstance(value, str):
        return value[:MAX_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _pick(payload: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Project an allowlist over a payload, always producing every key."""
    return {field: _bounded_scalar(payload.get(field)) for field in fields}


def _counted(label: str, values: Iterable[Any]) -> list[dict[str, Any]]:
    """Tally occurrences into a sorted list of labelled records.

    A list of ``{label: value, "count": n}`` records rather than a mapping keyed
    by the tallied values themselves. Consumers that redact by key allowlist can
    vet two fixed keys once; a mapping would present each tallied value as a
    key, and every such allowlist would drop the whole breakdown as unknown.
    """
    counts: dict[str, int] = {}
    for value in values:
        key = value if isinstance(value, str) and value else "unknown"
        counts[key] = counts.get(key, 0) + 1
    return [{label: key, "count": counts[key]} for key in sorted(counts)]


class Omissions:
    """Counts, digests and reasons for everything the export left out.

    Modeled on the audit bundle's omission ledger: a reader needs to tell an
    empty section from a withheld one, and needs a digest that identifies
    *which* payloads were withheld without being able to reconstruct them.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}

    def record(self, section: str, reason: str, payload: Any, count: int = 1) -> None:
        entry = self._entries.setdefault(
            (section, reason),
            {
                "section": section,
                "reason": reason,
                "count": 0,
                "digest": hashlib.sha256(),
            },
        )
        entry["count"] += count
        entry["digest"].update(canonical_json_bytes(payload))

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {
                "section": entry["section"],
                "reason": entry["reason"],
                "count": entry["count"],
                "sha256": entry["digest"].hexdigest(),
            }
            for entry in sorted(
                self._entries.values(),
                key=lambda item: (item["section"], item["reason"]),
            )
        ]


def _tail_lines(handle: IO[bytes], max_lines: int) -> tuple[list[str], bool]:
    """Return a log's newest ``max_lines`` physical lines, oldest first.

    Read backwards from the end in blocks, so the cost is the retained window
    rather than the file. Reading forwards and stopping at the cap costs the same
    on a short log and gives the wrong answer on a long one — it keeps the oldest
    records and never observes the newest, which are the ones an export exists to
    carry.

    The second element says whether anything was left before the window, which is
    what separates "this is the whole log" from "this is its tail".
    """
    handle.seek(0, os.SEEK_END)
    end = handle.tell()
    position = end
    blocks: list[bytes] = []
    newlines = 0
    while position > 0 and end - position < MAX_LOG_TAIL_BYTES:
        step = min(
            LOG_TAIL_BLOCK_BYTES, position, MAX_LOG_TAIL_BYTES - (end - position)
        )
        position -= step
        handle.seek(position)
        block = handle.read(step)
        blocks.insert(0, block)
        newlines += block.count(b"\n")
        # One line past the window before stopping: until the read reaches the
        # start of the file its first line may have begun earlier, so that line
        # is not one this pass may keep, and the window has to be complete
        # without it. Splitting on b"\n" is safe on UTF-8, whose multi-byte
        # sequences never contain that byte.
        if newlines > max_lines:
            break
    lines = b"".join(blocks).splitlines(keepends=True)
    if position > 0 and lines:
        lines = lines[1:]
    truncated = position > 0 or len(lines) > max_lines
    selected = lines[-max_lines:] if max_lines > 0 else []
    return [line.decode("utf-8", "replace") for line in selected], truncated


def _read_jsonl(
    path: Path, section: str, omissions: Omissions
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read a bounded tail of an append-only JSON-lines log.

    The bound is the file's actual tail, not a prefix of it, and ``scanned``
    counts the lines that tail contained. Everything downstream — the malformed
    tally, the omission digests, the per-section ``_tail`` caps — then applies to
    the newest records rather than to the oldest ones.

    Damage is counted rather than raised: a log that a crash truncated mid-line
    is itself evidence, and refusing to export the surrounding run because of it
    would withhold the very correlation being debugged.
    """
    summary: dict[str, Any] = {
        "available": False,
        "reason": None,
        "scanned": 0,
        "rejected": 0,
        "truncated": False,
    }
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        summary["reason"] = OMISSION_MISSING
        omissions.record(section, OMISSION_MISSING, path.name)
        return [], summary
    except OSError:
        summary["reason"] = OMISSION_UNREADABLE
        omissions.record(section, OMISSION_UNREADABLE, path.name)
        return [], summary

    try:
        with handle:
            lines, truncated = _tail_lines(handle, MAX_LOG_LINES)
    except OSError:
        # A tail read seeks, so an input a forward read would have streamed —
        # a FIFO left in the state directory — fails here instead. Every
        # unavailable input degrades to a named reason rather than to a raise.
        summary["reason"] = OMISSION_UNREADABLE
        omissions.record(section, OMISSION_UNREADABLE, path.name)
        return [], summary

    if truncated:
        summary["truncated"] = True
        omissions.record(section, OMISSION_BOUNDED, [path.name, "scan"])
    summary["scanned"] = len(lines)

    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError, ValueError:
            summary["rejected"] += 1
            # The digest covers the line's own hash, never the line: a
            # malformed record is exactly the record most likely to be a
            # half-written body.
            omissions.record(
                section,
                OMISSION_MALFORMED,
                hashlib.sha256(line.encode("utf-8", "replace")).hexdigest(),
            )
            continue
        if not isinstance(parsed, dict):
            summary["rejected"] += 1
            omissions.record(section, OMISSION_MALFORMED, path.name)
            continue
        records.append(parsed)
    summary["available"] = True
    return records, summary


def _tail(
    records: list[dict[str, Any]], limit: int, section: str, omissions: Omissions
) -> list[dict[str, Any]]:
    """Keep the newest ``limit`` records, counting whatever that drops."""
    if len(records) <= limit:
        return records
    dropped = len(records) - limit
    omissions.record(section, OMISSION_BOUNDED, [section, limit], count=dropped)
    return records[-limit:]


# ---------------------------------------------------------------------------
# Build identity
# ---------------------------------------------------------------------------


def binary_sha256(executable: str | None) -> str | None:
    """Hash the entry point this build is running from.

    The version string says which release this claims to be; the hash says which
    bytes actually ran. A bundle correlating an ASL export with an AgentMux
    binary needs both, because the interesting failure is the one where the two
    sides agree on version and disagree on behavior.

    Symlinks are followed here — unlike in ``resolved_executable``, which
    deliberately reports the stable launcher name. The name is the identity
    worth persisting; the target is the bytes worth hashing.
    """
    if not executable:
        return None
    path = Path(executable)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > MAX_BINARY_HASH_BYTES:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def build_identity(argv0: str | None = None) -> dict[str, Any]:
    """Return the build block: who is speaking and which bytes are speaking."""
    executable = agent_mux_pairing.resolved_executable(argv0)
    return {
        "product": "agent-session-loop",
        "version": __version__,
        "executable": executable,
        "binary_sha256": binary_sha256(executable),
        "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
        "state_schema_version": agent_mux_projection.STATE_SCHEMA_VERSION,
        "projection_schema_version": agent_mux_projection.PROJECTION_SCHEMA_VERSION,
        "hook_schema_version": agent_mux_projection.HOOK_SCHEMA_VERSION,
        "bridge_protocol": agent_mux_pairing.BRIDGE_PROTOCOL_VERSION,
    }


# ---------------------------------------------------------------------------
# Per-run sections
# ---------------------------------------------------------------------------


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


def _write_output(path: str, payload: bytes) -> None:
    """Write the export beside its destination and rename it into place."""
    target = Path(path)
    temp = target.with_name(f".{target.name}.tmp")
    with temp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def export_from_args(args: argparse.Namespace) -> int:
    """``asl diagnostics export`` entry point."""
    if not getattr(args, "agent_mux", False):
        # A profile flag rather than an implicit default: the next consumer gets
        # its own profile, and a caller that forgot to name one must not silently
        # receive AgentMux's.
        message = "no export profile selected; pass --agent-mux"
        if getattr(args, "json", False):
            responses.print_json(
                responses.error_payload(
                    COMMAND_NAME,
                    reason=REASON_NO_PROFILE,
                    error=message,
                    paused=False,
                )
            )
        else:
            print(f"{COMMAND_NAME}: {message}", file=sys.stderr)
        return 2

    document = build_export(
        state_dir=getattr(args, "state_dir", None),
        run_id=getattr(args, "run_id", None),
        plan_dir=getattr(args, "plan_dir", None),
    )
    payload = canonical_json_bytes(document)

    output = getattr(args, "output", None)
    if output:
        try:
            _write_output(output, payload)
        except OSError as error:
            message = f"could not write export to {output}: {error}"
            if getattr(args, "json", False):
                responses.print_json(
                    responses.error_payload(
                        COMMAND_NAME,
                        reason=REASON_WRITE_FAILED,
                        error=message,
                        paused=False,
                    )
                )
            else:
                print(f"{COMMAND_NAME}: {message}", file=sys.stderr)
            return 1

    ok = bool(document.get("ok"))
    if getattr(args, "json", False):
        fields = {
            "export": document,
            "output": output or None,
            "digest": document["digest"],
        }
        if ok:
            responses.print_json(
                responses.success_payload(
                    COMMAND_NAME, reason=document.get("reason", REASON_OK), **fields
                )
            )
        else:
            responses.print_json(
                responses.error_payload(
                    COMMAND_NAME,
                    reason=str(document.get("reason") or REASON_STATE_ROOT_ERROR),
                    error=str(document.get("error") or ""),
                    paused=False,
                    **fields,
                )
            )
    elif not output:
        # The bare invocation writes the document itself, byte for byte, because
        # that is what a consuming bundle stores. Wrapping it in a human summary
        # would make the caller parse an envelope to reach the artifact whose
        # byte stability is the whole point.
        sys.stdout.write(payload.decode("utf-8"))
    else:
        print(f"Wrote {output} ({len(payload)} bytes, digest {document['digest']})")
        if not ok:
            sys.stdout.flush()
            print(f"{COMMAND_NAME}: {document.get('error', '')}", file=sys.stderr)

    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write ASL's sanitized AgentMux diagnostics export.",
    )
    parser.add_argument(
        "--agent-mux",
        action="store_true",
        help="Export the AgentMux correlation profile",
    )
    parser.add_argument("--state-dir", help="Override operational state directory")
    parser.add_argument("--run-id", help="Limit the export to one run ID")
    parser.add_argument("--plan-dir", help="Limit the export to one plan directory")
    parser.add_argument("--output", help="Write the export to PATH instead of stdout")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    return export_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
