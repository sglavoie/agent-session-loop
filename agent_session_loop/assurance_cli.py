"""Compatibility façade for the focused :mod:`.assurance_cli` modules."""

from __future__ import annotations

# ruff: noqa: F401
import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import assurance_cli_actions as _actions
from . import assurance_ledger as ledger
from . import assurance_projection as projection
from . import assurance_reconcile as reconcile
from . import responses, state, state_plan_refs
from .assurance_cli_actions import (
    _confirm,
    _is_tty,
    _report_write_failure,
    _resolve_run_location,
)
from .assurance_cli_browse import (
    _show_text_lines,
    assurance_list_from_args,
    assurance_show_from_args,
)
from .assurance_cli_common import (
    COMMAND_ASSURANCE_CLOSEOUT,
    COMMAND_ASSURANCE_LIST,
    COMMAND_ASSURANCE_MERGE,
    COMMAND_ASSURANCE_PLAN,
    COMMAND_ASSURANCE_PROCESS_PENDING,
    COMMAND_ASSURANCE_RECONCILE,
    COMMAND_ASSURANCE_RECONCILE_APPLY,
    COMMAND_ASSURANCE_SHOW,
    DEFAULT_LIST_LIMIT,
    DEFAULT_LIST_STATE,
    DISPOSITION_CONSEQUENCE,
    MAX_RENDERED_ARTIFACTS,
    MAX_RENDERED_OCCURRENCES,
    MAX_RENDERED_RECONCILE_ROWS,
    MAX_RENDERED_WARNINGS,
    REASON_AMBIGUOUS_PLAN,
    REASON_AMBIGUOUS_RUN,
    REASON_CANCELLED,
    REASON_INPUT_ERROR,
    REASON_INVALID_ISSUE_ID,
    REASON_LEDGER_READ_FAILED,
    REASON_LEDGER_WRITE_FAILED,
    REASON_MANIFEST_READ_FAILED,
    REASON_UNKNOWN_ISSUE,
    REASON_UNKNOWN_RUN,
    SUMMARY_PREVIEW_CHARS,
    TEXT_LABEL_CLOSEOUT,
    TEXT_LABEL_LIST,
    TEXT_LABEL_MERGE,
    TEXT_LABEL_PLAN,
    TEXT_LABEL_PROCESS_PENDING,
    TEXT_LABEL_RECONCILE,
    TEXT_LABEL_SHOW,
    _issue_detail,
    _issue_row,
    _list_text_lines,
    _matching_issues,
    _observed_order,
    _occurrence_payload,
    _preview,
    _print_error,
    _print_warnings,
    _read_inbox,
    _run_payload,
    _warning_fields,
    _warning_payloads,
)
from .assurance_cli_pending import (
    _PLAN_HINTS,
    _pending_result_line,
    _plan_text_lines,
    _process_pending_text_lines,
    assurance_plan_from_args,
    assurance_process_pending_from_args,
)
from .assurance_cli_reconcile import (
    _read_manifest,
    _reconcile_apply,
    _reconcile_apply_text_lines,
    _reconcile_preview_text_lines,
    assurance_reconcile_from_args,
)
from .assurance_models_primitives import AssuranceError, issue_id_number, parse_issue_id
from .assurance_models_reconcile import (
    DECISION_MERGE,
    ReconciliationManifest,
    parse_reconciliation_manifest,
)
from .assurance_models_vocab import CLOSEOUT_OUTCOMES, ISSUE_STATES, SEVERITIES


def _call_action(handler: Any, args: argparse.Namespace) -> int:
    """Keep historical façade patch points attached to their action owner."""
    previous_confirm = _actions._confirm
    previous_is_tty = _actions._is_tty
    _actions._confirm = _confirm
    _actions._is_tty = _is_tty
    try:
        return handler(args)
    finally:
        _actions._confirm = previous_confirm
        _actions._is_tty = previous_is_tty


def assurance_disposition_from_args(args: argparse.Namespace) -> int:
    return _call_action(_actions.assurance_disposition_from_args, args)


def assurance_merge_from_args(args: argparse.Namespace) -> int:
    return _call_action(_actions.assurance_merge_from_args, args)


def assurance_closeout_from_args(args: argparse.Namespace) -> int:
    return _call_action(_actions.assurance_closeout_from_args, args)
