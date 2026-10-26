"""Commit message output."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from . import (
    commit_message_diff as diff,
)
from . import (
    commit_message_git as git,
)
from . import (
    commit_message_models as models,
)

# ---------------------------------------------------------------------------
# `asl commit` handler
# ---------------------------------------------------------------------------


def _reject_conflicting_modes(args: argparse.Namespace) -> None:
    """Refuse flag combinations that ask for two different commands at once."""
    conflicts = (
        (args.from_id and args.staged, "--from-id and --staged"),
        (args.from_id and args.pathspec, "--from-id and --pathspec"),
        (args.from_id and args.message_file, "--from-id and --message-file"),
        (args.prepare_only and args.message_file, "--prepare-only and --message-file"),
        (args.staged and args.pathspec, "--staged and --pathspec"),
    )
    for conflicting, names in conflicts:
        if conflicting:
            raise models.CommitMessageError(
                f"{names} are mutually exclusive",
                reason=models.REASON_INVALID_ARGUMENTS,
            )
    if _expected_snapshot(args) and not args.pathspec:
        raise models.CommitMessageError(
            "--expect-snapshot names a scoped snapshot and needs --pathspec",
            reason=models.REASON_INVALID_ARGUMENTS,
        )


def _resolve_repo_root(path: str | None) -> Path:
    """Resolve the worktree ``asl commit`` operates on, refusing anything else."""
    start = Path(path).expanduser() if path else Path.cwd()
    if not start.exists():
        raise models.CommitMessageError(
            f"path does not exist: {start}",
            reason=models.REASON_INVALID_ARGUMENTS,
        )
    root = git.find_git_root(start)
    if root is None:
        raise models.CommitMessageError(
            f"not a git worktree: {start}",
            reason=models.REASON_NOT_A_WORKTREE,
        )
    return root


def _stage_for_commit(args: argparse.Namespace, repo_root: Path) -> None:
    """Bring the index to what this invocation intends to commit.

    ``--staged`` promises the index is already right and refuses to touch it;
    ``--pathspec`` stages exactly what it names and leaves every other change
    alone; the default stages everything, exactly as the skill does today.
    """
    if args.staged:
        if not git.has_staged_changes(repo_root):
            raise models.CommitMessageError(
                "no staged changes; stage something or drop --staged",
                reason=models.REASON_NO_STAGED_CHANGES,
            )
        return

    if args.pathspec:
        staged = git.stage_paths(repo_root, args.pathspec)
    else:
        if not git.has_changes(repo_root):
            raise models.CommitMessageError(
                "no changes to commit", reason=models.REASON_NO_CHANGES
            )
        staged = git.stage_all(repo_root)

    if staged.returncode != 0:
        raise models.CommitMessageError(
            f"git add failed: {staged.stderr.strip()}",
            reason=models.REASON_STAGE_FAILED,
        )


def _reject_empty_diff(
    diff_result: models.DiffResult,
    repo_root: Path,
    *,
    from_id: str | None = None,
    pathspec: Sequence[str] | None = None,
) -> None:
    """Refuse a diff with nothing to summarize, saying which kind of nothing."""
    if not diff_result.is_empty:
        return
    if git.changes_are_all_binary(repo_root, from_id=from_id, pathspec=pathspec):
        raise models.CommitMessageError(
            "all changed files are binary - no text diff to summarize",
            reason=models.REASON_ALL_BINARY,
        )
    raise models.CommitMessageError("diff is empty", reason=models.REASON_EMPTY_DIFF)


def _read_message_text(source: str) -> str:
    """Read ``--message-file``, treating ``-`` as stdin."""
    if source == "-":
        return sys.stdin.read()
    path = Path(source).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise models.CommitMessageError(
            f"cannot read message file {path}: {error}",
            reason=models.REASON_INVALID_ARGUMENTS,
        ) from error


def _is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except OSError, ValueError:
        return False


def _confirm(args: argparse.Namespace, question: str) -> bool:
    """Ask before committing, when there is a person there to ask.

    A piped or redirected invocation is a deliberate one and proceeds
    unprompted, which is what keeps the documented command shape scriptable
    and lets the runner drive it without a terminal.
    """
    if args.yes or args.json:
        return True
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        return True
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        # Ctrl-D at the prompt declines like any answer that is not yes; end
        # the prompt's line so the cancellation starts on its own.
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _prepare_header(
    args: argparse.Namespace,
    diff_result: models.DiffResult,
    snapshot: str | None = None,
) -> dict[str, Any]:
    """Build the ``--prepare-only`` header ``gitsummary-prepare.py`` used to emit.

    The key set and its order are the skill's parsing contract, and
    ``build_truncation_notice`` is the notice that contract carries — the
    prompt's looser ``build_truncation_note`` is a different string.
    ``snapshot`` names the staged content this header describes so the skill's
    second call can prove it is committing the same thing; it is ``None`` for
    every unscoped invocation, which commits the index it is handed.
    """
    return {
        "mode": models.PREPARE_MODE_FROM_ID
        if args.from_id
        else models.PREPARE_MODE_COMMIT,
        "yes": bool(args.yes),
        "no_verify": not args.verify,
        "guidance": args.guidance,
        "is_truncated": diff_result.is_truncated,
        "truncation_notes": diff.build_truncation_notice(diff_result),
        "from_id": args.from_id,
        "snapshot": snapshot,
    }


def _emit_prepare_only(
    args: argparse.Namespace,
    diff_result: models.DiffResult,
    snapshot: str | None = None,
) -> int:
    """Emit a single-line JSON header, a blank line, and the budgeted diff."""
    from . import responses

    header = _prepare_header(args, diff_result, snapshot)
    if args.json:
        responses.print_json(
            responses.success_payload(
                models.COMMIT_COMMAND,
                mode="prepare-only",
                committed=False,
                header=header,
                diff=diff_result.diff,
            )
        )
        return 0
    sys.stdout.write(json.dumps(header) + "\n\n")
    sys.stdout.write(diff_result.diff)
    return 0


def _warn_if_truncated(
    args: argparse.Namespace, diff_result: models.DiffResult
) -> None:
    if args.json or not diff_result.is_truncated:
        return
    notice = diff.build_truncation_notice(diff_result)
    if notice:
        print(
            f"asl commit: diff reduced to fit the prompt budget: {notice}",
            file=sys.stderr,
        )


def _emit_message_only(
    args: argparse.Namespace,
    message: str,
    diff_result: models.DiffResult,
    settings: dict[str, str | None],
) -> int:
    """Print a generated message without committing, as ``--from-id`` promises."""
    from . import responses

    if args.json:
        responses.print_json(
            responses.success_payload(
                models.COMMIT_COMMAND,
                mode=models.PREPARE_MODE_FROM_ID,
                committed=False,
                from_id=args.from_id,
                message=message,
                is_truncated=diff_result.is_truncated,
                truncation_notes=diff.build_truncation_notice(diff_result),
                **settings,
            )
        )
    else:
        print(message)
    return 0


def _commit_pathspec(args: argparse.Namespace) -> list[str] | None:
    """The paths this invocation owns, or ``None`` when it owns the index.

    ``--pathspec`` is the only mode that scopes the commit: ``--staged`` is
    mutually exclusive with it and promises the whole index is intended.
    """
    return list(args.pathspec) if args.pathspec else None


def _expected_snapshot(args: argparse.Namespace) -> str | None:
    """The snapshot a two-step invocation was told to commit, if any.

    ``getattr`` so an in-process caller assembling its own namespace need not
    know about the flag.
    """
    expected = getattr(args, "expect_snapshot", None)
    return cast(str | None, expected) or None
