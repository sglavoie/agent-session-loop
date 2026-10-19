# ruff: noqa: F403, F405
from .repository_registration_derivation import (
    register_repositories,
    register_source_plan,
)
from .repository_registration_models import *

REGISTER_REASON_OK = "ok"
REGISTER_REASON_REFUSED = "refused"
REGISTER_REASON_PLAN_STRUCTURE = "plan-structure"
REGISTER_REASON_LOCK_CONTENTION = "lock-contention"
REGISTER_REASON_INPUT_ERROR = "input-error"


def result_payload(result: RegistrationResult) -> dict[str, Any]:
    """Serialize a :class:`RegistrationResult` for the ``--json`` surface."""
    return {
        "status": result.status,
        "missing_fact": result.missing_fact,
        "detail": result.detail,
        "workspace_root": (
            None if result.workspace_root is None else str(result.workspace_root)
        ),
        "repositories": [repository.to_dict() for repository in result.repositories],
        "sessions_changed": list(result.sessions_changed),
        "files": [str(path) for path in result.files],
        "readiness": result.readiness,
        "next_commands": list(result.next_commands),
        "markdown": result.markdown,
    }


def _emit_register_error(
    reason: str, error: str, *, want_json: bool, **fields: Any
) -> int:
    if want_json:
        responses.print_json(
            responses.error_payload(
                "register-repositories",
                reason=reason,
                error=error,
                **fields,
            )
        )
    else:
        print(
            f"agent-session-loop register-repositories: {error}",
            file=sys.stderr,
        )
    return 1


def register_repositories_from_args(args: Any) -> int:
    """Run the ``register-repositories`` subcommand from CLI arguments."""
    source_mode = bool(args.plan_file) or bool(args.inline_stdin)
    if source_mode == (args.plan_dir is not None):
        return _emit_register_error(
            REGISTER_REASON_INPUT_ERROR,
            "give exactly one of PLAN_DIR, --plan-file, or --inline-stdin",
            want_json=bool(args.json),
        )
    if source_mode:
        return _register_source_plan_from_args(args)
    return _register_plan_dir_from_args(args)


def _register_plan_dir_from_args(args: Any) -> int:
    """Migrate a split plan directory in place."""
    want_json = bool(args.json)
    plan_dir = str(args.plan_dir)

    def _emit_error(reason: str, error: str, **fields: Any) -> int:
        return _emit_register_error(
            reason, error, want_json=want_json, plan_dir=plan_dir, **fields
        )

    if args.output is not None:
        return _emit_error(
            REGISTER_REASON_INPUT_ERROR,
            "--output belongs to source-plan mode; a plan directory is "
            "migrated in place",
        )

    # The migration rewrites every session file, and PlanTransaction leaves
    # serializing writers to its caller, so a live runner owns the plan first.
    lock_info = state_locks.inspect_lock_info(plan_dir, args.state_dir)
    if lock_info is not None and lock_info.active:
        return _emit_error(
            REGISTER_REASON_LOCK_CONTENTION,
            f"plan is locked by pid {lock_info.pid} at {lock_info.path}; "
            "register the registry once that run has finished",
        )

    try:
        result = register_repositories(plan_dir, cwd=args.cwd)
    except plans_errors.PlanError as error:
        return _emit_error(REGISTER_REASON_PLAN_STRUCTURE, str(error))

    payload = result_payload(result)
    if result.status == "refused":
        return _emit_error(
            REGISTER_REASON_REFUSED,
            f"missing fact: {result.missing_fact}: {result.detail}",
            **payload,
        )

    if want_json:
        responses.print_json(
            responses.success_payload(
                "register-repositories",
                reason=REGISTER_REASON_OK,
                plan_dir=plan_dir,
                **payload,
            )
        )
        return 0

    print(f"Plan directory: {plan_dir}")
    print(f"Status: {result.status}")
    if result.status != "applied":
        print(result.detail)
        return 0
    print(f"Workspace root: {result.workspace_root}")
    for line in repository_scope.registry_table_lines(result.repositories):
        print(line)
    print("Changed files:")
    for path in result.files:
        print(f"  {path}")
    print("Run next, in this order:")
    for command in result.next_commands:
        print(f"  {command}")
    return 0


def _register_source_plan_from_args(args: Any) -> int:
    """Emit a migrated source plan, never writing over its input."""
    want_json = bool(args.json)
    output = None if args.output is None else os.path.expanduser(str(args.output))

    def _emit_error(reason: str, error: str, **fields: Any) -> int:
        return _emit_register_error(
            reason, error, want_json=want_json, source_path=source_path, **fields
        )

    if args.inline_stdin:
        source_path = "<stdin>"
        try:
            text = authoring_paths.extract_inline_plan(sys.stdin.read())
        except ValueError as error:
            return _emit_error(REGISTER_REASON_INPUT_ERROR, str(error))
    else:
        source_path = os.path.expanduser(str(args.plan_file))
        if not Path(source_path).is_file():
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                f"source plan file does not exist: {args.plan_file}",
            )
        if output is not None and os.path.realpath(output) == os.path.realpath(
            source_path
        ):
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                "--output names the input plan; the migrated markdown never "
                "replaces its source",
            )
        try:
            text = Path(source_path).read_text(encoding="utf-8")
        except OSError as error:
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                f"cannot read source plan {source_path}: {error}",
            )

    try:
        result = register_source_plan(text, cwd=args.cwd)
    except plans_errors.PlanError as error:
        return _emit_error(REGISTER_REASON_PLAN_STRUCTURE, str(error))

    payload = result_payload(result)
    if result.status == "refused":
        return _emit_error(
            REGISTER_REASON_REFUSED,
            f"missing fact: {result.missing_fact}: {result.detail}",
            **payload,
        )

    if output is not None:
        try:
            Path(output).write_text(result.markdown or "", encoding="utf-8")
        except OSError as error:
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                f"cannot write migrated plan {output}: {error}",
            )

    if want_json:
        responses.print_json(
            responses.success_payload(
                "register-repositories",
                reason=REGISTER_REASON_OK,
                source_path=source_path,
                output_path=output,
                **payload,
            )
        )
        return 0

    # Without --output the markdown is the whole of stdout, so a caller may
    # pipe it straight into `asl split`.
    if output is None:
        print(result.markdown or "", end="")
        return 0

    print(f"Source plan: {source_path}")
    print(f"Status: {result.status}")
    print(f"Migrated plan: {output}")
    if result.status != "applied":
        print(result.detail)
        return 0
    print(f"Workspace root: {result.workspace_root}")
    for line in repository_scope.registry_table_lines(result.repositories):
        print(line)
    return 0
