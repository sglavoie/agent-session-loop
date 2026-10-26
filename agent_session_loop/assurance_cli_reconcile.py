"""Focused implementation extracted from :mod:`.assurance_cli`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_reconcile as reconcile
from . import responses
from .assurance_cli_actions import (
    _confirm,
    _report_write_failure,
)
from .assurance_cli_common import (
    COMMAND_ASSURANCE_RECONCILE,
    COMMAND_ASSURANCE_RECONCILE_APPLY,
    MAX_RENDERED_RECONCILE_ROWS,
    REASON_CANCELLED,
    REASON_MANIFEST_READ_FAILED,
    TEXT_LABEL_RECONCILE,
    _print_error,
    _print_warnings,
    _read_inbox,
    _warning_fields,
)
from .assurance_models_primitives import AssuranceError
from .assurance_models_reconcile import (
    DECISION_MERGE,
    ReconciliationManifest,
    parse_reconciliation_manifest,
)

_RECONCILE_HINTS: dict[str, str] = {
    reconcile.STALE_CATALOG_DIGEST: (
        "run `asl assurance reconcile --json` again and review the new preview"
    ),
    reconcile.UNRESOLVED_DECISION: (
        "rewrite each 'review' decision as 'merge' or 'keep-distinct'"
    ),
    reconcile.IDENTITY_KEY_CONFLICT: (
        "run `asl assurance show ISSUE` to see which issue already owns the key"
    ),
    REASON_MANIFEST_READ_FAILED: (
        "generate one with `asl assurance reconcile --json | jq .manifest`"
    ),
}


def _read_manifest(args: argparse.Namespace) -> ReconciliationManifest:
    """Load and validate the reviewed manifest named by ``--apply``."""
    path = Path(args.apply).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AssuranceError(f"cannot read manifest {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise AssuranceError(f"manifest {path} is not UTF-8 text: {error}") from error
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise AssuranceError(f"manifest {path} is not valid JSON: {error}") from error
    return parse_reconciliation_manifest(payload)


def _reconcile_preview_text_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Inbox: {payload['issue_count']} canonical issue(s), "
        f"{payload['bound_count']} with an identity key.",
        f"Catalog: {payload['catalog_digest']}",
    ]
    bindings = payload["bindings"]
    if not bindings:
        lines.append("Every canonical issue already has its identity key.")
    else:
        lines.append(f"Safe bindings to add ({len(bindings)}):")
        for item in bindings[:MAX_RENDERED_RECONCILE_ROWS]:
            lines.append(f"  bind {item['identity_key']} -> {item['issue_id']}")
        omitted = len(bindings) - MAX_RENDERED_RECONCILE_ROWS
        if omitted > 0:
            lines.append(f"  ... and {omitted} more")
    for item in payload["blocked"]:
        lines.append(
            f"  blocked {item['issue_id']} ({item['reason']}): {item['detail']}"
        )
    unresolved = payload["unresolved"]
    if not unresolved:
        lines.append("No pairs need review.")
    else:
        lines.append(f"Pairs awaiting a verdict ({len(unresolved)}):")
        for item in unresolved[:MAX_RENDERED_RECONCILE_ROWS]:
            reasons = ", ".join(item["reasons"]) or "none"
            lines.append(
                f"  review {' + '.join(item['issue_ids'])} "
                f"(reasons: {reasons}; similarity {item['similarity']:.2f})"
            )
        omitted = len(unresolved) - MAX_RENDERED_RECONCILE_ROWS
        if omitted > 0:
            lines.append(f"  ... and {omitted} more")
    if payload["truncated_pairs"]:
        lines.append(
            f"{payload['truncated_pairs']} weaker pair(s) were not offered; "
            "reconcile again after applying this manifest to see them."
        )
    if payload["suppressed_pairs"]:
        lines.append(
            f"{payload['suppressed_pairs']} pair(s) were already reviewed as "
            "distinct defects and are not asked about again."
        )
    lines.append("Nothing was written. To act on this:")
    lines.append("  asl assurance reconcile --json | jq .manifest > manifest.json")
    lines.append("  # rewrite each 'review' decision as 'merge' or 'keep-distinct'")
    lines.append("  asl assurance reconcile --apply manifest.json --yes")
    return lines


def _reconcile_apply_text_lines(payload: dict[str, Any]) -> list[str]:
    if not payload["applied"]:
        return [
            "Every decision in this manifest is already recorded; nothing was written."
        ]
    lines = [
        f"Applied {payload['records']} record(s): {payload['bindings']} binding(s), "
        f"{payload['kept_distinct']} ruling(s) and {payload['merges']} merge(s).",
    ]
    if payload["kept_distinct"]:
        lines.append(
            f"{payload['kept_distinct']} pair(s) were reviewed as distinct defects; "
            "they stop being suggested until one of them changes."
        )
    if payload["merges"]:
        lines.append("Merging is irreversible.")
    lines.append(f"Ledger: {payload['ledger_path']}")
    return lines


def _reconcile_apply(args: argparse.Namespace) -> int:
    """Apply one reviewed manifest as a single append, or report why not."""
    try:
        manifest = _read_manifest(args)
    except AssuranceError as error:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_RECONCILE_APPLY,
            text_label=TEXT_LABEL_RECONCILE,
            reason=REASON_MANIFEST_READ_FAILED,
            error=str(error),
            hint=_RECONCILE_HINTS[REASON_MANIFEST_READ_FAILED],
        )

    merges = manifest.of_kind(DECISION_MERGE)
    if merges and not _confirm(
        args,
        f"Apply {len(manifest.decisions)} decision(s), including {len(merges)} "
        "merge(s)? Merging cannot be undone.",
    ):
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_RECONCILE_APPLY,
            text_label=TEXT_LABEL_RECONCILE,
            reason=REASON_CANCELLED,
            error="applying the reconciliation manifest was cancelled",
        )

    try:
        result = reconcile.apply_manifest(manifest, state_dir=args.state_dir)
    except (AssuranceError, OSError) as error:
        return _report_write_failure(
            args,
            error,
            command_name=COMMAND_ASSURANCE_RECONCILE_APPLY,
            text_label=TEXT_LABEL_RECONCILE,
            hints=_RECONCILE_HINTS,
        )

    payload = responses.success_payload(
        COMMAND_ASSURANCE_RECONCILE_APPLY,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        manifest_path=str(Path(args.apply).expanduser()),
        catalog_digest=manifest.catalog_digest,
        **result.to_dict(),
    )
    if args.json:
        responses.print_json(payload)
        return 0
    for line in _reconcile_apply_text_lines(payload):
        print(line)
    return 0


def assurance_reconcile_from_args(args: argparse.Namespace) -> int:
    """Preview the inbox's identity repairs, or apply a reviewed manifest.

    Without ``--apply`` this reads and reports and writes nothing at all, not
    even the manifest it prints: the preview is the input to a human decision,
    and a command that left a file behind would invite applying one nobody read.
    """
    if args.apply is not None:
        return _reconcile_apply(args)

    inbox = _read_inbox(
        args,
        command_name=COMMAND_ASSURANCE_RECONCILE,
        text_label=TEXT_LABEL_RECONCILE,
    )
    if inbox is None:
        return 1

    warnings = _warning_fields(inbox)
    preview = reconcile.build_preview(inbox)
    payload = responses.success_payload(
        COMMAND_ASSURANCE_RECONCILE,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        **preview.to_dict(),
        **warnings,
    )
    if args.json:
        responses.print_json(payload)
        return 0
    _print_warnings(warnings)
    for line in _reconcile_preview_text_lines(payload):
        print(line)
    return 0
