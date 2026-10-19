"""Focused portion of agent_session_loop/diagnostics_export.py."""

from .diagnostics_export_build import *


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

__all__ = ["argparse", "hashlib", "json", "os", "sys", "Iterable", "Mapping", "Sequence", "Path", "IO", "Any", "__version__", "agent_mux_pairing", "agent_mux_projection", "events", "mailbox_constants", "plans_files", "responses", "state", "state_plan_refs", "state_schema_upgrade", "COMMAND_NAME", "EXPORT_SCHEMA_VERSION", "EXPORT_KIND", "REASON_OK", "REASON_STATE_ROOT_ERROR", "REASON_NO_PROFILE", "REASON_WRITE_FAILED", "OMISSION_MISSING", "OMISSION_UNREADABLE", "OMISSION_MALFORMED", "OMISSION_BOUNDED", "OMISSION_EXCLUDED_BODY", "OMISSION_EXCLUDED_DETAIL", "OMISSION_FOREIGN_CONTRACT", "OMISSION_UNPROJECTABLE", "PROJECTION_PRESENT", "PROJECTION_ABSENT", "PROJECTION_INVALID", "PROJECTION_FOREIGN", "MAX_RUNS", "MAX_DELIVERY_RECORDS", "MAX_CONTROL_RECORDS", "MAX_RECEIPT_RECORDS", "MAX_ISSUE_IDS", "MAX_LOG_LINES", "MAX_LOG_TAIL_BYTES", "LOG_TAIL_BLOCK_BYTES", "MAX_STRING_CHARS", "MAX_EXPORT_BYTES", "MAX_BINARY_HASH_BYTES", "DELIVERY_FIELDS", "CONTROL_FIELDS", "RECEIPT_FIELDS", "BODY_FIELDS", "REDACTION_POLICY", "canonical_json_bytes", "_bounded_str", "_bounded_scalar", "_pick", "_counted", "Omissions", "_tail_lines", "_read_jsonl", "_tail", "binary_sha256", "build_identity", "_projection_section", "_assurance_section", "_events_sections", "_receipts_section", "_run_record", "_limits", "_selects", "_measurable", "_finalize", "build_export", "_tally", "_write_output", "export_from_args", "main"]  # fmt: skip
