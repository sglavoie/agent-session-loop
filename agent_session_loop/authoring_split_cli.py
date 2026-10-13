"""``asl split`` command handlers and their payload helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import (
    harnesses,
    repository_intent,
    responses,
)
from .authoring_lint import (
    _plan_declares_registry,
    lint_plan_document,
    plan_footprint_payload,
)
from .authoring_paths import capture_inline_plan, extract_inline_plan
from .authoring_split import (
    MULTI_REPOSITORY_MESSAGE,
    SPLIT_REASON_INPUT_ERROR,
    SPLIT_REASON_MALFORMED_PLAN,
    SPLIT_REASON_REGISTRY_REQUIRED,
    SPLIT_REASON_SINGLE_SESSION,
    InvalidContractError,
    MissingContractError,
    SingleSessionError,
    split_plan,
)
from .plans_errors import PlanError
from .plans_models import ReadinessMetadata
from .workspaces import (
    RepositoryRegistry,
)


def _readiness_payload(metadata: ReadinessMetadata) -> dict[str, Any]:
    return {
        "state": metadata.state,
        "readiness_sha256": metadata.readiness_sha256,
        "checked_at": metadata.checked_at,
    }


def _registry_payload(registry: RepositoryRegistry | None) -> dict[str, Any]:
    if registry is None:
        return {}
    return {
        "repositories": [repository.to_dict() for repository in registry],
        "registry_digest": registry.digest,
    }


def _split_next_steps(output_dir: Path) -> list[dict[str, Any]]:
    """Session-expand invocations offered as the split plan's next step."""
    step = responses.invocation_step(
        (
            "Validate the split plan against the codebase and mark it "
            "loop-ready before running it."
        ),
        harnesses.skill_invocations("session-expand", str(output_dir)),
    )
    return responses.next_steps_payload([step])["next_steps"]


def _resolve_skill_prefix(skill_prefix: str) -> str:
    """Resolve a ``--skill-prefix`` choice to a prefix char.

    ``auto`` detects the invoking harness and raises :class:`ValueError` when
    detection is ambiguous; a named harness maps directly to its prefix. The
    failure is raised rather than returned beside the prefix so the resolved
    value is a plain ``str`` — the caller's ``except ValueError`` is the same
    input-error path every other resolution step in this command already
    takes.
    """
    if skill_prefix == "auto":
        detected = harnesses.detect_invoking_harness()
        if detected is None:
            raise ValueError(
                "could not auto-detect the invoking harness; pass "
                "--skill-prefix {codex,claude,copilot}"
            )
        return harnesses.skill_prefixes()[detected]
    prefix = harnesses.skill_prefixes().get(skill_prefix)
    if prefix is None:
        raise ValueError(f"unknown skill prefix harness: {skill_prefix}")
    return prefix


def _emit_split_error(
    reason: str, error: str, *, want_json: bool, **fields: Any
) -> int:
    if want_json:
        responses.print_json(
            responses.error_payload(
                "split", reason=reason, error=error, paused=False, **fields
            )
        )
    else:
        print(f"agent-session-loop split: {error}", file=sys.stderr)
    return 1


def check_from_args(args: Any) -> int:
    """Run ``asl split --check``: lint a source plan and write nothing.

    Deliberately skips ``capture_inline_plan`` so a repair loop can re-check the
    same plan repeatedly without persisting a source file per attempt.
    """
    want_json = bool(args.json)

    if args.inline_stdin:
        source_path = "<stdin>"
        try:
            text = extract_inline_plan(sys.stdin.read())
        except ValueError as error:
            return _emit_split_error(
                SPLIT_REASON_INPUT_ERROR, str(error), want_json=want_json
            )
    else:
        source_path = os.path.expanduser(str(args.plan_file))
        if not Path(source_path).is_file():
            return _emit_split_error(
                SPLIT_REASON_INPUT_ERROR,
                f"source plan file does not exist: {args.plan_file}",
                want_json=want_json,
            )
        try:
            text = Path(source_path).read_text(encoding="utf-8")
        except OSError as error:
            return _emit_split_error(
                SPLIT_REASON_INPUT_ERROR,
                f"cannot read source plan {source_path}: {error}",
                want_json=want_json,
                source_path=source_path,
            )

    check_cwd = getattr(args, "cwd", None)
    result = lint_plan_document(text, cwd=check_cwd, source_path=source_path)
    footprint_fields: dict[str, Any] = {}
    if result.repository_footprint is not None:
        footprint_fields["repository_footprint"] = result.repository_footprint

    # Source-only evidence is conclusive when the plan's own prose already
    # resolves into two repositories. Anything less waits for the
    # contract-aware write path, which also sees the contracts file.
    registry_required = False
    if check_cwd is not None and result.registry is None:
        intent = repository_intent.plan_intent(
            text,
            root=check_cwd,
            provenance_roots=repository_intent.provenance_exempt_roots(source_path),
        )
        registry_required = intent.registry_required
        if registry_required:
            footprint_fields["repository_intent"] = repository_intent.intent_report(
                intent
            )

    if result.findings:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "split",
                    reason=(
                        SPLIT_REASON_REGISTRY_REQUIRED
                        if registry_required
                        else SPLIT_REASON_MALFORMED_PLAN
                    ),
                    error=result.findings[0].message,
                    paused=False,
                    checked=True,
                    source_path=source_path,
                    session_ids=list(result.session_ids),
                    findings=[finding.as_dict() for finding in result.findings],
                    **_registry_payload(result.registry),
                    **footprint_fields,
                )
            )
        else:
            print(
                f"agent-session-loop split --check: {len(result.findings)} "
                f"malformed session heading(s) in {source_path}",
                file=sys.stderr,
            )
            for finding in result.findings:
                print(f"  {finding.message}", file=sys.stderr)
                if finding.suggested_line is not None:
                    print(f"    suggested: {finding.suggested_line}", file=sys.stderr)
        return 1

    if registry_required:
        return _emit_split_error(
            SPLIT_REASON_REGISTRY_REQUIRED,
            MULTI_REPOSITORY_MESSAGE,
            want_json=want_json,
            checked=True,
            source_path=source_path,
            session_ids=list(result.session_ids),
            **footprint_fields,
        )

    if len(result.session_ids) < 2:
        return _emit_split_error(
            SPLIT_REASON_SINGLE_SESSION,
            str(SingleSessionError(len(result.session_ids))),
            want_json=want_json,
            checked=True,
            source_path=source_path,
            session_ids=list(result.session_ids),
            **_registry_payload(result.registry),
            **footprint_fields,
        )

    if want_json:
        responses.print_json(
            responses.success_payload(
                "split",
                checked=True,
                source_path=source_path,
                session_ids=list(result.session_ids),
                session_count=len(result.session_ids),
                **_registry_payload(result.registry),
                **footprint_fields,
            )
        )
    else:
        print(f"Checked {len(result.session_ids)} sessions in {source_path}")
        if result.registry is not None:
            print(f"  Repositories: {', '.join(result.registry.aliases)}")
        for session_id in result.session_ids:
            print(f"  Session {session_id}")
    return 0


def split_from_args(args: Any) -> int:
    """Run the ``split`` subcommand from CLI arguments."""
    want_json = bool(args.json)
    base_dir = args.base_dir
    common_fields: dict[str, Any] = {}

    def _emit_error(reason: str, error: str, **fields: Any) -> int:
        return _emit_split_error(
            reason, error, want_json=want_json, **{**common_fields, **fields}
        )

    if args.check:
        return check_from_args(args)

    if args.contracts_file is None:
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            "--contracts-file is required unless --check is given",
        )

    try:
        prefix = _resolve_skill_prefix(args.skill_prefix)
    except ValueError as error:
        return _emit_error(SPLIT_REASON_INPUT_ERROR, str(error))

    # Resolve the source plan (inline stdin capture or an explicit file).
    if args.inline_stdin:
        raw_input = sys.stdin.read()
        try:
            captured = capture_inline_plan(raw_input, base_dir)
        except ValueError as error:
            return _emit_error(SPLIT_REASON_INPUT_ERROR, str(error))
        source_path = str(captured["source_path"])
    else:
        source_path = os.path.expanduser(str(args.plan_file))
        if not Path(source_path).is_file():
            return _emit_error(
                SPLIT_REASON_INPUT_ERROR,
                f"source plan file does not exist: {args.plan_file}",
            )
    common_fields["source_path"] = source_path

    # Load the Step 7 contracts keyed by canonical session id.
    try:
        contracts_text = Path(os.path.expanduser(str(args.contracts_file))).read_text(
            encoding="utf-8"
        )
    except OSError as error:
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            f"cannot read contracts file {args.contracts_file}: {error}",
        )
    try:
        contracts_by_id = json.loads(contracts_text)
    except json.JSONDecodeError as error:
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            f"contracts file is not valid JSON: {error.msg} at line {error.lineno}",
        )
    if not isinstance(contracts_by_id, dict):
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            "contracts file must be a JSON object keyed by session id",
        )

    footprint_fields: dict[str, Any] = {}
    split_cwd = getattr(args, "cwd", None)
    if split_cwd is not None:
        try:
            plan_text = Path(source_path).read_text(encoding="utf-8")
        except OSError as error:
            return _emit_error(
                SPLIT_REASON_INPUT_ERROR,
                f"cannot read source plan {source_path}: {error}",
            )
        footprint = plan_footprint_payload(
            plan_text,
            split_cwd,
            contracts_by_id=contracts_by_id,
            source_path=source_path,
        )
        if footprint is not None:
            footprint_fields["repository_footprint"] = footprint
        registry_declared = _plan_declares_registry(plan_text)
        # The contracts file names locations the prose never mentions, and a
        # contract target routinely does not exist yet, so this verdict can be
        # conclusive where `--check` was not. It runs before the output
        # directory is resolved, so a refusal leaves nothing behind.
        intent = repository_intent.plan_intent(
            plan_text,
            root=split_cwd,
            contracts_by_id=contracts_by_id,
            provenance_roots=repository_intent.provenance_exempt_roots(source_path),
        )
        if intent.registry_required and not registry_declared:
            footprint_fields["repository_intent"] = repository_intent.intent_report(
                intent
            )
            return _emit_error(
                SPLIT_REASON_REGISTRY_REQUIRED,
                MULTI_REPOSITORY_MESSAGE,
                **footprint_fields,
            )
        if footprint is not None and not footprint["registry_present"]:
            return _emit_error(
                SPLIT_REASON_MALFORMED_PLAN,
                (
                    "plan reaches outside a single repository but declares "
                    "no `## Repositories` table; add the registry and "
                    "per-session repository scopes before splitting"
                ),
                **footprint_fields,
            )

    analysis_note: str | None = None
    if args.analysis_file:
        try:
            analysis_note = Path(os.path.expanduser(str(args.analysis_file))).read_text(
                encoding="utf-8"
            )
        except OSError as error:
            return _emit_error(
                SPLIT_REASON_INPUT_ERROR,
                f"cannot read analysis file {args.analysis_file}: {error}",
            )

    try:
        result = split_plan(
            plan_file=source_path,
            contracts_by_id=contracts_by_id,
            base_dir=base_dir,
            skill_prefix=prefix,
            analysis_note=analysis_note,
            force_new=bool(getattr(args, "force_new", False)),
        )
    except SingleSessionError as error:
        return _emit_error(SPLIT_REASON_SINGLE_SESSION, str(error))
    except MissingContractError as error:
        return _emit_error(f"missing-contract:{error.session_id}", str(error))
    except InvalidContractError as error:
        return _emit_error(f"invalid-contract:{error.session_id}", str(error))
    except PlanError as error:
        return _emit_error(SPLIT_REASON_MALFORMED_PLAN, str(error))

    payload = {
        "output_dir": str(result.output_dir),
        "plan_id": result.plan_id,
        "source_path": result.source_path,
        "session_ids": list(result.session_ids),
        "files": [str(path) for path in result.files],
        "readiness": _readiness_payload(result.readiness),
        "next_steps": _split_next_steps(result.output_dir),
        "skill_prefix": result.skill_prefix,
        **_registry_payload(result.registry),
        **footprint_fields,
    }
    if want_json:
        responses.print_json(responses.success_payload("split", **payload))
    else:
        print(f"Split {len(result.session_ids)} sessions from {result.source_path}")
        print(f"Output directory: {result.output_dir}")
        print(f"Plan ID: {result.plan_id}")
        if result.registry is not None:
            print(f"Repositories: {', '.join(result.registry.aliases)}")
        for path in result.files:
            print(f"  {path}")
        print(f"Readiness: {result.readiness.state}")
    return 0
