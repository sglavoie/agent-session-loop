"""Commit message cli."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

from . import (
    commit_message_diff as diff,
)
from . import (
    commit_message_format as format,
)
from . import (
    commit_message_generation as generation,
)
from . import (
    commit_message_git as git,
)
from . import (
    commit_message_models as models,
)
from . import (
    commit_message_output as output,
)
from .cli_parser_support import POSITIVE_INTEGER, flag_refusal


def _generation_timeout(args: argparse.Namespace) -> int:
    """The bound this invocation puts on its one message-generation turn.

    ``getattr`` rather than ``args.timeout`` so an in-process caller building
    its own namespace still gets the bound instead of an ``AttributeError``;
    the flag itself already defaults to it. A non-positive value is refused
    rather than quietly read as "no bound": ``run_harness`` would treat 0 as an
    immediate expiry and a negative number is not a bound at all.
    """
    timeout = getattr(args, "timeout", None)
    if timeout is None:
        return models.DEFAULT_GENERATION_TIMEOUT_SECONDS
    if int(timeout) <= 0:
        raise models.CommitMessageError(
            flag_refusal("--timeout", POSITIVE_INTEGER, timeout),
            reason=models.REASON_INVALID_ARGUMENTS,
        )
    return int(timeout)


def _commit_and_report(
    args: argparse.Namespace,
    repo_root: Path,
    message: str,
    *,
    mode: str,
    extra: dict[str, Any],
    snapshot: str | None = None,
) -> int:
    """Print the message, then commit it unless the caller stopped short.

    Stdout carries the message and nothing else, so ``asl commit --dry-run >
    msg.txt`` is a usable command; progress and outcome go to stderr.

    A scoped commit re-checks ``snapshot`` immediately before committing:
    message generation is a harness turn that can take minutes, and
    ``git commit --only`` would otherwise sweep in whatever the worktree holds
    by the time it runs.
    """
    from . import responses

    if not args.json:
        print(message)

    if args.dry_run:
        if args.json:
            responses.print_json(
                responses.success_payload(
                    models.COMMIT_COMMAND,
                    mode=mode,
                    committed=False,
                    dry_run=True,
                    repo_root=str(repo_root),
                    message=message,
                    **extra,
                )
            )
        return 0

    if not output._confirm(args, "Commit with this message?"):
        raise models.CommitMessageError(
            "commit declined", reason=models.REASON_CANCELLED
        )

    pathspec = output._commit_pathspec(args)
    if pathspec and snapshot is not None:
        git.assert_pathspec_snapshot(repo_root, pathspec, snapshot)
    committed = git.commit_staged(
        repo_root, message, verify=args.verify, pathspec=pathspec
    )
    if committed.returncode != 0:
        raise models.CommitMessageError(
            f"git commit failed: {(committed.stderr or committed.stdout).strip()}",
            reason=models.REASON_COMMIT_FAILED,
            details={"returncode": committed.returncode},
        )

    sha = git.head_sha(repo_root)
    if args.json:
        responses.print_json(
            responses.success_payload(
                models.COMMIT_COMMAND,
                mode=mode,
                committed=True,
                dry_run=False,
                repo_root=str(repo_root),
                commit=sha,
                message=message,
                **extra,
            )
        )
    else:
        print(f"asl commit: committed {sha[:12]}", file=sys.stderr)
    return 0


def _run_from_id(args: argparse.Namespace, repo_root: Path) -> int:
    """Describe an existing commit; never stage and never commit."""
    diff_result = git.capture_commit_diff(args.from_id, repo_root)
    output._reject_empty_diff(diff_result, repo_root, from_id=args.from_id)
    if args.prepare_only:
        return output._emit_prepare_only(args, diff_result)

    output._warn_if_truncated(args, diff_result)
    settings = generation.generation_settings(args.harness, args.model, args.effort)
    message = format.format_commit_message(
        generation.generate_commit_message(
            diff_result,
            repo_root=repo_root,
            harness=cast(str, settings["harness"]),
            guidance=args.guidance,
            model=args.model,
            effort=args.effort,
            timeout=_generation_timeout(args),
        )
    )
    return output._emit_message_only(args, message, diff_result, settings)


def _run_message_file(args: argparse.Namespace, repo_root: Path) -> int:
    """Commit an operator-supplied message; the one mode that needs no harness."""
    text = output._read_message_text(args.message_file)
    if not text.strip():
        raise models.CommitMessageError(
            "the supplied commit message is empty",
            reason=models.REASON_EMPTY_MESSAGE,
        )
    if not git.has_staged_changes(repo_root, output._commit_pathspec(args)):
        raise models.CommitMessageError(
            "no staged changes to commit",
            reason=models.REASON_NO_STAGED_CHANGES,
        )
    # An operator's message has no model preamble to cut, and its title need
    # not be conventional, so nothing above a later title-shaped line is lost.
    message = format.format_commit_message(text, extract=False)
    if not message.strip():
        raise models.CommitMessageError(
            "the supplied commit message is empty after formatting",
            reason=models.REASON_EMPTY_MESSAGE,
        )
    return _commit_and_report(
        args,
        repo_root,
        message,
        mode="message-file",
        extra={"message_file": args.message_file, "generated": False},
        snapshot=output._expected_snapshot(args),
    )


def _run_default(args: argparse.Namespace, repo_root: Path) -> int:
    """Capture, generate, format, and commit — the mode every flag defaults to."""
    pathspec = output._commit_pathspec(args)
    # Ahead of the capture rather than after it, so an index mutation that
    # races the capture itself reads as drift instead of being blessed by the
    # token that follows it.
    snapshot = git.pathspec_snapshot(repo_root, pathspec) if pathspec else None
    diff_result = git.capture_staged_diff(repo_root, pathspec)
    output._reject_empty_diff(diff_result, repo_root, pathspec=pathspec)
    if args.prepare_only:
        return output._emit_prepare_only(args, diff_result, snapshot)

    output._warn_if_truncated(args, diff_result)
    settings = generation.generation_settings(args.harness, args.model, args.effort)
    message = format.format_commit_message(
        generation.generate_commit_message(
            diff_result,
            repo_root=repo_root,
            harness=cast(str, settings["harness"]),
            guidance=args.guidance,
            model=args.model,
            effort=args.effort,
            timeout=_generation_timeout(args),
        )
    )
    if not message.strip():
        raise models.CommitMessageError(
            "the generated commit message is empty after formatting",
            reason=models.REASON_EMPTY_MESSAGE,
        )
    return _commit_and_report(
        args,
        repo_root,
        message,
        mode=models.PREPARE_MODE_COMMIT,
        extra={
            "generated": True,
            "is_truncated": diff_result.is_truncated,
            "truncation_notes": diff.build_truncation_notice(diff_result),
            **settings,
        },
        snapshot=snapshot,
    )


def _dispatch_commit(args: argparse.Namespace) -> int:
    output._reject_conflicting_modes(args)
    # Ahead of staging, so an unusable bound is refused before the invocation
    # has touched the index rather than after.
    _generation_timeout(args)
    repo_root = output._resolve_repo_root(args.path)

    if args.from_id:
        return _run_from_id(args, repo_root)

    expected = output._expected_snapshot(args)
    if expected is not None:
        # Ahead of staging, so a drifted path is reported without the index
        # first being moved onto content this invocation is about to refuse.
        git.assert_pathspec_snapshot(repo_root, list(args.pathspec), expected)

    output._stage_for_commit(args, repo_root)
    if args.message_file:
        return _run_message_file(args, repo_root)
    return _run_default(args, repo_root)


def commit_from_args(args: argparse.Namespace) -> int:
    """Run the ``asl commit`` subcommand from CLI arguments."""
    from . import responses

    try:
        return _dispatch_commit(args)
    except models.CommitMessageError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    models.COMMIT_COMMAND,
                    reason=error.reason,
                    error=str(error),
                    details=error.details,
                )
            )
        else:
            print(f"asl commit: {error}", file=sys.stderr)
        return 1
