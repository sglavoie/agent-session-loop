"""Safe ``asl run`` command parsing and child-argument building."""

from __future__ import annotations

import argparse
import os
import shlex
from typing import Any

from asl_autopilot_constants import (
    _ENV_ASSIGNMENT_RE,
    _SHELL_OPERATOR_CHARS,
    DEFAULT_MAX_REVIEW_FIX_CYCLES,
    INVOCATION_ORIGIN,
    PARSE_STAGE,
)

from agent_session_loop import cli_trace as _cli_trace
from agent_session_loop import review as _review
from agent_session_loop import run_flags as _run_flags
from agent_session_loop import run_inputs as _run_inputs


class _ParseError(Exception):
    """Raised internally when the embedded ``asl run`` parser rejects args."""


class _RunArgumentParser(argparse.ArgumentParser):
    """`asl run` flag mirror that raises instead of calling ``sys.exit``."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ParseError(message)


def _expand_user_only(value: str) -> str:
    """Expand a leading ``~``/``~user`` without touching shell variables.

    The helper never runs a shell, so tilde home-directory expansion (which a
    shell would normally do) has to happen here. ``os.path.expanduser`` only
    rewrites a leading tilde and leaves ``$VAR`` untouched, which is exactly the
    "expand ~, do not expand shell variables" contract for this session.
    """
    return os.path.expanduser(value)


def _parse_error(reason: str, error: str) -> dict:
    """Build a stable structured parse-failure payload."""
    return {
        "ok": False,
        "stage": PARSE_STAGE,
        "reason": reason,
        "error": error,
    }


def _build_run_parser() -> _RunArgumentParser:
    """Return a parser for the user-facing ``asl run`` flag surface.

    Built from the shared declarative spec in
    ``agent_session_loop.run_flags`` (``mode="supervisor"``: ``None`` defaults so
    the reconstruction below can tell which flags the operator actually passed).
    The runner's real CLI subparser is built from the same spec, so the two can
    never drift.
    """
    parser = _RunArgumentParser(prog="asl run", add_help=False)
    _run_flags.add_run_arguments(parser, mode="supervisor")
    return parser


# Dests the reconstructed child ``asl run`` argv never carries. Most are
# helper-owned or supplied separately; ``fresh_conversation`` is here because it
# is a one-use operator authorization to discard a saved conversation. The
# helper relaunches the same session on every recoverable pause, so forwarding
# it would re-authorize a fresh start on each retry — turning one deliberate
# decision into a standing one. Its counterpart at the other replay surface is
# ``run_entry._RESUME_OVERLAY_EXCLUDED_DESTS``.
#
# This is the second of two layers, and it is deliberately not the only one:
# ``parse_command`` refuses a supervised command carrying
# ``--fresh-conversation`` outright, so no namespace reaching here should have
# it set. Dropping it here would otherwise be a *silent* discard of an explicit
# operator authorization, which is the failure mode the refusal exists to
# prevent; keeping the dest listed means a future caller that builds a
# namespace without going through ``parse_command`` still cannot replay it.
_CHILD_RUN_ARG_SKIP_DESTS = frozenset(
    {
        "dry_run",
        "fresh_conversation",
        "json",
        "max_review_fix_plans",
        "plan_dir",
        "reply",
        "reply_stdin",
    }
)


def _append_child_run_flag(
    argv: list[str], flag: _run_flags.RunFlag, value: Any
) -> None:
    """Serialize one parsed run flag back into child argv.

    The helper keeps the child command in shared-spec order, so adding an
    ordinary flag to ``RUN_FLAG_SPEC`` automatically forwards it here.
    """
    if value is None:
        return
    if flag.action == "append":
        for entry in value:
            argv.extend([flag.option_strings[0], str(entry)])
        return
    if flag.action == "store_true":
        if value:
            argv.append(flag.option_strings[0])
        return
    if flag.action == "store_false":
        if value is False:
            argv.append(flag.option_strings[0])
        return
    if flag.action == "store_const":
        if value == flag.const:
            argv.append(flag.option_strings[0])
        return
    argv.extend([flag.option_strings[0], str(value)])


def _build_child_run_args(
    namespace: argparse.Namespace,
    *,
    plan_dir: str,
    cwd: str | None,
    state_dir: str | None,
) -> list[str]:
    """Reconstruct the supervised ``asl run`` argv from the declarative spec."""
    argv = ["run", plan_dir]
    legacy_cycles = getattr(namespace, "max_review_fix_plans", None)
    for flag in _run_flags.RUN_FLAG_SPEC:
        if flag.positional or flag.dest in _CHILD_RUN_ARG_SKIP_DESTS:
            continue
        if flag.dest == "max_review_fix_cycles":
            value = getattr(namespace, flag.dest, None)
            if value is None:
                value = legacy_cycles
            if value is not None:
                argv.extend(["--max-review-fix-cycles", str(value)])
            continue
        if flag.dest == "cwd":
            value = cwd
        elif flag.dest == "state_dir":
            value = state_dir
        else:
            value = getattr(namespace, flag.dest, None)
        _append_child_run_flag(argv, flag, value)
    argv.append("--json")
    return argv


def _tokenize(command: str) -> list:
    """Split ``command`` into shell-like tokens, rejecting shell operators.

    Uses ``shlex`` with ``punctuation_chars`` so unquoted operators such as
    ``|``, ``&&``, ``;``, ``>`` become standalone tokens we can reject, while the
    same characters inside quotes stay part of their token (e.g. a ``--note``).
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def parse_command(command: str) -> dict:
    """Safely parse an ``asl run`` command string into a child-exec model.

    On success returns ``ok: True`` with the environment assignments, executable,
    reconstructed child ``argv`` (with ``--json`` appended when missing), and the
    resolved paths later sessions need to start and poll the run. On failure
    returns ``ok: False`` with a stable ``reason`` and human-readable ``error``.

    The reconstructed argv is canonical (flags emitted in a fixed order) rather
    than byte-identical to the input: the child semantics are preserved, tilde
    paths are expanded because no shell will do it, and ``--json`` is guaranteed
    so downstream sessions can rely on structured output.
    """
    try:
        tokens = _tokenize(command)
    except ValueError as exc:
        return _parse_error("shell-syntax", f"could not tokenize command: {exc}")

    if not tokens:
        return _parse_error("empty-command", "no command was provided to supervise")

    for token in tokens:
        if token and all(ch in _SHELL_OPERATOR_CHARS for ch in token):
            return _parse_error(
                "shell-syntax", f"unsupported shell operator: {token!r}"
            )
        if "`" in token or "$(" in token:
            return _parse_error(
                "shell-syntax",
                f"unsupported command substitution in token: {token!r}",
            )

    # Leading NAME=VALUE assignments precede the executable.
    env: dict = {}
    index = 0
    while index < len(tokens):
        match = _ENV_ASSIGNMENT_RE.match(tokens[index])
        if not match:
            break
        env[match.group(1)] = _expand_user_only(match.group(2))
        index += 1

    rest = tokens[index:]
    if not rest:
        return _parse_error(
            "missing-executable", "no `asl` executable found after env assignments"
        )

    executable = rest[0]
    if os.path.basename(executable) != "asl":
        return _parse_error(
            "not-asl",
            f"executable must be `asl` (or a path ending in /asl), got {executable!r}",
        )

    after_executable = rest[1:]
    if not after_executable:
        return _parse_error(
            "unsupported-subcommand", "missing subcommand; only `asl run` is supported"
        )
    if after_executable[0] != "run":
        return _parse_error(
            "unsupported-subcommand",
            f"only `asl run` is supported, got subcommand {after_executable[0]!r}",
        )

    run_tokens = after_executable[1:]
    parser = _build_run_parser()
    try:
        namespace = parser.parse_args(run_tokens)
    except _ParseError as exc:
        return _parse_error("invalid-argument", str(exc))

    if namespace.dry_run:
        return _parse_error(
            "unsupported-mode",
            "--dry-run is not supported; the helper supervises a real run",
        )
    if namespace.reply is not None or namespace.reply_stdin:
        return _parse_error(
            "unsupported-mode",
            "--reply/--reply-stdin are owned by the helper; do not pass them",
        )
    if getattr(namespace, "fresh_conversation", False):
        # One-use authorizations and a process that relaunches the same session
        # on every recoverable pause do not mix. Refusing here rather than
        # quietly dropping the flag is the point: a helper that accepted the
        # command and then stripped the authorization would either replay a
        # fresh start on each retry, or discard an operator's explicit decision
        # without saying so. The decision stays with the person who made it,
        # exactly as `asl control recover-conversation` prints it rather than
        # performing it.
        return _parse_error(
            "unsupported-mode",
            "--fresh-conversation is a one-use authorization to discard a "
            "saved conversation and cannot be supervised; run it yourself "
            "(`asl run PLAN_DIR --resume --fresh-conversation`), then "
            "supervise the plan",
        )

    # Mirror ASL's own numeric/harness validation so the helper fails fast with
    # the same guarantees instead of deferring to a child crash. The checks live
    # in ``agent_session_loop.run_flags`` — the same module the runner validates
    # against — so the helper and the runner can never disagree. On a ``--resume``
    # command the harness/model cross-field check is deferred to the child: the
    # helper validates the raw command before the persisted ``run_config`` is
    # overlaid, whereas the runner rehydrates the review harness first, so a
    # model-only resume override the runner accepts must not be rejected here.
    validation_errors = _run_flags.validate_run_namespace(
        namespace,
        defer_resume_dependent=bool(getattr(namespace, "resume", False)),
        default_max_review_fix_cycles=DEFAULT_MAX_REVIEW_FIX_CYCLES,
        default_max_review_fix_cycles_ceiling=(
            _run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES_CEILING
        ),
    )
    if validation_errors:
        return _parse_error("invalid-argument", validation_errors[0].message)

    plan_dir = _expand_user_only(namespace.plan_dir)
    cwd = _expand_user_only(namespace.cwd) if namespace.cwd is not None else None
    state_dir = (
        _expand_user_only(namespace.state_dir)
        if namespace.state_dir is not None
        else None
    )

    json_appended = not namespace.json
    child_args = _build_child_run_args(
        namespace,
        plan_dir=plan_dir,
        cwd=cwd,
        state_dir=state_dir,
    )

    return {
        "ok": True,
        "stage": PARSE_STAGE,
        "reason": "parsed",
        "env": env,
        "executable": executable,
        "run_args": child_args,
        "argv": [executable, *child_args],
        "json_appended": json_appended,
        "plan_dir": plan_dir,
        "cwd": cwd,
        "state_dir": state_dir,
        "harness": namespace.harness,
        "review_harness": namespace.review_harness,
        "no_mailbox": bool(namespace.no_mailbox),
        "max_review_fix_cycles": (
            namespace.max_review_fix_cycles
            if namespace.max_review_fix_cycles is not None
            else (
                namespace.max_review_fix_plans
                if namespace.max_review_fix_plans is not None
                else DEFAULT_MAX_REVIEW_FIX_CYCLES
            )
        ),
        # Whether the operator passed a repair cap explicitly (either the
        # canonical flag or the deprecated alias). On a `--resume` command an
        # implicit cap must defer to the persisted `run_config`, so the
        # supervisor keys off this to decide whether to prefer the persisted
        # value (see `_effective_review_config`).
        "max_review_fix_cycles_explicit": (
            namespace.max_review_fix_cycles is not None
            or namespace.max_review_fix_plans is not None
        ),
        # `--resume` reruns the plan against its persisted `run_config`; the
        # supervisor resolves the effective review harness/cap from that config
        # before an all-complete review-continuation decision.
        "resume": bool(getattr(namespace, "resume", False)),
        # The requested `--max-sessions` cap (or `None`). The supervisor treats
        # this as a *global* session budget for the whole run, not a per-chunk
        # cap: once this many implementation sessions have completed across the
        # supervision it stops cleanly with `reason: "max-sessions"`.
        "max_sessions": namespace.max_sessions,
    }


# Single source of truth for the review verdict grammar. The shared parser
# returns the same ``ok``/``outcome`` payload this helper always exposed; the
# runner enforces the same grammar (and the structured-artifact contract)
# through the same module.
parse_review_verdict = _review.parse_review_verdict


def _child_env(parsed: dict) -> dict:
    """Overlay the command's env assignments on the current environment.

    The helper runs the child without a shell, so the parsed ``NAME=VALUE``
    assignments have to be merged into ``os.environ`` here rather than being
    interpreted by a shell prefix.

    The invocation-origin marker is applied after that overlay, so a supervised
    command cannot relabel this helper's own children as direct operator calls.
    """
    env = dict(os.environ)
    env.update(parsed.get("env") or {})
    return _cli_trace.child_env(env, INVOCATION_ORIGIN)


def _canonical_path(path: str | None) -> str | None:
    """Best-effort real-path canonicalization for lock/plan-dir comparison."""
    if path is None:
        return None
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _status_argv(parsed: dict) -> list:
    """Build the ``asl status <plan> [--state-dir DIR] --json`` argv."""
    argv = [parsed["executable"], "status", parsed["plan_dir"]]
    if parsed.get("state_dir") is not None:
        argv += ["--state-dir", parsed["state_dir"]]
    argv.append("--json")
    return argv
