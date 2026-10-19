# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
#!/usr/bin/env python3
"""Resplit a pending session through focused parser, contract, and transaction modules."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import resplit_contracts as _contracts
import resplit_execution as _execution
import resplit_parsing as _parsing
import resplit_render as _render
import resplit_shared as _shared

for _module in (_shared, _parsing, _contracts, _render, _execution):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


def _walk_up_for_plan_dir(start: Path) -> Path | None:
    current = plans_files.canonicalize_path(start)
    if current.is_file():
        current = current.parent
    while True:
        if (
            (current / "context.md").is_file()
            and (current / "manifest.md").is_file()
            and any(
                plans_session_ids.session_id_from_filename(path.name)
                for path in current.iterdir()
            )
        ):
            return current
        if current.parent == current:
            return None
        current = current.parent


def _resolve_cli_target(args: argparse.Namespace) -> tuple[Path, str | None]:
    if args.target is None:
        inferred = _walk_up_for_plan_dir(Path.cwd())
        if inferred is None:
            raise ResplitError(
                "ambiguous-input",
                "no plan directory argument and none could be inferred from cwd",
            )
        return inferred, args.session

    target = plans_files.canonicalize_path(args.target)
    direct_session_id = plans_session_ids.session_id_from_filename(target.name)
    if direct_session_id is not None:
        if args.session is not None:
            raise ResplitError(
                "ambiguous-input",
                "cannot combine a direct session file with --session",
            )
        if not target.is_file():
            raise ResplitError(
                "ambiguous-input", f"session file does not exist: {target}"
            )
        return target.parent, direct_session_id.value

    if not target.is_dir():
        raise ResplitError(
            "ambiguous-input", f"plan directory does not exist: {target}"
        )
    return target, args.session


def _parse_count(args: argparse.Namespace) -> int | None:
    values = [value for value in (args.count, args.parts) if value is not None]
    if len(values) > 1:
        raise ResplitError("ambiguous-input", "provide child count only once")
    return _validate_parts(values[0]) if values else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resplit one pending session file into nested child sessions."
    )
    parser.add_argument(
        "target", nargs="?", help="Plan directory or session-<id>.md file"
    )
    parser.add_argument("count", nargs="?", type=int, help="Child count, 2 through 20")
    parser.add_argument("--session", help="Explicit session id to resplit")
    parser.add_argument("--parts", type=int, help="Child count, 2 through 20")
    parser.add_argument(
        "--child-spec",
        type=Path,
        help=(
            "JSON list or {children: [...]} with title/instructions/done_when "
            "plus checks and/or inherit_checks per child"
        ),
    )
    parser.add_argument(
        "--state-dir", help="Override agent-session-loop state directory"
    )
    parser.add_argument(
        "--skill-prefix",
        default="$",
        choices=("$", "/"),
        help="Skill invocation prefix to write into generated guidance",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    return parser


def _print_result(result: dict[str, Any]) -> None:
    print(
        f"Resplit session {result['parent_id']} into {', '.join(result['children'])}."
    )
    print(f"Child files: {', '.join(result['child_files'])}")
    print(f"Archive directory: {result['archive_dir']}")
    archived = result["archived_files"] or ["none"]
    print(f"Archived files: {', '.join(archived)}")
    ledgers = result["ledger_updates"] or ["none"]
    print(f"Derived ledgers updated: {', '.join(ledgers)}")
    references = result.get("reference_updates") or ["none"]
    print(f"Downstream references updated: {', '.join(references)}")
    readiness = result.get("readiness") or {}
    if readiness:
        print(f"Readiness metadata refreshed: {readiness.get('state', 'draft')}")
    print("Rerun session-expand before continuing execution.")
    print("RESPLIT_STATUS: complete")
    print(f"Next command: {result['next_command']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        plan_dir, session_id = _resolve_cli_target(args)
        count = _parse_count(args)
        child_specs = _load_child_specs(args.child_spec)
        result = resplit_session(
            plan_dir,
            session_id=session_id,
            parts=count,
            child_specs=child_specs,
            state_dir=args.state_dir,
            skill_prefix=args.skill_prefix,
        )
    except (plans_errors.PlanError, ResplitError) as error:
        reason = error.reason if isinstance(error, ResplitError) else "malformed-plan"
        detail = error.detail if isinstance(error, ResplitError) else str(error)
        remediation = PAUSE_REMEDIATION.get(reason)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": reason,
                        "detail": detail,
                        "remediation": remediation,
                    },
                    sort_keys=True,
                )
            )
        else:
            print("RESPLIT_STATUS: paused")
            print(f"RESPLIT_PAUSE: {reason} - {detail}")
            if remediation:
                print(f"RESPLIT_REMEDIATION: {remediation}")
        return 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
