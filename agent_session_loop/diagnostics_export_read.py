"""Focused portion of agent_session_loop/diagnostics_export.py."""

from .diagnostics_export_common import *


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

__all__ = ["argparse", "hashlib", "json", "os", "sys", "Iterable", "Mapping", "Sequence", "Path", "IO", "Any", "__version__", "agent_mux_pairing", "agent_mux_projection", "events", "mailbox_constants", "plans_files", "responses", "state", "state_plan_refs", "state_schema_upgrade", "COMMAND_NAME", "EXPORT_SCHEMA_VERSION", "EXPORT_KIND", "REASON_OK", "REASON_STATE_ROOT_ERROR", "REASON_NO_PROFILE", "REASON_WRITE_FAILED", "OMISSION_MISSING", "OMISSION_UNREADABLE", "OMISSION_MALFORMED", "OMISSION_BOUNDED", "OMISSION_EXCLUDED_BODY", "OMISSION_EXCLUDED_DETAIL", "OMISSION_FOREIGN_CONTRACT", "OMISSION_UNPROJECTABLE", "PROJECTION_PRESENT", "PROJECTION_ABSENT", "PROJECTION_INVALID", "PROJECTION_FOREIGN", "MAX_RUNS", "MAX_DELIVERY_RECORDS", "MAX_CONTROL_RECORDS", "MAX_RECEIPT_RECORDS", "MAX_ISSUE_IDS", "MAX_LOG_LINES", "MAX_LOG_TAIL_BYTES", "LOG_TAIL_BLOCK_BYTES", "MAX_STRING_CHARS", "MAX_EXPORT_BYTES", "MAX_BINARY_HASH_BYTES", "DELIVERY_FIELDS", "CONTROL_FIELDS", "RECEIPT_FIELDS", "BODY_FIELDS", "REDACTION_POLICY", "canonical_json_bytes", "_bounded_str", "_bounded_scalar", "_pick", "_counted", "Omissions", "_tail_lines", "_read_jsonl", "_tail", "binary_sha256", "build_identity"]  # fmt: skip
