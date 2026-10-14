"""Diff, prompt, and message-formatting tests for `asl commit`.

The formatting expectations are golden strings rather than a live comparison
against `skills/gitsummary/scripts/format-commit.py`: that script is gone now
that the skill drives `asl commit`, so the parity it used to guarantee is
pinned here instead of imported from there.
"""

from __future__ import annotations

import inspect
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_session_loop import cli, commit_message, config
from tests.support import (
    GENERATION_TIMEOUT_PROBE_SECONDS,
    git_stdout,
    init_git_repo,
    install_fake_codex,
    patched_env,
    wait_for_process_exit,
)


def build_hunk(index: int, *, lines: int = 6) -> str:
    """One unified-diff hunk with a stable, greppable body."""
    body = "".join(
        f"+hunk {index} line {line} {'x' * 40}\n" for line in range(1, lines + 1)
    )
    return f"@@ -{index * 10},3 +{index * 10},{lines} @@ context {index}\n{body}"


def build_file_diff(path: str, *, hunks: int = 1, lines: int = 6) -> str:
    """A single file's diff section with ``hunks`` hunks."""
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
    )
    return header + "".join(build_hunk(i, lines=lines) for i in range(1, hunks + 1))


def build_binary_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"Binary files a/{path} and b/{path} differ\n"
    )


class DiffParsingTests(unittest.TestCase):
    def test_parses_each_file_section_with_its_line_counts(self) -> None:
        raw = build_file_diff("src/alpha.py", hunks=1, lines=3) + (
            "diff --git a/src/beta.py b/src/beta.py\n"
            "index 3333333..4444444 100644\n"
            "--- a/src/beta.py\n"
            "+++ b/src/beta.py\n"
            "@@ -1,2 +1,1 @@\n"
            "-removed one\n"
            "-removed two\n"
            "+added one\n"
        )

        files = commit_message._parse_diff_into_files(raw)

        self.assertEqual([f.path for f in files], ["src/alpha.py", "src/beta.py"])
        self.assertEqual(files[0].additions, 3)
        self.assertEqual(files[0].deletions, 0)
        self.assertEqual(files[1].additions, 1)
        self.assertEqual(files[1].deletions, 2)
        self.assertTrue(all(f.original_size > 0 for f in files))
        self.assertFalse(any(f.is_binary for f in files))
        # Each section carries its own header, so the sections partition the diff.
        self.assertEqual(sum(len(f.content) for f in files), len(raw))

    def test_parses_empty_and_headerless_diffs_as_no_files(self) -> None:
        self.assertEqual(commit_message._parse_diff_into_files(""), [])
        self.assertEqual(commit_message._parse_diff_into_files("   \n\n"), [])
        self.assertEqual(commit_message._parse_diff_into_files("not a diff\n"), [])

    def test_classifies_both_binary_markers_and_skips_line_counting(self) -> None:
        raw = build_binary_diff("assets/logo.png") + (
            "diff --git a/assets/icon.ico b/assets/icon.ico\n"
            "index 5555555..6666666 100644\n"
            "GIT binary patch\n"
            "literal 12\n"
            "+not a real addition\n"
        )

        files = commit_message._parse_diff_into_files(raw)

        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.is_binary for f in files))
        self.assertEqual([f.additions for f in files], [0, 0])
        self.assertEqual([f.deletions for f in files], [0, 0])


class TruncationTests(unittest.TestCase):
    def test_truncation_cuts_at_a_hunk_boundary_and_reports_the_remainder(
        self,
    ) -> None:
        raw = build_file_diff("src/wide.py", hunks=4, lines=6)
        parsed = commit_message._parse_diff_into_files(raw)[0]
        hunk_size = len(build_hunk(1, lines=6).encode("utf-8"))
        header_size = parsed.original_size - 4 * hunk_size
        # Room for two hunks plus the reserve, but not a third.
        max_size = header_size + 2 * hunk_size + commit_message._TRUNCATION_RESERVE + 1

        truncated = commit_message._truncate_file_diff(parsed, max_size)

        self.assertTrue(truncated.is_truncated)
        self.assertEqual(truncated.original_size, parsed.original_size)
        self.assertIn("hunk 1 line 1", truncated.content)
        self.assertIn("hunk 2 line 6", truncated.content)
        self.assertNotIn("hunk 3 line 1", truncated.content)
        self.assertNotIn("hunk 4 line 1", truncated.content)
        # The cut lands on the boundary: the third hunk's header is absent too.
        self.assertNotIn("context 3", truncated.content)
        self.assertIn("[...2 more hunks: +12, -0 lines...]", truncated.content)

    def test_content_within_budget_is_returned_untouched(self) -> None:
        parsed = commit_message._parse_diff_into_files(
            build_file_diff("src/small.py", hunks=1, lines=2)
        )[0]

        result = commit_message._truncate_file_diff(parsed, parsed.original_size)

        self.assertIs(result, parsed)
        self.assertFalse(result.is_truncated)

    def test_hunkless_content_falls_back_to_a_flat_slice(self) -> None:
        parsed = commit_message.FileDiff(
            path="src/blob.py",
            content="diff --git a/src/blob.py b/src/blob.py\n" + "y" * 500,
            original_size=538,
        )

        result = commit_message._truncate_file_diff(parsed, 100)

        self.assertTrue(result.is_truncated)
        self.assertTrue(result.content.endswith("\n[...truncated...]\n"))
        self.assertEqual(len(result.content), 100 + len("\n[...truncated...]\n"))


class BudgetAllocationTests(unittest.TestCase):
    def test_budget_constants_match_the_ported_source(self) -> None:
        self.assertEqual(commit_message.MAX_TOTAL_DIFF_SIZE, 150 * 1024)
        self.assertEqual(commit_message.MAX_FILE_DIFF_SIZE, 30 * 1024)

    def test_fair_share_cap_and_surplus_redistribution(self) -> None:
        small = commit_message.FileDiff(path="a.py", content="", original_size=100)
        large_one = commit_message.FileDiff(
            path="b.py", content="", original_size=90000
        )
        large_two = commit_message.FileDiff(
            path="c.py", content="", original_size=90000
        )
        binary = commit_message.FileDiff(
            path="d.png", content="", is_binary=True, original_size=4000
        )

        budgets = commit_message._compute_file_budgets(
            [small, large_one, large_two, binary]
        )

        text_budget = commit_message.MAX_TOTAL_DIFF_SIZE - (
            1 * commit_message.BINARY_SUMMARY_OVERHEAD
        )
        cap = min(commit_message.MAX_FILE_DIFF_SIZE, text_budget // 3)
        self.assertNotIn("d.png", budgets)
        self.assertEqual(budgets["a.py"], cap)
        # The small file's unused share is split between the two large files.
        expected_extra = (cap - 100) // 2
        expected_large = min(commit_message.MAX_FILE_DIFF_SIZE, cap + expected_extra)
        self.assertEqual(budgets["b.py"], expected_large)
        self.assertEqual(budgets["c.py"], expected_large)

    def test_binary_only_diff_allocates_no_text_budget(self) -> None:
        binary = commit_message.FileDiff(path="x.png", content="", is_binary=True)

        self.assertEqual(commit_message._compute_file_budgets([binary]), {})


class ProcessRawDiffTests(unittest.TestCase):
    def test_empty_diff_reports_is_empty(self) -> None:
        for raw in ("", "   \n", "no diff headers here\n"):
            with self.subTest(raw=raw):
                result = commit_message.process_raw_diff(raw)
                self.assertTrue(result.is_empty)
                self.assertEqual(result.diff, "")
                self.assertEqual(result.total_size, 0)
                self.assertFalse(result.is_truncated)
                self.assertEqual(result.files_included, [])

    def test_small_diff_is_passed_through_whole(self) -> None:
        raw = build_file_diff("src/alpha.py", hunks=1, lines=2) + build_file_diff(
            "src/beta.py", hunks=1, lines=2
        )

        result = commit_message.process_raw_diff(raw)

        self.assertFalse(result.is_empty)
        self.assertFalse(result.is_truncated)
        self.assertEqual(sorted(result.files_included), ["src/alpha.py", "src/beta.py"])
        self.assertEqual(result.omitted_files, [])
        self.assertEqual(result.truncated_files, [])
        self.assertEqual(result.total_size, len(result.diff.encode("utf-8")))
        self.assertIn("hunk 1 line 1", result.diff)

    def test_binary_files_are_summarized_rather_than_inlined(self) -> None:
        raw = build_file_diff("src/alpha.py", hunks=1, lines=2) + build_binary_diff(
            "assets/logo.png"
        )

        result = commit_message.process_raw_diff(raw)

        self.assertEqual(result.binary_files, ["assets/logo.png"])
        self.assertEqual(result.files_included, ["src/alpha.py"])
        self.assertTrue(result.is_truncated)
        self.assertIn(
            "diff --git a/assets/logo.png b/assets/logo.png\n[Binary file]\n",
            result.diff,
        )
        self.assertNotIn("Binary files a/assets/logo.png", result.diff)

    def test_oversized_file_is_truncated_and_recorded(self) -> None:
        raw = build_file_diff("src/huge.py", hunks=900, lines=6)
        self.assertGreater(
            len(raw.encode("utf-8")), commit_message.MAX_FILE_DIFF_SIZE * 4
        )

        result = commit_message.process_raw_diff(raw)

        self.assertEqual(result.truncated_files, ["src/huge.py"])
        self.assertTrue(result.is_truncated)
        self.assertLessEqual(result.total_size, commit_message.MAX_FILE_DIFF_SIZE)
        self.assertIn("more hunks:", result.diff)

    def test_total_budget_overflow_demotes_files_to_stat_only(self) -> None:
        # Five oversized text files consume the whole per-file budget; the
        # binary summaries then overrun the flat 50-byte-per-binary
        # reservation, so the last text files fall back to stat lines.
        long_path = "vendor/" + "d" * 140
        raw = "".join(
            build_file_diff(f"src/module_{index}.py", hunks=900, lines=6)
            for index in range(5)
        ) + "".join(
            build_binary_diff(f"{long_path}/{index}.bin") for index in range(30)
        )

        result = commit_message.process_raw_diff(raw)

        self.assertTrue(result.is_truncated)
        self.assertTrue(result.omitted_files)
        self.assertEqual(len(result.binary_files), 30)
        self.assertEqual(
            len(result.files_included) + len(result.omitted_files),
            5,
        )
        self.assertLessEqual(result.total_size, commit_message.MAX_TOTAL_DIFF_SIZE)
        self.assertIn(
            "# Stat-only summaries (diff too large for full content):", result.diff
        )
        for path in result.omitted_files:
            self.assertIn(f"{path} | +", result.diff)


class ReportingHelperTests(unittest.TestCase):
    def test_format_size_renders_bytes_kilobytes_and_megabytes(self) -> None:
        self.assertEqual(commit_message.format_size(0), "0 B")
        self.assertEqual(commit_message.format_size(1023), "1023 B")
        self.assertEqual(commit_message.format_size(1024), "1.0 KB")
        self.assertEqual(commit_message.format_size(1536), "1.5 KB")
        self.assertEqual(commit_message.format_size(150 * 1024), "150.0 KB")
        self.assertEqual(commit_message.format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(commit_message.format_size(3 * 1024 * 1024), "3.0 MB")

    def test_truncation_notice_is_empty_when_nothing_was_reduced(self) -> None:
        result = commit_message.DiffResult(diff="x", files_included=["a.py"])

        self.assertEqual(commit_message.build_truncation_notice(result), "")

    def test_truncation_notice_counts_each_reduction_kind(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            binary_files=["a.png", "b.png"],
            truncated_files=["big.py"],
            omitted_files=["c.py", "d.py", "e.py", "f.py", "g.py"],
            is_truncated=True,
        )

        notice = commit_message.build_truncation_notice(result)

        self.assertEqual(
            notice,
            "2 binary file(s) summarized: a.png, b.png; "
            "1 large file(s) truncated: big.py; "
            "5 file(s) reduced to stat summary: c.py, d.py, e.py, ... (+2 more)",
        )


class DequalifyPathsTests(unittest.TestCase):
    def test_strips_alias_and_legacy_repository_prefixes(self) -> None:
        self.assertEqual(
            commit_message.dequalify_paths(
                ["SVC:src/app.py", ".:README.md", "OWL:nested/dir/file.txt"]
            ),
            ["src/app.py", "README.md", "nested/dir/file.txt"],
        )

    def test_splits_on_the_first_colon_only(self) -> None:
        self.assertEqual(
            commit_message.dequalify_paths(["SVC:weird:name.txt"]),
            ["weird:name.txt"],
        )

    def test_drops_empty_remainders_and_deduplicates(self) -> None:
        self.assertEqual(
            commit_message.dequalify_paths(
                ["SVC:a.py", ".:a.py", "SVC:", "", "OWL:b.py"]
            ),
            ["a.py", "b.py"],
        )

    def test_unqualified_entries_pass_through(self) -> None:
        self.assertEqual(commit_message.dequalify_paths(["plain.py"]), ["plain.py"])


class GitSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        init_git_repo(self.repo)

    def write(self, name: str, text: str) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def staged_names(self) -> list[str]:
        listing = git_stdout(self.repo, "diff", "--cached", "--name-only")
        return sorted(listing.splitlines()) if listing else []

    def test_find_git_root_walks_upward_from_a_nested_path(self) -> None:
        nested = self.repo / "a" / "b" / "c"
        nested.mkdir(parents=True)

        self.assertEqual(commit_message.find_git_root(nested), self.repo.resolve())
        self.assertEqual(commit_message.find_git_root(self.repo), self.repo.resolve())

    def test_find_git_root_returns_none_outside_a_worktree(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()

        # The temp dir itself is not a worktree; only ``repo`` is.
        self.assertIsNone(commit_message.find_git_root(outside))

    def test_change_predicates_track_the_worktree_and_the_index(self) -> None:
        self.assertFalse(commit_message.has_changes(self.repo))
        self.assertFalse(commit_message.has_staged_changes(self.repo))

        self.write("new.txt", "hello\n")
        self.assertTrue(commit_message.has_changes(self.repo))
        self.assertFalse(commit_message.has_staged_changes(self.repo))

        commit_message.stage_all(self.repo)
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_stage_all_stages_every_change(self) -> None:
        self.write("one.txt", "one\n")
        self.write("nested/two.txt", "two\n")

        result = commit_message.stage_all(self.repo)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.staged_names(), ["nested/two.txt", "one.txt"])

    def test_stage_paths_stages_only_the_named_files(self) -> None:
        self.write("keep.txt", "keep\n")
        self.write("also.txt", "also\n")
        self.write("untouched.txt", "untouched\n")

        result = commit_message.stage_paths(self.repo, ["keep.txt", "also.txt"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.staged_names(), ["also.txt", "keep.txt"])
        self.assertIn("untouched.txt", git_stdout(self.repo, "status", "--porcelain"))

    def test_stage_paths_accepts_a_deletion_already_staged(self) -> None:
        self.write("gone.txt", "gone\n")
        self.write("worktree_gone.txt", "gone too\n")
        commit_message.stage_all(self.repo)
        git_stdout(self.repo, "commit", "-qm", "seed")
        git_stdout(self.repo, "rm", "-q", "gone.txt")
        (self.repo / "worktree_gone.txt").unlink()
        self.write("changed.txt", "changed\n")

        result = commit_message.stage_paths(
            self.repo, ["gone.txt", "worktree_gone.txt", "changed.txt"]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.staged_names(), ["changed.txt", "gone.txt", "worktree_gone.txt"]
        )

    def test_stage_paths_still_rejects_an_unknown_path(self) -> None:
        result = commit_message.stage_paths(self.repo, ["never-existed.txt"])

        self.assertNotEqual(result.returncode, 0)

    def test_stage_paths_with_no_paths_stages_nothing(self) -> None:
        self.write("keep.txt", "keep\n")

        result = commit_message.stage_paths(self.repo, [])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.staged_names(), [])

    def test_capture_staged_diff_reads_the_index(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        commit_message.stage_all(self.repo)

        result = commit_message.capture_staged_diff(self.repo)

        self.assertFalse(result.is_empty)
        self.assertEqual(result.files_included, ["src/alpha.py"])
        self.assertIn("+print('alpha')", result.diff)
        self.assertEqual(result.total_size, len(result.diff.encode("utf-8")))

    def test_capture_staged_diff_on_a_clean_index_is_empty(self) -> None:
        result = commit_message.capture_staged_diff(self.repo)

        self.assertTrue(result.is_empty)
        self.assertEqual(result.diff, "")

    def test_capture_commit_diff_resolves_a_short_sha(self) -> None:
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_all(self.repo)
        git_stdout(self.repo, "commit", "-m", "add beta", "--no-verify")
        short_sha = git_stdout(self.repo, "rev-parse", "--short", "HEAD")

        result = commit_message.capture_commit_diff(short_sha, self.repo)

        self.assertEqual(result.files_included, ["src/beta.py"])
        self.assertIn("+print('beta')", result.diff)

    def test_capture_commit_diff_rejects_an_unresolvable_sha(self) -> None:
        with self.assertRaises(commit_message.CommitMessageError) as caught:
            commit_message.capture_commit_diff("deadbee", self.repo)

        self.assertIn("commit not found", str(caught.exception))

    def test_git_helper_reports_a_missing_executable_without_raising(self) -> None:
        result = commit_message._git(self.repo, ["definitely-not-a-git-command"])

        self.assertNotEqual(result.returncode, 0)


class ApiErrorGuardTests(unittest.TestCase):
    def test_api_error_prefix_is_rejected(self) -> None:
        self.assertTrue(commit_message.looks_like_api_error("API Error: 500"))
        self.assertTrue(
            commit_message.looks_like_api_error(
                '  API Error: 500 {"type":"error","error":{"type":"api_error"}}\n'
            )
        )

    def test_bare_error_object_is_rejected(self) -> None:
        self.assertTrue(commit_message.looks_like_api_error('{"type": "error"}'))
        self.assertTrue(
            commit_message.looks_like_api_error(
                'Request failed: {"type": "error", "error": {"message": "overloaded"}}'
            )
        )

    def test_a_normal_message_containing_braces_is_accepted(self) -> None:
        message = (
            "fix: escape the {placeholder} braces in the template\n"
            "\n"
            'The renderer choked on {"type": "config"} literals in the body.\n'
        )

        self.assertFalse(commit_message.looks_like_api_error(message))

    def test_non_error_json_and_unparseable_braces_are_accepted(self) -> None:
        self.assertFalse(commit_message.looks_like_api_error('{"type": "result"}'))
        self.assertFalse(commit_message.looks_like_api_error("feat: add {"))
        self.assertFalse(commit_message.looks_like_api_error(""))


class TrailerStrippingTests(unittest.TestCase):
    def test_attribution_trailers_are_dropped_with_their_blank_lines(self) -> None:
        text = (
            "fix: correct the retry ceiling\n"
            "\n"
            "body\n"
            "\n"
            "Co-authored-by: Someone <someone@example.com>\n"
            "Signed-off-by: Someone Else <else@example.com>\n"
        )

        self.assertEqual(
            commit_message.strip_disallowed_trailers(text),
            "fix: correct the retry ceiling\n\nbody",
        )

    def test_trailers_match_case_insensitively_and_through_indentation(self) -> None:
        text = "fix: x\n\n  co-authored-by: A <a@example.com>\n\tSIGNED-OFF-BY: B\n"

        self.assertEqual(commit_message.strip_disallowed_trailers(text), "fix: x")

    def test_a_body_line_merely_mentioning_a_trailer_survives(self) -> None:
        text = "fix: x\n\nDrop the Co-authored-by handling from the formatter path.\n"

        self.assertEqual(
            commit_message.strip_disallowed_trailers(text),
            "fix: x\n\nDrop the Co-authored-by handling from the formatter path.",
        )


class ExtractCommitMessageTests(unittest.TestCase):
    def test_preamble_before_the_first_title_is_stripped(self) -> None:
        text = (
            "I've read the diff and here is the message.\n"
            "\n"
            "Here goes:\n"
            "\n"
            "feat: add the native commit generator\n"
            "\n"
            "body\n"
        )

        self.assertEqual(
            commit_message.extract_commit_message(text),
            "feat: add the native commit generator\n\nbody",
        )

    def test_scoped_and_breaking_titles_are_recognized(self) -> None:
        for title in ("fix(cli): x", "feat!: x", "refactor(a/b)!: x"):
            with self.subTest(title=title):
                self.assertEqual(
                    commit_message.extract_commit_message(f"preamble\n{title}\n"),
                    title,
                )

    def test_text_without_a_title_falls_back_to_the_whole_response(self) -> None:
        text = "no conventional title anywhere\nsecond line\n"

        self.assertEqual(
            commit_message.extract_commit_message(text),
            "no conventional title anywhere\nsecond line",
        )

    def test_the_fallback_still_strips_trailers(self) -> None:
        text = "no title here\n\nCo-authored-by: A <a@example.com>\n"

        self.assertEqual(commit_message.extract_commit_message(text), "no title here")


class LineFormattingTests(unittest.TestCase):
    def test_detect_line_type_classifies_each_shape(self) -> None:
        self.assertEqual(commit_message.detect_line_type("   "), ("blank", "   "))
        self.assertEqual(
            commit_message.detect_line_type("plain prose"), ("paragraph", "plain prose")
        )
        for line, expected in (
            ("- dash", ("", "-", "dash")),
            ("  * star", ("  ", "*", "star")),
            ("• unicode", ("", "•", "unicode")),
            ("  10. numbered", ("  ", "10.", "numbered")),
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    commit_message.detect_line_type(line), ("bullet", expected)
                )

    def test_a_marker_without_trailing_space_is_not_a_bullet(self) -> None:
        self.assertEqual(
            commit_message.detect_line_type("-nospace"), ("paragraph", "-nospace")
        )

    def test_format_bullet_hangs_continuations_under_the_text_column(self) -> None:
        formatted = commit_message.format_bullet(
            "  ", "-", "alpha " * 30, width=40
        ).split("\n")

        self.assertTrue(formatted[0].startswith("  - alpha"))
        # "  - " is four columns wide, so every continuation starts at column 4.
        for line in formatted[1:]:
            self.assertTrue(line.startswith("    alpha"), line)
        self.assertTrue(all(len(line) <= 40 for line in formatted))

    def test_format_bullet_aligns_wider_markers_to_their_own_width(self) -> None:
        formatted = commit_message.format_bullet(
            "", "10.", "beta " * 20, width=40
        ).split("\n")

        self.assertTrue(formatted[0].startswith("10. beta"))
        for line in formatted[1:]:
            self.assertTrue(line.startswith("    beta"), line)

    def test_format_paragraph_inherits_its_own_leading_indent(self) -> None:
        formatted = commit_message.format_paragraph("    gamma " * 12, width=40)

        for line in formatted.split("\n"):
            self.assertTrue(line.startswith("    gamma"), line)

    def test_format_paragraph_honours_an_explicit_indent(self) -> None:
        formatted = commit_message.format_paragraph(
            "delta " * 12, width=40, initial_indent=">> ", subsequent_indent="   "
        ).split("\n")

        self.assertTrue(formatted[0].startswith(">> delta"))
        for line in formatted[1:]:
            self.assertTrue(line.startswith("   delta"), line)

    def test_wrapping_never_breaks_a_long_word_or_a_hyphenated_one(self) -> None:
        url = "https://example.com/a/very/long/path/that/exceeds/the/wrap/width"

        formatted = commit_message.format_paragraph(f"see {url} ok", width=40)

        self.assertIn(url, formatted.split("\n")[1])


class FormatCommitMessageTests(unittest.TestCase):
    # A message exercising every branch at once: preamble, an over-long title,
    # a paragraph needing a re-wrap, mixed bullet markers, a bullet
    # continuation line, preserved blank lines, and an attribution trailer.
    REPRESENTATIVE = (
        "I've read the diff and here is the commit message you asked for.\n"
        "\n"
        "feat(commit): add a native commit message generator that no longer "
        "shells out to the retired spec binary\n"
        "\n"
        "Replace the external spec commit dependency with an in-tree "
        "implementation so a\n"
        "run no longer dies on a bare exit 127 when the binary disappears. "
        "The runner and the gitsummary skill now share one code path.\n"
        "\n"
        "Collapse the three copies of the logic:\n"
        "\n"
        "  - Port the diff budgeting from the skill so truncation stays "
        "byte-identical\n"
        "    across both callers.\n"
        "- Port the formatter and add the API-error guard the skill copy "
        "never had.\n"
        "  1. Keep the Conventional Commits rules and the type table intact.\n"
        "\n"
        "Commit failures now soft-fail instead of pausing the run.\n"
        "\n"
        "Co-authored-by: Someone Else <someone@example.com>\n"
    )

    EXPECTED = (
        "feat(commit): add a native commit message generator that no longer "
        "shells out to the retired spec binary\n"
        "\n"
        "Replace the external spec commit dependency with an in-tree "
        "implementation so a\n"
        "run no longer dies on a bare exit 127 when the binary disappears. "
        "The runner and\n"
        "the gitsummary skill now share one code path.\n"
        "\n"
        "Collapse the three copies of the logic:\n"
        "\n"
        "  - Port the diff budgeting from the skill so truncation stays "
        "byte-identical\n"
        "    across both callers.\n"
        "- Port the formatter and add the API-error guard the skill copy "
        "never had.\n"
        "  1. Keep the Conventional Commits rules and the type table intact.\n"
        "\n"
        "Commit failures now soft-fail instead of pausing the run."
    )

    def test_representative_message_matches_the_ported_output(self) -> None:
        self.assertEqual(
            commit_message.format_commit_message(self.REPRESENTATIVE), self.EXPECTED
        )

    def test_title_is_kept_verbatim_while_the_body_wraps_at_eighty(self) -> None:
        lines = commit_message.format_commit_message(self.REPRESENTATIVE).split("\n")

        title = lines[0]
        self.assertEqual(
            title,
            "feat(commit): add a native commit message generator that no "
            "longer shells out to the retired spec binary",
        )
        self.assertGreater(len(title), 80)
        for line in lines[1:]:
            self.assertLessEqual(len(line), 80, line)

    def test_bullet_hanging_indents_align_to_the_text_column(self) -> None:
        lines = commit_message.format_commit_message(self.REPRESENTATIVE).split("\n")

        self.assertIn(
            "  - Port the diff budgeting from the skill so truncation stays "
            "byte-identical",
            lines,
        )
        # The continuation was a separate source line; it is re-joined and
        # hung under the bullet's text column rather than left as written.
        self.assertIn("    across both callers.", lines)

    def test_blank_lines_between_sections_are_preserved(self) -> None:
        lines = commit_message.format_commit_message(self.REPRESENTATIVE).split("\n")

        # One after the title, one after the re-wrapped paragraph, one before
        # the bullet list, and one before the closing paragraph.
        self.assertEqual(
            [index for index, line in enumerate(lines) if not line], [1, 5, 7, 12]
        )

    def test_trailing_blank_lines_are_dropped(self) -> None:
        self.assertEqual(
            commit_message.format_commit_message("docs: update\n\nbody\n\n\n\n"),
            "docs: update\n\nbody",
        )

    def test_the_width_is_configurable(self) -> None:
        message = "fix: x\n\n" + "word " * 30

        wrapped = commit_message.format_commit_message(message, width=40)

        for line in wrapped.split("\n")[1:]:
            self.assertLessEqual(len(line), 40, line)


class TruncationNoteTests(unittest.TestCase):
    def test_an_untruncated_diff_gets_no_note(self) -> None:
        result = commit_message.DiffResult(
            diff="x", files_included=["a.py"], is_truncated=False
        )

        self.assertEqual(commit_message.build_truncation_note(result), "")

    def test_the_note_counts_each_reduction_kind(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            binary_files=["a.png", "b.png"],
            truncated_files=["big.py", "bigger.py", "biggest.py"],
            omitted_files=["c.py", "d.py"],
            is_truncated=True,
        )

        note = commit_message.build_truncation_note(result)

        self.assertIn("2 binary file(s)", note)
        self.assertIn("3 file(s) partially shown", note)
        self.assertIn("2 file(s) omitted: c.py, d.py.", note)
        self.assertIn("focus on what IS visible", note)

    def test_the_omitted_list_caps_at_five_names_plus_a_remainder(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            omitted_files=[f"src/mod_{index}.py" for index in range(8)],
            is_truncated=True,
        )

        note = commit_message.build_truncation_note(result)

        self.assertIn(
            "8 file(s) omitted: src/mod_0.py, src/mod_1.py, src/mod_2.py, "
            "src/mod_3.py, src/mod_4.py, ... (+3 more)",
            note,
        )
        self.assertNotIn("src/mod_5.py", note)

    def test_the_note_reports_only_the_kinds_that_apply(self) -> None:
        result = commit_message.DiffResult(
            diff="x", binary_files=["a.png"], is_truncated=True
        )

        note = commit_message.build_truncation_note(result)

        self.assertIn("size limits. 1 binary file(s).", note)
        self.assertNotIn("partially shown", note)
        self.assertNotIn("file(s) omitted", note)

    def test_the_prompt_note_caps_looser_than_the_header_notice(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            omitted_files=[f"m{index}.py" for index in range(5)],
            is_truncated=True,
        )

        # Five names fit the prompt note whole; the header notice keeps three.
        self.assertIn("m4.py", commit_message.build_truncation_note(result))
        self.assertIn("(+2 more)", commit_message.build_truncation_notice(result))


class CommitPromptTests(unittest.TestCase):
    def build_result(self, **overrides: object) -> commit_message.DiffResult:
        fields: dict[str, object] = {
            "diff": build_file_diff("src/alpha.py", hunks=1, lines=2),
            "files_included": ["src/alpha.py"],
        }
        fields.update(overrides)
        return commit_message.DiffResult(**fields)  # type: ignore[arg-type]

    def test_the_prompt_carries_the_rules_the_type_table_and_the_diff(self) -> None:
        result = self.build_result()

        prompt = commit_message.build_commit_prompt(result)

        self.assertTrue(prompt.startswith("You are a commit message generator."))
        self.assertIn("Conventional Commits specification", prompt)
        self.assertIn("## Format Requirements", prompt)
        self.assertIn("## Content Rules", prompt)
        for commit_type in (
            "feat",
            "fix",
            "docs",
            "refactor",
            "test",
            "chore",
            "perf",
            "style",
        ):
            with self.subTest(commit_type=commit_type):
                self.assertIn(f"\n- {commit_type}: ", prompt)
        self.assertIn("Keep title under 72 characters", prompt)
        self.assertIn("Wrap body at 80 characters", prompt)
        self.assertIn('No author attribution (no "Co-authored-by:"', prompt)
        self.assertIn("No markdown syntax", prompt)
        self.assertIn("Use PRESENT TENSE", prompt)
        self.assertIn("Focus on WHAT changed and WHY it matters", prompt)
        self.assertIn("Do NOT describe HOW the code works", prompt)
        self.assertTrue(prompt.endswith("Generate the commit message:"))

    def test_the_diff_is_inlined_under_its_own_heading(self) -> None:
        result = self.build_result()

        prompt = commit_message.build_commit_prompt(result)

        self.assertIn(f"## Git Diff\n\n{result.diff}\n", prompt)
        self.assertNotIn("Read tool", prompt)

    def test_guidance_appears_in_its_own_section_when_supplied(self) -> None:
        prompt = commit_message.build_commit_prompt(
            self.build_result(), guidance="Mention the ticket number ABC-123."
        )

        self.assertIn(
            "\n## User Guidance\n\nMention the ticket number ABC-123.\n", prompt
        )

    def test_no_guidance_section_without_guidance(self) -> None:
        for guidance in (None, ""):
            with self.subTest(guidance=guidance):
                prompt = commit_message.build_commit_prompt(
                    self.build_result(), guidance=guidance
                )

                self.assertNotIn("User Guidance", prompt)

    def test_the_truncation_note_is_embedded_only_when_truncated(self) -> None:
        clean = commit_message.build_commit_prompt(self.build_result())
        self.assertNotIn("truncated to fit within size limits", clean)

        truncated_result = self.build_result(
            truncated_files=["src/alpha.py"], is_truncated=True
        )
        truncated = commit_message.build_commit_prompt(truncated_result)
        self.assertIn(commit_message.build_truncation_note(truncated_result), truncated)
        self.assertIn("1 file(s) partially shown", truncated)

    def test_the_dropped_spec_couplings_are_absent(self) -> None:
        prompt = commit_message.build_commit_prompt(self.build_result())

        # The feature-name context note and the spec-dir large-content branch
        # are the two couplings the port drops; only their type-table and
        # content-rule neighbours survive.
        self.assertNotIn("This change is part of the", prompt)
        self.assertNotIn("do NOT mention the feature name", prompt)
        self.assertNotIn(".spec", prompt)


class CommitHandlerTests(unittest.TestCase):
    """The four `asl commit` modes, driven by the fake codex harness fixture.

    Generation runs the real adapter against the real fixture executable, so
    these exercise the actual audit-profile argv rather than a patched
    ``run_harness``; the fixture records the argv and prompt it was handed.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

        self.repo = self.tmp_path / "repo"
        init_git_repo(self.repo)

        # `native_model_payload` reads `~/.codex/config.toml`, so the reported
        # model source would otherwise depend on the developer's own machine.
        self.home = self.tmp_path / "home"
        self.home.mkdir()

        self.fake_codex = install_fake_codex(self.tmp_path)
        self.config_home = self.tmp_path / "config"
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )
        self.record_path = self.tmp_path / "commit-turn.json"

    # -- helpers ---------------------------------------------------------

    def run_commit(
        self,
        *argv: str,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["commit", *argv])
        overrides: dict[str, str | None] = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "FAKE_CODEX_COMMIT_RECORD": str(self.record_path),
        }
        overrides.update(env or {})

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patched_env(**overrides), redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is None:
                code = commit_message.commit_from_args(args)
            else:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = commit_message.commit_from_args(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def write(self, relative: str, text: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def head_message(self) -> str:
        return git_stdout(self.repo, "log", "-1", "--format=%B").strip()

    def porcelain(self) -> str:
        # `-uall` so an untracked file inside a new directory is named rather
        # than collapsed into its parent.
        return git_stdout(self.repo, "status", "--porcelain", "-uall")

    def commit_count(self) -> int:
        return int(git_stdout(self.repo, "rev-list", "--count", "HEAD"))

    def recorded_turn(self) -> dict[str, Any]:
        """The argv and prompt the fake codex was handed for its one turn."""
        return cast(
            dict[str, Any], json.loads(self.record_path.read_text(encoding="utf-8"))
        )

    # -- default mode ----------------------------------------------------

    def test_default_mode_stages_generates_and_commits(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(0, code, err)
        self.assertEqual(before + 1, self.commit_count())
        self.assertIn("feat: add the fake codex commit branch", out)
        # Stdout is the committed text and nothing else, so `asl commit` can be
        # piped; the outcome line goes to stderr.
        self.assertEqual(self.head_message(), out.strip())
        self.assertIn("asl commit: committed", err)
        # The message was formatted, not committed as the harness wrote it.
        message = self.head_message().splitlines()
        self.assertEqual("feat: add the fake codex commit branch", message[0])
        self.assertEqual("", message[1])
        self.assertTrue(all(len(line) <= 80 for line in message[2:]), message)
        self.assertEqual("", self.porcelain())

    def test_generation_runs_under_the_audit_read_only_profile(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(0, code, err)
        argv = self.recorded_turn()["argv"]
        self.assertIn("-s", argv)
        self.assertEqual("read-only", argv[argv.index("-s") + 1])
        self.assertIn("--skip-git-repo-check", argv)
        # `--add-dir` is the worker's writable grant and must never appear.
        self.assertNotIn("--add-dir", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_the_prompt_inlines_the_staged_diff(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(0, code, err)
        prompt = self.recorded_turn()["prompt"]
        self.assertIn("diff --git a/src/alpha.py b/src/alpha.py", prompt)
        self.assertIn("+print('alpha')", prompt)
        self.assertTrue(prompt.endswith("Generate the commit message:"))

    def test_guidance_model_and_effort_reach_the_harness(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--guidance",
            "mention the alpha module",
            "--model",
            "gpt-5",
            "--effort",
            "high",
        )

        self.assertEqual(0, code, err)
        turn = self.recorded_turn()
        self.assertIn("mention the alpha module", turn["prompt"])
        argv = turn["argv"]
        self.assertEqual("gpt-5", argv[argv.index("--model") + 1])
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_dry_run_prints_the_message_without_committing(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(str(self.repo), "--yes", "--dry-run")

        self.assertEqual(0, code, err)
        self.assertEqual(before, self.commit_count())
        self.assertIn("feat: add the fake codex commit branch", out)
        # Staging still happened; only the commit was withheld.
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_staged_mode_refuses_an_empty_index(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--yes", "--staged")

        self.assertEqual(1, code)
        self.assertIn("no staged changes", err)
        # An unstaged worktree change is left exactly as it was.
        self.assertIn("?? src/alpha.py", self.porcelain())

    def test_staged_mode_commits_only_what_was_already_staged(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/alpha.py"])

        code, _out, err = self.run_commit(str(self.repo), "--yes", "--staged")

        self.assertEqual(0, code, err)
        self.assertIn("src/alpha.py", self.recorded_turn()["prompt"])
        self.assertNotIn("src/beta.py", self.recorded_turn()["prompt"])
        self.assertIn("?? src/beta.py", self.porcelain())

    def test_pathspec_stages_only_the_named_paths(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--pathspec", "src/alpha.py"
        )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        self.assertIn("?? src/beta.py", self.porcelain())

    def test_pathspec_excludes_preexisting_staged_paths(self) -> None:
        # The index is not empty when the scoped commit starts: `src/beta.py`
        # was staged by whoever ran `asl commit --pathspec`, and the scoped
        # contract says it must neither be committed nor unstaged.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--pathspec", "src/alpha.py"
        )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        # The pre-staged path stayed in the index, still staged against the new
        # HEAD, and never reached the generation prompt.
        self.assertIn("A  src/beta.py", self.porcelain())
        self.assertNotIn("src/alpha.py", self.porcelain())
        self.assertNotIn("src/beta.py", self.recorded_turn()["prompt"])

    def test_pathspec_does_not_commit_post_capture_edits(self) -> None:
        # An edit landing during the generation turn leaves the worktree
        # describing something the message was never written from, so the run
        # refuses rather than committing the captured blob behind the
        # operator's back.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--pathspec",
            "src/alpha.py",
            env={
                "FAKE_CODEX_COMMIT_EDIT_PATH": "src/alpha.py",
                "FAKE_CODEX_COMMIT_EDIT_TEXT": "print('smuggled')\n",
            },
        )

        self.assertEqual(1, code)
        self.assertIn("changed after the diff was captured", err)
        self.assertIn("src/alpha.py", err)
        self.assertEqual(before, self.commit_count())
        # The generation prompt only ever saw the captured content, and the
        # index still holds exactly that.
        self.assertNotIn("smuggled", self.recorded_turn()["prompt"])
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", ":src/alpha.py")
        )
        # The unrelated pre-existing index entry is neither committed nor
        # discarded by the refusal.
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_pathspec_reports_drift_as_its_own_reason(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            "--pathspec",
            "src/alpha.py",
            env={"FAKE_CODEX_COMMIT_EDIT_PATH": "src/alpha.py"},
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_SNAPSHOT_DRIFTED, payload["reason"])
        self.assertEqual(["src/alpha.py"], payload["details"]["paths"])

    def test_two_step_pathspec_does_not_commit_post_capture_edits(self) -> None:
        # The gitsummary skill prepares, writes the message from its own
        # context, and then commits through a second invocation; the drift
        # window spans the two calls.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--pathspec", "src/alpha.py"
        )
        self.assertEqual(0, code, err)
        snapshot = json.loads(out.partition("\n\n")[0])["snapshot"]
        self.assertTrue(snapshot)

        self.write("src/alpha.py", "print('smuggled')\n")
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--pathspec",
            "src/alpha.py",
            "--expect-snapshot",
            snapshot,
            "--message-file",
            "-",
            stdin="feat: add the alpha module\n\nIt prints a greeting.\n",
        )

        self.assertEqual(1, code)
        self.assertIn("changed after the diff was captured", err)
        self.assertEqual(before, self.commit_count())
        # Refusing came before staging, so the index still holds the prepared
        # snapshot rather than the drifted content.
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", ":src/alpha.py")
        )
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_two_step_pathspec_commits_the_captured_snapshot(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--pathspec", "src/alpha.py"
        )
        self.assertEqual(0, code, err)
        snapshot = json.loads(out.partition("\n\n")[0])["snapshot"]

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--pathspec",
            "src/alpha.py",
            "--expect-snapshot",
            snapshot,
            "--message-file",
            "-",
            stdin="feat: add the alpha module\n\nIt prints a greeting.\n",
        )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", "HEAD:src/alpha.py")
        )
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_pathspec_commit_outruns_an_edit_landing_after_validation(self) -> None:
        # Every other drift test mutates the path early enough for a later
        # check to see it.  This one lands the edit in the only window no
        # check can cover: after the final validation returns and before git
        # is asked to commit.  Nothing can re-check it, so the commit has to
        # be built from content fixed before that window opens.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])
        validated = commit_message.assert_pathspec_snapshot

        def smuggle(*args: object, **kwargs: object) -> None:
            validated(*args, **kwargs)  # type: ignore[arg-type]
            self.write("src/alpha.py", "print('smuggled')\n")

        with mock.patch.object(
            commit_message, "assert_pathspec_snapshot", side_effect=smuggle
        ):
            code, _out, err = self.run_commit(
                str(self.repo), "--yes", "--pathspec", "src/alpha.py"
            )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        # The commit carries the captured snapshot, not what the worktree held
        # a moment later, and the message was never written from the latter.
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", "HEAD:src/alpha.py")
        )
        self.assertNotIn("smuggled", self.recorded_turn()["prompt"])
        # The late edit is not swallowed either: it stays on disk as an
        # uncommitted modification for whoever made it.
        self.assertEqual(
            "print('smuggled')",
            (self.repo / "src/alpha.py").read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", ":src/alpha.py")
        )
        # The unrelated pre-existing index entry survives the scoped commit.
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_scoped_commit_carries_a_staged_deletion_as_a_deletion(self) -> None:
        # Seeding the scoped index from HEAD reinstates every path HEAD
        # carries, including one staged for deletion — no `--index-info` entry
        # exists to overwrite it — so it needs removing again or it would
        # quietly survive the commit meant to delete it.  Driven below the
        # CLI. Re-staging the deleted path must also succeed: it is still in
        # the commit scope even though it has already left the index.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/alpha.py", "src/beta.py"])
        git_stdout(self.repo, "commit", "-m", "seed", "--no-verify")

        (self.repo / "src/alpha.py").unlink()
        self.write("src/beta.py", "print('beta edited')\n")
        commit_message.stage_paths(self.repo, ["src/alpha.py", "src/beta.py"])

        staged = commit_message.stage_paths(self.repo, ["src/alpha.py"])
        self.assertEqual(0, staged.returncode, staged.stderr)
        committed = commit_message.commit_staged(
            self.repo, "chore: drop alpha\n", pathspec=["src/alpha.py"]
        )

        self.assertEqual(0, committed.returncode, committed.stderr)
        self.assertEqual(
            ["src/alpha.py"],
            git_stdout(
                self.repo, "show", "--name-only", "--format=", "HEAD"
            ).splitlines(),
        )
        self.assertNotIn(
            "src/alpha.py",
            git_stdout(self.repo, "ls-tree", "-r", "--name-only", "HEAD"),
        )
        # The unrelated staged edit is neither committed nor unstaged.
        self.assertIn("M  src/beta.py", self.porcelain())

    def test_expect_snapshot_without_pathspec_is_refused(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--expect-snapshot", "deadbeefdeadbeef"
        )

        self.assertEqual(1, code)
        self.assertIn("--expect-snapshot", err)
        self.assertIn("?? src/alpha.py", self.porcelain())

    def test_a_clean_worktree_is_refused_before_any_harness_turn(self) -> None:
        code, _out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(1, code)
        self.assertIn("no changes to commit", err)
        self.assertFalse(self.record_path.exists())

    def test_an_empty_commit_is_refused_as_an_empty_diff(self) -> None:
        git_stdout(self.repo, "commit", "--allow-empty", "-m", "nothing", "--no-verify")

        code, out, _err = self.run_commit(str(self.repo), "--from-id", "HEAD", "--json")

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_EMPTY_DIFF, payload["reason"])
        self.assertFalse(self.record_path.exists())

    def test_an_all_binary_change_is_named_as_such(self) -> None:
        (self.repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
        commit_message.stage_all(self.repo)
        self.assertTrue(commit_message.changes_are_all_binary(self.repo))

        # Git still emits a header and a `[Binary file]` summary for a binary
        # change, so the processed diff is only ever empty when git itself
        # produced no patch text; the probe above is what tells that case apart
        # from having nothing staged at all.
        empty = commit_message.DiffResult(diff="", is_empty=True)
        with mock.patch.object(
            commit_message, "capture_staged_diff", return_value=empty
        ):
            code, out, _err = self.run_commit(str(self.repo), "--yes", "--json")

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(commit_message.REASON_ALL_BINARY, payload["reason"])
        self.assertIn("all changed files are binary", payload["error"])

    def test_a_mixed_change_is_not_reported_as_all_binary(self) -> None:
        (self.repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
        self.write("src/alpha.py", "print('alpha')\n")
        commit_message.stage_all(self.repo)

        self.assertFalse(commit_message.changes_are_all_binary(self.repo))

    def test_not_a_git_worktree_is_refused(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir()

        code, _out, err = self.run_commit(str(outside), "--yes")

        self.assertEqual(1, code)
        self.assertIn("not a git worktree", err)

    def test_json_reports_the_commit_and_the_resolved_generation_settings(
        self,
    ) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(str(self.repo), "--yes", "--json")

        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual("commit", payload["command"])
        self.assertEqual("commit", payload["mode"])
        self.assertTrue(payload["committed"])
        self.assertTrue(payload["generated"])
        self.assertEqual(git_stdout(self.repo, "rev-parse", "HEAD"), payload["commit"])
        self.assertEqual("codex", payload["harness"])
        self.assertEqual(str(self.fake_codex), payload["harness_executable"])
        self.assertEqual(config.MODEL_SOURCE_UNRESOLVED, payload["model_source"])
        self.assertIsNone(payload["effort"])
        self.assertIsNone(payload["effort_source"])

    # -- harness failure handling ----------------------------------------

    def test_a_nonzero_harness_exit_is_classified_not_reported_raw(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            env={"FAKE_CODEX_COMMIT_MODE": "quota"},
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_GENERATION_FAILED, payload["reason"])
        classification = payload["details"]["classification"]
        self.assertEqual("quota-exhausted", classification["reason"])
        self.assertEqual(7, classification["returncode"])
        # `retry_hints` resolves the provider hint to an absolute instant.
        self.assertIsNotNone(payload["details"]["retry_at"])
        # The staged change survives, so the operator can retry or commit by hand.
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_an_unrecognized_failure_falls_closed_to_nonzero_exit(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            env={"FAKE_CODEX_COMMIT_MODE": "failure"},
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual("nonzero-exit", payload["details"]["classification"]["reason"])
        self.assertIsNone(payload["details"]["retry_at"])

    def test_generation_timeout_is_bounded(self) -> None:
        # The bound is the command's own, not the caller's: `asl commit` with
        # no `--timeout` still refuses to wait forever, which is what keeps a
        # stalled message harness from hanging a standalone commit.
        parser_default = cli.build_parser().parse_args(["commit"]).timeout
        self.assertEqual(
            commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS, parser_default
        )
        self.assertIsNotNone(parser_default)
        self.assertEqual(
            commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS,
            inspect.signature(commit_message.generate_commit_message)
            .parameters["timeout"]
            .default,
        )

        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()
        pid_path = self.tmp_path / "hang-child.pid"

        started = time.monotonic()
        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            "--timeout",
            str(GENERATION_TIMEOUT_PROBE_SECONDS),
            env={
                "FAKE_CODEX_COMMIT_MODE": "hang",
                "FAKE_CODEX_COMMIT_HANG_CHILD_PID": str(pid_path),
            },
        )
        elapsed = time.monotonic() - started

        self.assertEqual(1, code)
        # The bound is what ended the turn, not the fixture's own 600s sleep.
        self.assertLess(elapsed, 120, "the harness turn outlived its bound")
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_GENERATION_TIMEOUT, payload["reason"])
        self.assertEqual(
            GENERATION_TIMEOUT_PROBE_SECONDS, payload["details"]["timeout_seconds"]
        )
        self.assertIn(
            "did not produce a commit message within "
            f"{GENERATION_TIMEOUT_PROBE_SECONDS}s",
            payload["error"],
        )
        # Nothing was committed and the staged change survives for a retry.
        self.assertEqual(before, self.commit_count())
        self.assertTrue(commit_message.has_staged_changes(self.repo))
        # The whole process tree went down, not just the harness process: the
        # grandchild inherited the turn's process group and is killed with it.
        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a harness descendant outlived the generation timeout",
        )

    def test_a_non_positive_timeout_is_refused_before_staging(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo), "--yes", "--json", "--timeout", "0"
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_INVALID_ARGUMENTS, payload["reason"])
        # Refused ahead of staging, so the index is untouched.
        self.assertFalse(commit_message.has_staged_changes(self.repo))
        self.assertFalse(self.record_path.exists())

    def test_an_empty_response_is_rejected(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", env={"FAKE_CODEX_COMMIT_MODE": "empty"}
        )

        self.assertEqual(1, code)
        self.assertIn("returned an empty commit message", err)

    def test_an_api_error_body_is_never_committed(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", env={"FAKE_CODEX_COMMIT_MODE": "api-error"}
        )

        self.assertEqual(1, code)
        self.assertEqual(before, self.commit_count())
        self.assertIn("returned an API error", err)

    # -- `--from-id` mode ------------------------------------------------

    def test_from_id_prints_a_message_without_staging_or_committing(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        commit_message.stage_all(self.repo)
        git_stdout(self.repo, "commit", "-m", "add alpha", "--no-verify")
        sha = git_stdout(self.repo, "rev-parse", "--short", "HEAD")
        self.write("src/beta.py", "print('beta')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(str(self.repo), "--from-id", sha)

        self.assertEqual(0, code, err)
        self.assertEqual(before, self.commit_count())
        self.assertIn("feat: add the fake codex commit branch", out)
        self.assertIn("src/alpha.py", self.recorded_turn()["prompt"])
        # The unrelated worktree change was never staged.
        self.assertIn("?? src/beta.py", self.porcelain())

    def test_from_id_json_reports_that_nothing_was_committed(self) -> None:
        sha = git_stdout(self.repo, "rev-parse", "HEAD")

        code, out, err = self.run_commit(str(self.repo), "--from-id", sha, "--json")

        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual("from-id", payload["mode"])
        self.assertFalse(payload["committed"])
        self.assertEqual(sha, payload["from_id"])

    def test_from_id_rejects_an_unresolvable_sha(self) -> None:
        code, _out, err = self.run_commit(str(self.repo), "--from-id", "deadbee")

        self.assertEqual(1, code)
        self.assertIn("commit not found", err)

    # -- `--prepare-only` mode -------------------------------------------

    def test_prepare_only_emits_the_header_then_body_contract(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--guidance", "be terse"
        )

        self.assertEqual(0, code, err)
        self.assertEqual(before, self.commit_count())
        # The exact parse the retired `gitsummary-prepare.py` consumers did.
        header_line, _blank, body = out.partition("\n\n")
        header = json.loads(header_line)
        self.assertEqual(
            [
                "mode",
                "yes",
                "no_verify",
                "guidance",
                "is_truncated",
                "truncation_notes",
                "from_id",
                "snapshot",
            ],
            list(header),
        )
        self.assertEqual("commit", header["mode"])
        self.assertFalse(header["yes"])
        self.assertTrue(header["no_verify"])
        self.assertEqual("be terse", header["guidance"])
        self.assertFalse(header["is_truncated"])
        self.assertEqual("", header["truncation_notes"])
        self.assertIsNone(header["from_id"])
        # An unscoped invocation commits the index it is handed, so it has no
        # named-path snapshot to carry forward.
        self.assertIsNone(header["snapshot"])
        self.assertIn("diff --git a/src/alpha.py b/src/alpha.py", body)
        # No harness turn is needed to prepare.
        self.assertFalse(self.record_path.exists())

    def test_prepare_only_stages_the_worktree_like_the_skill_does(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--prepare-only")

        self.assertEqual(0, code, err)
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_prepare_only_reports_truncation_in_its_header(self) -> None:
        (self.repo / "assets").mkdir()
        (self.repo / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(str(self.repo), "--prepare-only")

        self.assertEqual(0, code, err)
        header = json.loads(out.partition("\n\n")[0])
        self.assertTrue(header["is_truncated"])
        self.assertIn("1 binary file(s) summarized", header["truncation_notes"])

    def test_prepare_only_from_id_reports_the_from_id_mode(self) -> None:
        sha = git_stdout(self.repo, "rev-parse", "HEAD")

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--from-id", sha
        )

        self.assertEqual(0, code, err)
        header = json.loads(out.partition("\n\n")[0])
        self.assertEqual("from-id", header["mode"])
        self.assertEqual(sha, header["from_id"])

    def test_prepare_only_json_carries_the_same_header_plus_the_diff(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(str(self.repo), "--prepare-only", "--json")

        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual("prepare-only", payload["mode"])
        self.assertEqual("commit", payload["header"]["mode"])
        self.assertIn("diff --git a/src/alpha.py", payload["diff"])

    # -- `--message-file` mode -------------------------------------------

    def test_message_file_commits_without_a_harness(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        message_path = self.tmp_path / "message.txt"
        message_path.write_text(
            "fix: correct the alpha greeting\n\nThe module printed the wrong "
            "word, which every caller then repeated downstream.\n",
            encoding="utf-8",
        )
        before = self.commit_count()

        # No harness is configured at all, proving this mode never needs one.
        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--message-file",
            str(message_path),
            env={"XDG_CONFIG_HOME": str(self.tmp_path / "absent")},
        )

        self.assertEqual(0, code, err)
        self.assertEqual(before + 1, self.commit_count())
        self.assertEqual(
            "fix: correct the alpha greeting", self.head_message().splitlines()[0]
        )
        self.assertFalse(self.record_path.exists())

    def test_message_file_dash_reads_stdin_and_formats_the_message(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--message-file",
            "-",
            stdin=(
                "feat: add alpha\n\n"
                "This body is deliberately written as one very long single line "
                "so that the formatter has to rewrap it at eighty columns before "
                "git ever sees the text.\n"
            ),
        )

        self.assertEqual(0, code, err)
        body_lines = self.head_message().splitlines()[2:]
        self.assertTrue(body_lines)
        self.assertTrue(all(len(line) <= 80 for line in body_lines), body_lines)
        self.assertIn("feat: add alpha", out)

    def test_message_file_strips_a_disallowed_trailer_before_committing(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--message-file",
            "-",
            stdin="feat: add alpha\n\nA body.\n\nCo-authored-by: Someone <a@b.c>\n",
        )

        self.assertEqual(0, code, err)
        self.assertNotIn("Co-authored-by", self.head_message())

    def test_message_file_refuses_an_empty_message(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--message-file", "-", stdin="   \n\n"
        )

        self.assertEqual(1, code)
        self.assertEqual(before, self.commit_count())
        self.assertIn("empty", err)

    def test_message_file_refuses_a_missing_file(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--message-file", str(self.tmp_path / "absent.txt")
        )

        self.assertEqual(1, code)
        self.assertIn("cannot read message file", err)

    # -- mode conflicts --------------------------------------------------

    def test_contradictory_mode_flags_are_refused(self) -> None:
        combinations = (
            ("--from-id", "HEAD", "--staged"),
            ("--from-id", "HEAD", "--pathspec", "src/alpha.py"),
            ("--from-id", "HEAD", "--message-file", "-"),
            ("--prepare-only", "--message-file", "-"),
            ("--staged", "--pathspec", "src/alpha.py"),
        )
        for combination in combinations:
            with self.subTest(combination=combination):
                code, out, err = self.run_commit(str(self.repo), "--json", *combination)
                self.assertEqual(1, code)
                payload = json.loads(out)
                self.assertEqual(
                    commit_message.REASON_INVALID_ARGUMENTS, payload["reason"]
                )
                self.assertIn("mutually exclusive", payload["error"])


class HarnessResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.config_home = self.tmp_path / "config"
        self.config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def test_an_explicit_harness_wins_over_configuration(self) -> None:
        self.config_path.write_text(
            json.dumps({"codex": {"executable": "codex"}}), encoding="utf-8"
        )
        with patched_env(XDG_CONFIG_HOME=str(self.config_home)):
            self.assertEqual("claude", commit_message.resolve_harness("claude"))

    def test_a_configured_installed_harness_is_preferred(self) -> None:
        fake = install_fake_codex(self.tmp_path)
        self.config_path.write_text(
            json.dumps({"copilot": {"executable": str(fake)}}), encoding="utf-8"
        )
        with patched_env(XDG_CONFIG_HOME=str(self.config_home)):
            self.assertEqual("copilot", commit_message.resolve_harness())

    def test_no_installed_harness_is_reported_rather_than_launched(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    name: {"executable": str(self.tmp_path / f"absent-{name}")}
                    for name in ("codex", "claude", "copilot")
                }
            ),
            encoding="utf-8",
        )
        with (
            patched_env(XDG_CONFIG_HOME=str(self.config_home)),
            self.assertRaises(commit_message.CommitMessageError) as caught,
        ):
            commit_message.resolve_harness()

        self.assertEqual(commit_message.REASON_NO_HARNESS, caught.exception.reason)
        self.assertIn("no harness executable found", str(caught.exception))

    def test_settings_report_a_flag_model_and_effort(self) -> None:
        fake = install_fake_codex(self.tmp_path)
        self.config_path.write_text(
            json.dumps({"codex": {"executable": str(fake)}}), encoding="utf-8"
        )
        with patched_env(XDG_CONFIG_HOME=str(self.config_home)):
            settings = commit_message.generation_settings("codex", "gpt-5", "high")

        self.assertEqual("codex", settings["harness"])
        self.assertEqual(str(fake), settings["harness_executable"])
        self.assertEqual("gpt-5", settings["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, settings["model_source"])
        self.assertEqual("high", settings["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, settings["effort_source"])


if __name__ == "__main__":
    unittest.main()
