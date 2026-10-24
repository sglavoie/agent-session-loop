"""The ``asl contract-check`` subcommand: static per-check contract diagnosis."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import (
    agent_mux_contract,
    plans_errors,
    plans_session_ids,
    plans_snapshot,
    responses,
    state,
)
from .cli_defaults import program_name
from .contracts import (
    CommandCheck,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
    read_completion_contract,
)
from .plans_session_ids import SessionId
from .verification_cli import _verification_context_payload
from .verification_command_diagnosis import (
    _is_bare_python_invocation,
    diagnose_command,
    diagnose_command_scope,
)
from .verification_models import (
    VerificationContext,
    _coerce_verification_context,
    verification_context_from_plan,
)

CONTRACT_CHECK_EXIT_REASON_OK = "ok"
CONTRACT_CHECK_EXIT_REASON_ISSUES = "contract-issues"
CONTRACT_CHECK_EXIT_REASON_INVALID_SESSION = "invalid-session"
CONTRACT_CHECK_EXIT_REASON_CWD_ERROR = "cwd-error"
CONTRACT_CHECK_EXIT_REASON_PLAN_ERROR = "plan-error"
#: ``--agent-mux`` found the corpus itself inconsistent.
CONTRACT_CHECK_EXIT_REASON_CONTRACT_SET_ISSUES = "contract-set-issues"
#: ``--agent-mux`` could not read the corpus at all.
CONTRACT_CHECK_EXIT_REASON_CONTRACT_SET_ERROR = "contract-set-error"

#: Per-check row statuses.
CONTRACT_CHECK_ROW_OK = "ok"
CONTRACT_CHECK_ROW_SCHEMA_ERROR = "schema-error"
CONTRACT_CHECK_ROW_MISSING_CONTRACT = "missing-contract"
CONTRACT_CHECK_ROW_FACTUAL_ISSUE = "factual-issue"

#: ``factual-issue`` sub-reasons.
CONTRACT_CHECK_ISSUE_COMMAND_NOT_RUNNABLE = "command-not-runnable"
CONTRACT_CHECK_ISSUE_FILE_MISSING = "file-missing"
CONTRACT_CHECK_ISSUE_REGEX_NOT_MATCHED = "regex-not-matched"
CONTRACT_CHECK_ISSUE_BARE_PYTHON = "bare-python"


def _contract_check_session_row(
    session: str, status: str, detail: str
) -> dict[str, Any]:
    return {
        "session": session,
        "name": None,
        "type": None,
        "repo": None,
        "resolved_root": None,
        "cwd": None,
        "qualified_cwd": None,
        "path": None,
        "qualified_path": None,
        "status": status,
        "reason": None,
        "detail": detail,
    }


def _diagnose_contract_check(
    check: VerificationCheck, *, context: VerificationContext
) -> tuple[str, str | None, str]:
    """Statically diagnose one check; return ``(status, reason, detail)``.

    No command is executed and no file is written: command runnability is
    diagnosed by :func:`diagnose_command`/:func:`diagnose_command_scope` (both
    mapped to ``command-not-runnable``), an unversioned ``python`` interpreter
    invocation is flagged ``bare-python``, and file/regex checks are resolved
    against the check's selected repository for existence and pattern-match.
    """
    resolved = context.resolve_check(check)
    if isinstance(check, CommandCheck):
        if _is_bare_python_invocation(check.argv):
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_BARE_PYTHON,
                (
                    "unversioned `python` is frequently unavailable where "
                    f"`python3` exists: {' '.join(check.argv)}; use `python3`"
                ),
            )
        scope = diagnose_command_scope(check)
        if scope is not None:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_COMMAND_NOT_RUNNABLE,
                scope.detail,
            )
        diagnostic = diagnose_command(
            replace(check, cwd=str(resolved.cwd)),
            repo_root=resolved.resolved_root,
        )
        if diagnostic is not None:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_COMMAND_NOT_RUNNABLE,
                diagnostic.detail,
            )
        return (
            CONTRACT_CHECK_ROW_OK,
            None,
            f"command is runnable: {' '.join(check.argv)}",
        )

    if isinstance(check, FileExistsCheck):
        assert resolved.path is not None
        target = resolved.path
        try:
            exists = target.exists()
        except OSError as error:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_FILE_MISSING,
                f"cannot stat {check.path}: {error}",
            )
        if exists:
            return (CONTRACT_CHECK_ROW_OK, None, f"file exists: {check.path}")
        return (
            CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
            CONTRACT_CHECK_ISSUE_FILE_MISSING,
            f"file does not exist: {check.path}",
        )

    if isinstance(check, RegexInFileCheck):
        assert resolved.path is not None
        target = resolved.path
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_FILE_MISSING,
                f"file does not exist: {check.path}",
            )
        except OSError as error:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_FILE_MISSING,
                f"cannot read {check.path}: {error}",
            )
        if re.compile(check.pattern, re.MULTILINE).search(text) is None:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_REGEX_NOT_MATCHED,
                f"pattern did not match in {check.path}: {check.pattern}",
            )
        return (CONTRACT_CHECK_ROW_OK, None, f"pattern matched in {check.path}")

    return (CONTRACT_CHECK_ROW_OK, None, "")


def _contract_check_rows(
    plan_dir: str | Path,
    session_id: SessionId,
    *,
    repo_root: Path | None = None,
    context: VerificationContext | None = None,
) -> list[dict[str, Any]]:
    """Return per-check rows for one session's completion contract."""
    verification_context = _coerce_verification_context(
        repo_root=repo_root, context=context
    )
    try:
        contract = read_completion_contract(plan_dir, session_id)
    except plans_errors.PlanError as error:
        return [
            _contract_check_session_row(
                session_id.value, CONTRACT_CHECK_ROW_SCHEMA_ERROR, str(error)
            )
        ]
    if contract is None:
        return [
            _contract_check_session_row(
                session_id.value,
                CONTRACT_CHECK_ROW_MISSING_CONTRACT,
                f"session {session_id.value} has no Completion Contract section",
            )
        ]

    rows: list[dict[str, Any]] = []
    for check in contract.verify.checks:
        try:
            resolved = verification_context.resolve_check(check)
            status, reason, detail = _diagnose_contract_check(
                check, context=verification_context
            )
        except plans_errors.PlanError as error:
            rows.append(
                {
                    "session": session_id.value,
                    "name": check.name,
                    "type": check.kind,
                    "repo": check.repo,
                    "resolved_root": None,
                    "cwd": None,
                    "qualified_cwd": None,
                    "path": None,
                    "qualified_path": None,
                    "status": CONTRACT_CHECK_ROW_SCHEMA_ERROR,
                    "reason": "repository-contract",
                    "detail": str(error),
                }
            )
            continue
        rows.append(
            {
                "session": session_id.value,
                "name": check.name,
                "type": check.kind,
                "repo": check.repo,
                "resolved_root": str(resolved.resolved_root),
                "cwd": str(resolved.cwd) if resolved.cwd is not None else None,
                "qualified_cwd": resolved.qualified_cwd,
                "path": str(resolved.path) if resolved.path is not None else None,
                "qualified_path": resolved.qualified_path,
                "status": status,
                "reason": reason,
                "detail": detail,
            }
        )
    return rows


def contract_check_from_args(args: argparse.Namespace) -> int:
    """Run the ``contract-check`` subcommand from CLI arguments.

    Statically checks each session's completion-contract ``Verify`` recipe
    without executing any command: it wraps
    :func:`agent_session_loop.contracts.read_completion_contract`,
    :func:`diagnose_command`/:func:`diagnose_command_scope`, and file/regex
    resolution. ``--session ID`` narrows to one session (the single-file mode
    plan-wide preflight cannot express). Every check produces a row whose status
    is ``ok``, ``schema-error``, ``missing-contract``, or ``factual-issue`` (with
    a ``command-not-runnable``/``file-missing``/``regex-not-matched``/
    ``bare-python`` reason). Exit 0 only when every row is ``ok``.
    """
    plan_dir_arg = str(args.plan_dir)
    raw_session = getattr(args, "session", None)
    cwd_arg = args.cwd
    want_json = bool(args.json)
    common_fields: dict[str, Any] = {
        "plan_dir": plan_dir_arg,
        "session": str(raw_session) if raw_session is not None else None,
    }

    def _emit_error(reason: str, error: str) -> int:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "contract-check",
                    reason=reason,
                    error=error,
                    paused=False,
                    **common_fields,
                )
            )
        else:
            print(f"{program_name()} contract-check: {error}", file=sys.stderr)
        return 1

    try:
        verification_context = verification_context_from_plan(plan_dir_arg, cwd_arg)
    except (plans_errors.PlanError, OSError) as error:
        return _emit_error(CONTRACT_CHECK_EXIT_REASON_CWD_ERROR, str(error))

    try:
        snapshot = plans_snapshot.inspect_plan(plan_dir_arg)
    except plans_errors.PlanError as error:
        return _emit_error(CONTRACT_CHECK_EXIT_REASON_PLAN_ERROR, str(error))

    known_ids = {status.id for status in snapshot.sessions}
    if raw_session is not None:
        try:
            session_id = plans_session_ids.parse_session_id(raw_session)
        except (ValueError, plans_errors.PlanError) as error:
            return _emit_error(CONTRACT_CHECK_EXIT_REASON_INVALID_SESSION, str(error))
        if session_id.value not in known_ids:
            return _emit_error(
                CONTRACT_CHECK_EXIT_REASON_INVALID_SESSION,
                f"session {session_id.value} is not present in {plan_dir_arg}",
            )
        session_ids = [session_id]
    else:
        session_ids = [
            plans_session_ids.parse_session_id(status.id)
            for status in snapshot.sessions
        ]

    rows: list[dict[str, Any]] = []
    for session_id in session_ids:
        rows.extend(
            _contract_check_rows(plan_dir_arg, session_id, context=verification_context)
        )

    issues = [row for row in rows if row["status"] != CONTRACT_CHECK_ROW_OK]
    result_fields = {
        "cwd": str(verification_context.root),
        **_verification_context_payload(verification_context),
        "checks": rows,
        "issue_count": len(issues),
    }

    agent_mux_ok = True
    if bool(getattr(args, "agent_mux", False)):
        try:
            corpus = agent_mux_contract.check_corpus()
            state_root = str(state.state_root(getattr(args, "state_dir", None)))
        except (agent_mux_contract.ContractError, OSError) as error:
            return _emit_error(
                CONTRACT_CHECK_EXIT_REASON_CONTRACT_SET_ERROR, str(error)
            )
        result_fields["agent_mux"] = agent_mux_contract.report_payload(
            corpus, state_root=state_root
        )
        agent_mux_ok = corpus.ok

    if not issues and agent_mux_ok:
        if want_json:
            responses.print_json(
                responses.success_payload(
                    "contract-check",
                    reason=CONTRACT_CHECK_EXIT_REASON_OK,
                    **{**common_fields, **result_fields},
                )
            )
        else:
            print(f"Plan: {plan_dir_arg}")
            root_label = (
                "Workspace root"
                if verification_context.workspace is not None
                else "Repo root"
            )
            print(f"{root_label}: {verification_context.root}")
            print(f"Checks: {len(rows)} (all ok)")
            _print_agent_mux_block(result_fields.get("agent_mux"))
        return 0

    if issues:
        reason = CONTRACT_CHECK_EXIT_REASON_ISSUES
        failure_message = f"{len(issues)} of {len(rows)} contract check(s) have issues"
    else:
        reason = CONTRACT_CHECK_EXIT_REASON_CONTRACT_SET_ISSUES
        failure_message = "the AgentMux compatibility contract set is inconsistent"
    if want_json:
        responses.print_json(
            responses.error_payload(
                "contract-check",
                reason=reason,
                error=failure_message,
                paused=False,
                **{**common_fields, **result_fields},
            )
        )
    else:
        print(f"Plan: {plan_dir_arg}")
        root_label = (
            "Workspace root"
            if verification_context.workspace is not None
            else "Repo root"
        )
        print(f"{root_label}: {verification_context.root}")
        for row in rows:
            marker = "OK" if row["status"] == CONTRACT_CHECK_ROW_OK else "ISSUE"
            label = row["name"] or row["status"]
            row_reason = f" ({row['reason']})" if row["reason"] else ""
            repository = f" {row.get('repo')}:" if row.get("repo") else ""
            print(
                f"  [{marker}]{repository} session {row['session']} "
                f"{label}{row_reason}: "
                f"{row['detail']}"
            )
        _print_agent_mux_block(result_fields.get("agent_mux"))
        print(f"contract-check: {failure_message}", file=sys.stderr)
    return 1


def _print_agent_mux_block(payload: Mapping[str, Any] | None) -> None:
    """Print the ``--agent-mux`` compatibility report in human-readable form."""
    if payload is None:
        return
    print(f"Contract set: {payload['contract_set_id']}")
    print(f"Corpus digest: {payload['corpus_digest']}")
    print(f"Corpus root: {payload['corpus_root']}")
    print(f"State root: {payload['state_root']}")
    for component in payload["components"]:
        print(
            f"  [{component['schema_status']}] {component['component']} "
            f"v{component.get('schema_version')} {component.get('digest')}"
        )
    fixtures = payload["fixtures"]
    print(
        f"  fixtures: {fixtures['total']} "
        f"({fixtures['accepted']} accepted, {fixtures['rejected']} rejected)"
    )
    for issue in payload["issues"]:
        print(
            f"  [ISSUE] {issue['reason']} at {issue['pointer'] or '/'}: "
            f"{issue['detail']}"
        )
