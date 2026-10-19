"""Diff capture, prompt construction, generation, and the ``asl commit`` handler.

The diff half was ported from the retired
``skills/gitsummary/scripts/gitsummary-prepare.py`` (itself a stdlib port of the
retired spec CLI's diff service).  The budget arithmetic came across verbatim,
so the truncated diff this module inlines into a prompt stays byte-for-byte
comparable with what that script produced.  The message half was ported from the
retired ``skills/gitsummary/scripts/format-commit.py``, plus the API-error guard
that copy never had.  The ``gitsummary`` skill now calls this module through
``asl commit`` rather than carrying its own copy.

The git surface rests only on :mod:`agent_session_loop.git_subprocess`, which
imports nothing but the standard library, on purpose: ``asl commit`` runs
standalone, outside a loop, so it must not pull the runner import graph in
behind it.  The same rule governs the ASL imports the generation half needs
— ``harnesses``, ``config``, ``harness_failures``, ``retry_hints``, and
``responses`` are imported inside the functions that use them, so importing
this module still costs nothing but the standard library.


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .commit_message_cli import (
    _commit_and_report as _commit_and_report,
)
from .commit_message_cli import (
    _dispatch_commit as _dispatch_commit,
)
from .commit_message_cli import (
    _generation_timeout as _generation_timeout,
)
from .commit_message_cli import (
    _run_default as _run_default,
)
from .commit_message_cli import (
    _run_from_id as _run_from_id,
)
from .commit_message_cli import (
    _run_message_file as _run_message_file,
)
from .commit_message_cli import (
    commit_from_args as commit_from_args,
)
from .commit_message_diff import (
    _capped_file_list as _capped_file_list,
)
from .commit_message_diff import (
    _compute_file_budgets as _compute_file_budgets,
)
from .commit_message_diff import (
    _create_binary_summary as _create_binary_summary,
)
from .commit_message_diff import (
    _create_stat_line as _create_stat_line,
)
from .commit_message_diff import (
    _parse_diff_into_files as _parse_diff_into_files,
)
from .commit_message_diff import (
    _priority_score as _priority_score,
)
from .commit_message_diff import (
    _truncate_file_diff as _truncate_file_diff,
)
from .commit_message_diff import (
    build_truncation_notice as build_truncation_notice,
)
from .commit_message_diff import (
    format_size as format_size,
)
from .commit_message_diff import (
    process_raw_diff as process_raw_diff,
)
from .commit_message_format import (
    _is_disallowed_trailer as _is_disallowed_trailer,
)
from .commit_message_format import (
    build_commit_prompt as build_commit_prompt,
)
from .commit_message_format import (
    build_truncation_note as build_truncation_note,
)
from .commit_message_format import (
    detect_line_type as detect_line_type,
)
from .commit_message_format import (
    extract_commit_message as extract_commit_message,
)
from .commit_message_format import (
    format_bullet as format_bullet,
)
from .commit_message_format import (
    format_commit_message as format_commit_message,
)
from .commit_message_format import (
    format_paragraph as format_paragraph,
)
from .commit_message_format import (
    looks_like_api_error as looks_like_api_error,
)
from .commit_message_format import (
    strip_disallowed_trailers as strip_disallowed_trailers,
)
from .commit_message_generation import (
    _classified_harness_error as _classified_harness_error,
)
from .commit_message_generation import (
    _generation_timeout_error as _generation_timeout_error,
)
from .commit_message_generation import (
    generate_commit_message as generate_commit_message,
)
from .commit_message_generation import (
    generation_settings as generation_settings,
)
from .commit_message_generation import (
    resolve_harness as resolve_harness,
)
from .commit_message_git import (
    _build_scoped_index as _build_scoped_index,
)
from .commit_message_git import (
    _deletion_already_staged as _deletion_already_staged,
)
from .commit_message_git import (
    _git as _git,
)
from .commit_message_git import (
    _paths_staged_for_deletion as _paths_staged_for_deletion,
)
from .commit_message_git import (
    _pathspec_args as _pathspec_args,
)
from .commit_message_git import (
    _staged_index_entries as _staged_index_entries,
)
from .commit_message_git import (
    assert_pathspec_snapshot as assert_pathspec_snapshot,
)
from .commit_message_git import (
    capture_commit_diff as capture_commit_diff,
)
from .commit_message_git import (
    capture_staged_diff as capture_staged_diff,
)
from .commit_message_git import (
    changes_are_all_binary as changes_are_all_binary,
)
from .commit_message_git import (
    commit_staged as commit_staged,
)
from .commit_message_git import (
    dequalify_paths as dequalify_paths,
)
from .commit_message_git import (
    find_git_root as find_git_root,
)
from .commit_message_git import (
    has_changes as has_changes,
)
from .commit_message_git import (
    has_staged_changes as has_staged_changes,
)
from .commit_message_git import (
    head_sha as head_sha,
)
from .commit_message_git import (
    pathspec_snapshot as pathspec_snapshot,
)
from .commit_message_git import (
    stage_all as stage_all,
)
from .commit_message_git import (
    stage_paths as stage_paths,
)
from .commit_message_git import (
    worktree_drifted_paths as worktree_drifted_paths,
)
from .commit_message_models import (
    _BINARY_FILES_PATTERN as _BINARY_FILES_PATTERN,
)
from .commit_message_models import (
    _BULLET_PATTERN as _BULLET_PATTERN,
)
from .commit_message_models import (
    _DISALLOWED_TRAILERS as _DISALLOWED_TRAILERS,
)
from .commit_message_models import (
    _FILE_HEADER_PATTERN as _FILE_HEADER_PATTERN,
)
from .commit_message_models import (
    _GIT_BINARY_PATCH_PATTERN as _GIT_BINARY_PATCH_PATTERN,
)
from .commit_message_models import (
    _HUNK_PATTERN as _HUNK_PATTERN,
)
from .commit_message_models import (
    _LEADING_INDENT_PATTERN as _LEADING_INDENT_PATTERN,
)
from .commit_message_models import (
    _NOTICE_NAME_LIMIT as _NOTICE_NAME_LIMIT,
)
from .commit_message_models import (
    _PLUS_PATH_PATTERN as _PLUS_PATH_PATTERN,
)
from .commit_message_models import (
    _PROMPT_NAME_LIMIT as _PROMPT_NAME_LIMIT,
)
from .commit_message_models import (
    _TRUNCATION_RESERVE as _TRUNCATION_RESERVE,
)
from .commit_message_models import (
    BINARY_SUMMARY_OVERHEAD as BINARY_SUMMARY_OVERHEAD,
)
from .commit_message_models import (
    COMMIT_COMMAND as COMMIT_COMMAND,
)
from .commit_message_models import (
    COMMIT_TITLE_PATTERN as COMMIT_TITLE_PATTERN,
)
from .commit_message_models import (
    DEFAULT_GENERATION_TIMEOUT_SECONDS as DEFAULT_GENERATION_TIMEOUT_SECONDS,
)
from .commit_message_models import (
    MAX_FILE_DIFF_SIZE as MAX_FILE_DIFF_SIZE,
)
from .commit_message_models import (
    MAX_TOTAL_DIFF_SIZE as MAX_TOTAL_DIFF_SIZE,
)
from .commit_message_models import (
    PREPARE_MODE_COMMIT as PREPARE_MODE_COMMIT,
)
from .commit_message_models import (
    PREPARE_MODE_FROM_ID as PREPARE_MODE_FROM_ID,
)
from .commit_message_models import (
    REASON_ALL_BINARY as REASON_ALL_BINARY,
)
from .commit_message_models import (
    REASON_API_ERROR as REASON_API_ERROR,
)
from .commit_message_models import (
    REASON_CANCELLED as REASON_CANCELLED,
)
from .commit_message_models import (
    REASON_COMMIT_FAILED as REASON_COMMIT_FAILED,
)
from .commit_message_models import (
    REASON_COMMIT_NOT_FOUND as REASON_COMMIT_NOT_FOUND,
)
from .commit_message_models import (
    REASON_DIFF_FAILED as REASON_DIFF_FAILED,
)
from .commit_message_models import (
    REASON_EMPTY_DIFF as REASON_EMPTY_DIFF,
)
from .commit_message_models import (
    REASON_EMPTY_MESSAGE as REASON_EMPTY_MESSAGE,
)
from .commit_message_models import (
    REASON_FAILED as REASON_FAILED,
)
from .commit_message_models import (
    REASON_GENERATION_FAILED as REASON_GENERATION_FAILED,
)
from .commit_message_models import (
    REASON_GENERATION_TIMEOUT as REASON_GENERATION_TIMEOUT,
)
from .commit_message_models import (
    REASON_INVALID_ARGUMENTS as REASON_INVALID_ARGUMENTS,
)
from .commit_message_models import (
    REASON_NO_CHANGES as REASON_NO_CHANGES,
)
from .commit_message_models import (
    REASON_NO_HARNESS as REASON_NO_HARNESS,
)
from .commit_message_models import (
    REASON_NO_STAGED_CHANGES as REASON_NO_STAGED_CHANGES,
)
from .commit_message_models import (
    REASON_NOT_A_WORKTREE as REASON_NOT_A_WORKTREE,
)
from .commit_message_models import (
    REASON_SNAPSHOT_DRIFTED as REASON_SNAPSHOT_DRIFTED,
)
from .commit_message_models import (
    REASON_STAGE_FAILED as REASON_STAGE_FAILED,
)
from .commit_message_models import (
    SNAPSHOT_TOKEN_LENGTH as SNAPSHOT_TOKEN_LENGTH,
)
from .commit_message_models import (
    CommitMessageError as CommitMessageError,
)
from .commit_message_models import (
    DiffResult as DiffResult,
)
from .commit_message_models import (
    FileDiff as FileDiff,
)
from .commit_message_output import (
    _commit_pathspec as _commit_pathspec,
)
from .commit_message_output import (
    _confirm as _confirm,
)
from .commit_message_output import (
    _emit_message_only as _emit_message_only,
)
from .commit_message_output import (
    _emit_prepare_only as _emit_prepare_only,
)
from .commit_message_output import (
    _expected_snapshot as _expected_snapshot,
)
from .commit_message_output import (
    _is_tty as _is_tty,
)
from .commit_message_output import (
    _prepare_header as _prepare_header,
)
from .commit_message_output import (
    _read_message_text as _read_message_text,
)
from .commit_message_output import (
    _reject_conflicting_modes as _reject_conflicting_modes,
)
from .commit_message_output import (
    _reject_empty_diff as _reject_empty_diff,
)
from .commit_message_output import (
    _resolve_repo_root as _resolve_repo_root,
)
from .commit_message_output import (
    _stage_for_commit as _stage_for_commit,
)
from .commit_message_output import (
    _warn_if_truncated as _warn_if_truncated,
)
