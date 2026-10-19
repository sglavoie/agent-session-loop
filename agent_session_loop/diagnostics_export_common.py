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


__all__ = ["argparse", "hashlib", "json", "os", "sys", "Iterable", "Mapping", "Sequence", "Path", "IO", "Any", "__version__", "agent_mux_pairing", "agent_mux_projection", "events", "mailbox_constants", "plans_files", "responses", "state", "state_plan_refs", "state_schema_upgrade", "COMMAND_NAME", "EXPORT_SCHEMA_VERSION", "EXPORT_KIND", "REASON_OK", "REASON_STATE_ROOT_ERROR", "REASON_NO_PROFILE", "REASON_WRITE_FAILED", "OMISSION_MISSING", "OMISSION_UNREADABLE", "OMISSION_MALFORMED", "OMISSION_BOUNDED", "OMISSION_EXCLUDED_BODY", "OMISSION_EXCLUDED_DETAIL", "OMISSION_FOREIGN_CONTRACT", "OMISSION_UNPROJECTABLE", "PROJECTION_PRESENT", "PROJECTION_ABSENT", "PROJECTION_INVALID", "PROJECTION_FOREIGN", "MAX_RUNS", "MAX_DELIVERY_RECORDS", "MAX_CONTROL_RECORDS", "MAX_RECEIPT_RECORDS", "MAX_ISSUE_IDS", "MAX_LOG_LINES", "MAX_LOG_TAIL_BYTES", "LOG_TAIL_BLOCK_BYTES", "MAX_STRING_CHARS", "MAX_EXPORT_BYTES", "MAX_BINARY_HASH_BYTES", "DELIVERY_FIELDS", "CONTROL_FIELDS", "RECEIPT_FIELDS", "BODY_FIELDS", "REDACTION_POLICY", "canonical_json_bytes", "_bounded_str", "_bounded_scalar", "_pick", "_counted", "Omissions"]  # fmt: skip
